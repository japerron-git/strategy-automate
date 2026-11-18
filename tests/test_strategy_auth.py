import os as _os
import sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import _hermetic  # noqa: E402,F401  (scrub MSTR_*/proxy env, private secret store)

import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(ROOT, "skills", "strategy-platform", "scripts"))

import requests  # noqa: E402
from requests.structures import CaseInsensitiveDict  # noqa: E402

import strategy_auth as sa  # noqa: E402

BASE = "https://tenant.example.com/MicroStrategyLibrary"


class Resp:
    def __init__(self, status=204, headers=None, body=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = CaseInsensitiveDict(headers or {})
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """Records calls; answers from a {(verb, path): Resp} table."""

    def __init__(self, routes=None):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.verify = True
        self.calls = []
        self.routes = routes or {}

    def _do(self, verb, url, **kw):
        path = url.replace(BASE, "")
        self.calls.append((verb, path, kw.get("json")))
        resp = self.routes.get((verb, path), Resp(404))
        return resp

    def post(self, url, **kw):
        return self._do("POST", url, **kw)

    def get(self, url, **kw):
        return self._do("GET", url, **kw)

    def delete(self, url, **kw):
        return self._do("DELETE", url, **kw)


def cfg(**kw):
    with mock.patch.dict(os.environ, {}, clear=True):
        return sa.AuthConfig.from_env(base=BASE, cache=False, **kw)


LOGIN_OK = {("POST", "/api/auth/login"): Resp(204, {"X-MSTR-AuthToken": "TOK"})}


class ConfigTests(unittest.TestCase):
    def test_ldap_alias_and_unknown_method(self):
        c = cfg(method="ldap")
        self.assertEqual((c.method, c.login_mode), ("password", 16))
        with self.assertRaises(sa.AuthError):
            cfg(method="kerberos")

    def test_env_values_and_flag_overrides(self):
        env = {"MSTR_BASE": BASE + "/", "MSTR_USER": "u", "MSTR_PASSWORD": "p", "MSTR_LOGIN_MODE": "16",
               "MSTR_SESSION_CACHE": "0", "MSTR_SSO_PORT": "9000"}
        with mock.patch.dict(os.environ, env, clear=True):
            c = sa.AuthConfig.from_env(username="flag-user", password="")
        self.assertEqual((c.base, c.username, c.password, c.login_mode), (BASE, "flag-user", "p", 16))
        self.assertEqual((c.cache, c.sso_port), (False, 9000))

    def test_auto_prefers_explicit_credentials_over_cache(self):
        self.assertEqual(sa._auto_method(cfg(api_token="T", username="u", password="p")), "api-token")
        self.assertEqual(sa._auto_method(cfg(username="u", password="p")), "password")
        self.assertEqual(sa._auto_method(cfg(identity_token="IT")), "identity-token")
        c = cfg()
        c.cache = True
        self.assertEqual(sa._auto_method(c), "cached")
        with mock.patch.object(sa, "_saved_api_token", return_value=""):
            self.assertEqual(sa._auto_method(cfg()), "sso")


class SignInTests(unittest.TestCase):
    def test_password_and_ldap_bodies(self):
        s = FakeSession(LOGIN_OK)
        r = sa.sign_in(s, cfg(username="u", password="p"))
        self.assertEqual(s.calls[0][2], {"username": "u", "password": "p", "loginMode": 1})
        self.assertEqual((r.method, r.owns_session, s.headers["X-MSTR-AuthToken"]), ("password", True, "TOK"))
        s = FakeSession(LOGIN_OK)
        self.assertEqual(sa.sign_in(s, cfg(method="ldap", username="u", password="p")).method, "ldap")
        self.assertEqual(s.calls[0][2]["loginMode"], 16)

    def test_api_token_goes_in_username_with_mode_4096(self):
        s = FakeSession(LOGIN_OK)
        sa.sign_in(s, cfg(method="api-token", api_token="APITOKEN"))
        self.assertEqual(s.calls[0][2], {"loginMode": 4096, "username": "APITOKEN"})

    def test_missing_password_explains_sso(self):
        with self.assertRaisesRegex(sa.AuthError, "--auth-method sso"):
            sa.sign_in(FakeSession(LOGIN_OK), cfg(method="password", username="u"))

    def test_failed_login_message_has_no_secret(self):
        s = FakeSession({("POST", "/api/auth/login"): Resp(401, body={"code": "ERR003", "message": "bad"})})
        with self.assertRaises(sa.AuthError) as ctx:
            sa.sign_in(s, cfg(username="u", password="hunter2"))
        self.assertIn("ERR003", str(ctx.exception))
        self.assertNotIn("hunter2", str(ctx.exception))

    def test_identity_token_delegates_without_verifier_and_is_not_owned(self):
        s = FakeSession({("POST", "/api/auth/delegate"): Resp(204, {"X-MSTR-AuthToken": "DT"}),
                         ("GET", "/api/sessions/userInfo"): Resp(200, body={"fullName": "A User"})})
        r = sa.sign_in(s, cfg(method="identity-token", identity_token="IT"))
        self.assertEqual(s.calls[0][2], {"loginMode": -1, "identityToken": "IT"})
        self.assertEqual((r.owns_session, r.user), (False, "A User"))

    def test_sign_out_only_for_owned_sessions(self):
        s = FakeSession({("POST", "/api/auth/logout"): Resp(204)})
        s.headers["X-MSTR-AuthToken"] = "TOK"
        sa.sign_out(s, BASE, sa.SignIn("sso", owns_session=False))
        self.assertEqual(s.calls, [])
        sa.sign_out(s, BASE, sa.SignIn("password", owns_session=True))
        self.assertEqual(s.calls, [("POST", "/api/auth/logout", None)])
        self.assertNotIn("X-MSTR-AuthToken", s.headers)


class PkceTests(unittest.TestCase):
    def test_rfc7636_vector(self):
        self.assertEqual(sa.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
                         "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_pair_is_random_and_consistent(self):
        v1, c1 = sa.pkce_pair()
        v2, _ = sa.pkce_pair()
        self.assertNotEqual(v1, v2)
        self.assertEqual(sa.pkce_challenge(v1), c1)
        self.assertTrue(43 <= len(v1) <= 128)

    def test_epoch_parsing(self):
        self.assertEqual(sa._epoch("1970-01-01T00:00:10Z"), 10)
        self.assertEqual(sa._epoch("1970-01-01T01:00:00.000+0100"), 0)
        self.assertEqual(sa._epoch("1970-01-01T00:00:00-01:00"), 3600)
        self.assertIsNone(sa._epoch("tomorrow"))


def _http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


class BrowserHandoffTests(unittest.TestCase):
    """Drive the loopback page the way a browser would."""

    def run_flow(self, browser):
        c = cfg(method="sso")
        c.sso_port, c.sso_timeout = 0, 10
        opened, threads = [], []

        def fake_open(url):
            opened.append(url)
            t = threading.Thread(target=browser, args=(url,), daemon=True)
            threads.append(t)
            t.start()
            return True

        try:
            with mock.patch.object(sa.webbrowser, "open", fake_open), mock.patch("sys.stderr"):
                return sa.browser_sso(c), opened
        finally:
            for t in threads:   # let the fake browser finish recording what it saw
                t.join(timeout=5)

    @staticmethod
    def page_cfg(url):
        status, html, headers = _http(url)
        assert status == 200, status
        assert "nonce-" in headers.get("Content-Security-Policy", "")
        return json.loads(re.search(r'id="cfg">(.*?)</script>', html, re.S).group(1))

    def test_allow_returns_identity_token_bound_to_pkce(self):
        seen = {}

        def browser(url):
            page = self.page_cfg(url)
            seen.update(page)
            origin = url.rstrip("/")
            # wrong state and missing Origin are refused before the real answer
            body = json.dumps({"state": "nope", "identityToken": "X"}).encode()
            seen["bad_state"] = _http(url + "callback", body, {"Origin": origin, "Content-Type": "application/json"})[0]
            body = json.dumps({"state": page["state"], "identityToken": "X"}).encode()
            seen["no_origin"] = _http(url + "callback", body, {"Content-Type": "application/json"})[0]
            body = json.dumps({"state": page["state"], "identityToken": "IDTOK", "pkce": True, "user": "A User"}).encode()
            seen["ok"] = _http(url + "callback", body, {"Origin": origin, "Content-Type": "application/json"})[0]

        (identity_token, verifier, user), opened = self.run_flow(browser)
        self.assertEqual((identity_token, user), ("IDTOK", "A User"))
        self.assertEqual(sa.pkce_challenge(verifier), seen["challenge"])
        self.assertEqual(seen["base"], BASE)
        self.assertEqual((seen["bad_state"], seen["no_origin"], seen["ok"]), (403, 403, 200))
        self.assertTrue(opened[0].startswith("http://127.0.0.1:"))

    def test_cancel_raises(self):
        def browser(url):
            page = self.page_cfg(url)
            body = json.dumps({"state": page["state"], "cancelled": True}).encode()
            _http(url + "callback", body, {"Origin": url.rstrip("/"), "Content-Type": "application/json"})

        with self.assertRaisesRegex(sa.AuthError, "cancelled"):
            self.run_flow(browser)

    def test_foreign_host_header_is_rejected(self):
        seen = {}

        def browser(url):
            port = url.rsplit(":", 1)[1].strip("/")
            seen["status"] = _http(url, headers={"Host": f"evil.example:{port}"})[0]
            page = self.page_cfg(url)
            body = json.dumps({"state": page["state"], "cancelled": True}).encode()
            _http(url + "callback", body, {"Origin": url.rstrip("/"), "Content-Type": "application/json"})

        with self.assertRaises(sa.AuthError):
            self.run_flow(browser)
        self.assertEqual(seen["status"], 421)


class SecretStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"MSTR_SECRET_STORE": "file", "XDG_CONFIG_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_file_store_round_trip_is_private(self):
        self.assertTrue(sa.secret_set("session:x", "value"))
        self.assertEqual(sa.secret_get("session:x"), "value")
        mode = stat.S_IMODE(os.stat(sa._secrets_file()).st_mode)
        self.assertEqual(mode, 0o600)
        sa.secret_delete("session:x")
        self.assertIsNone(sa.secret_get("session:x"))

    def test_cached_session_restored_only_while_alive(self):
        s = FakeSession()
        s.headers["X-MSTR-AuthToken"] = "TOK"
        s.cookies.set("JSESSIONID", "J", domain="tenant.example.com", path="/")
        sa.save_session(s, BASE, sa.SignIn("sso", False, "A User"))

        alive = FakeSession({("GET", "/api/sessions"): Resp(200, body={})})
        data = sa.load_cached_session(alive, BASE)
        self.assertEqual((data["user"], alive.headers["X-MSTR-AuthToken"]), ("A User", "TOK"))
        self.assertEqual(alive.cookies.get("JSESSIONID"), "J")

        dead = FakeSession({("GET", "/api/sessions"): Resp(401)})
        self.assertIsNone(sa.load_cached_session(dead, BASE))
        self.assertNotIn("X-MSTR-AuthToken", dead.headers)
        self.assertIsNone(sa.secret_get(f"session:{BASE}"))

    def test_reused_password_session_is_cached_and_not_logged_out(self):
        routes = {("POST", "/api/auth/login"): Resp(204, {"X-MSTR-AuthToken": "TOK"}),
                  ("GET", "/api/sessions"): Resp(200, body={})}
        c = sa.AuthConfig.from_env(base=BASE, username="u", password="p", reuse=True)
        first = FakeSession(routes)
        r1 = sa.sign_in(first, c)
        self.assertEqual((r1.method, r1.owns_session), ("password", False))
        second = FakeSession(routes)
        r2 = sa.sign_in(second, c)
        self.assertTrue(r2.method.startswith("cached:"))
        self.assertFalse(any(path == "/api/auth/login" for _, path, _ in second.calls))
        other_user = sa.AuthConfig.from_env(base=BASE, username="someone-else", password="p", reuse=True)
        third = FakeSession(routes)
        self.assertEqual(sa.sign_in(third, other_user).method, "password")   # separate cache slot

    def test_explicit_sso_reuses_a_cached_browser_session(self):
        # MSTR_AUTH_METHOD=sso used to skip the cache: every command asked the browser again (seen 2026-11-18)
        s = FakeSession()
        s.headers["X-MSTR-AuthToken"] = "TOK"
        sa.save_session(s, BASE, sa.SignIn("sso", False, "A User"))
        cfg = sa.AuthConfig.from_env(base=BASE, method="sso")
        alive = FakeSession({("GET", "/api/sessions"): Resp(200, body={})})
        with mock.patch.object(sa, "browser_sso", side_effect=AssertionError("asked the browser")):
            self.assertEqual(sa.sign_in(alive, cfg).method, "cached:sso")
        no_cache = sa.AuthConfig.from_env(base=BASE, method="sso", cache=False)        # a fresh sign-in on request
        with mock.patch.object(sa, "browser_sso", side_effect=sa.AuthError("browser")), self.assertRaises(sa.AuthError):
            sa.sign_in(FakeSession({("GET", "/api/sessions"): Resp(200, body={})}), no_cache)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(sa, "browser_sso", side_effect=sa.AuthError("b")), \
                self.assertRaises(sa.AuthError):
            sa.sign_in(FakeSession({("GET", "/api/sessions"): Resp(401)}), cfg)          # refused: said, then browser
        self.assertIn("the cached session was refused (HTTP 401", err.getvalue())
        self.assertIn("forgotten", err.getvalue())

    def test_methods_says_what_auto_would_really_use(self):
        cfg = sa.AuthConfig.from_env(base=BASE)
        self.assertEqual(sa._auto_would_use(cfg), "sso (no cached session yet)")
        sa.secret_set(f"session:{BASE}", json.dumps({"token": "T"}))
        self.assertTrue(sa._auto_would_use(cfg).startswith("cached"))

    def test_expired_saved_api_token_is_ignored(self):
        sa.secret_set(f"api-token:{BASE}", json.dumps({"apiToken": "OLD", "expireTime": "2000-01-01T00:00:00Z"}))
        self.assertEqual(sa._saved_api_token(BASE), "")
        sa.secret_set(f"api-token:{BASE}", json.dumps({"apiToken": "NEW", "expireTime": "2999-01-01T00:00:00Z"}))
        self.assertEqual(sa._saved_api_token(BASE), "NEW")


class TransportSafetyTests(unittest.TestCase):
    def test_tokens_do_not_follow_a_redirect_to_another_host(self):
        import http.server
        seen = {}

        class Target(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen[self.path] = {k.lower(): v for k, v in self.headers.items()}
                self.send_response(200)
                self.end_headers()

        target = http.server.HTTPServer(("127.0.0.1", 0), Target)
        port = target.server_address[1]

        class Bouncer(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                # off to another host name (localhost != 127.0.0.1 as an origin)
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{port}/other")
                self.end_headers()

        bouncer = http.server.HTTPServer(("127.0.0.1", 0), Bouncer)
        for srv in (target, bouncer):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            s = sa.SafeSession()
            s.headers["X-MSTR-AuthToken"] = "TOKEN"
            with self.assertRaises(sa.CrossOriginRedirect):
                s.get(f"http://127.0.0.1:{bouncer.server_address[1]}/b", timeout=5)
            self.assertNotIn("/other", seen)                      # nothing reached the other origin
            plain = requests.Session()
            plain.headers["X-MSTR-AuthToken"] = "TOKEN"
            plain.get(f"http://127.0.0.1:{bouncer.server_address[1]}/b", timeout=5)
            self.assertIn("x-mstr-authtoken", seen["/other"])   # why SafeSession exists
        finally:
            for srv in (target, bouncer):
                srv.shutdown()
                srv.server_close()

    def test_same_origin_rules(self):
        self.assertTrue(sa._same_origin("https://h/a", "https://h:443/b"))
        self.assertFalse(sa._same_origin("https://h/a", "http://h/b"))       # downgrade
        self.assertFalse(sa._same_origin("https://h/a", "https://evil/b"))
        self.assertFalse(sa._same_origin("https://h/a", "https://h:8443/b"))

    def test_cleartext_and_credential_urls_are_refused(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(sa.AuthError, "plain http"):
                sa.check_base("http://tenant.example.com/MicroStrategyLibrary")
            with self.assertRaisesRegex(sa.AuthError, "credentials in the URL"):
                sa.check_base("https://u:p@tenant.example.com/MicroStrategyLibrary")
            sa.check_base("http://localhost:8080/MicroStrategyLibrary")
        with mock.patch.dict(os.environ, {"MSTR_ALLOW_HTTP": "1"}):
            sa.check_base("http://lab.example/MicroStrategyLibrary")


if __name__ == "__main__":
    unittest.main()
