"""Sign in to a Strategy Library REST API — shared by every script in this directory.

Pick a method with --auth-method or MSTR_AUTH_METHOD (default "auto"):

  password        POST /api/auth/login with MSTR_USER + MSTR_PASSWORD. MSTR_LOGIN_MODE picks
                  1 = Standard (default) or 16 = LDAP; `--auth-method ldap` is a shortcut.
  anonymous       POST /api/auth/login, loginMode 8 (guest), when the tenant enables it.
  api-token       POST /api/auth/login, loginMode 4096, with the API token in `username`
                  (MSTR_API_TOKEN, or one saved by `strategy_auth.py login --save-api-token`).
  sso             Browser single sign-on through whatever the tenant uses (SAML, OIDC, ...).
                  This process serves one page on http://127.0.0.1:<port>. After the user
                  clicks Allow there, the page turns their Library browser session into an
                  identity token bound to a PKCE challenge (POST /api/v2/auth/identityToken)
                  and hands it back; this process redeems it with POST /api/auth/delegate
                  and the PKCE verifier only it holds. No password reaches the command line.
  identity-token  POST /api/auth/delegate with an identity token another application minted
                  (MSTR_DELEGATE_IDENTITY_TOKEN).
  oidc            Native OIDC (login mode 4194304): authorization code + PKCE against the
                  identity provider that GET /api/config/oidc/native names, then
                  POST /api/auth/oidc/token. The provider's native client must allow the
                  redirect http://127.0.0.1:<port>/oauth/callback. Not yet exercised against a
                  live OIDC tenant.

auto = MSTR_API_TOKEN, else MSTR_USER + MSTR_PASSWORD, else MSTR_DELEGATE_IDENTITY_TOKEN,
else a cached browser session for this tenant, else a saved API token, else sso.

Sessions that came from a browser (sso, oidc, identity-token) are cached in the OS secret
store — macOS Keychain, libsecret's secret-tool, else a 0600 file under
~/.config/strategy-automate — so later commands reuse them until they expire. They share the
browser's server session, so scripts never log them out. MSTR_SESSION_CACHE=0 turns caching
off; MSTR_SECRET_STORE=keychain|secret-tool|file|none forces a store. Tokens are never printed.

Every script talks HTTP through SafeSession (below): a default (15 s connect, MSTR_HTTP_TIMEOUT
read, default 300 s) timeout, MSTR_HTTP_RETRIES (default 2) retries of connection failures and of
502/503/504 on reads only, and no redirect is ever followed to another origin. A browser sign-in
is never attempted where nobody can answer it (CI=true, MSTR_NO_BROWSER=1, Linux without a
display): `auto` fails fast there and names the variables to set.

Run this file directly to see what a tenant supports and to sign in once:

  python3 strategy_auth.py methods
  python3 strategy_auth.py login [--method sso] [--save-api-token --lifetime-minutes 480]
  python3 strategy_auth.py status
  python3 strategy_auth.py logout [--forget-api-token]

See memory/reference_strategy_authentication.md.
"""
from __future__ import annotations

import argparse
import base64
import calendar
import contextlib
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from typing import Any

try:
    import requests
except ImportError:  # file-only consumers import the helpers without requests
    requests = None  # type: ignore[assignment]
NETWORK_ERRORS: tuple = (requests.RequestException,) if requests is not None else ()

# EnumDSSXMLAuthModes values the REST API accepts or reports (GET /api/config/authModes).
LOGIN_MODES = {1: "Standard", 8: "Anonymous", 16: "LDAP", 4096: "API token",
               1048576: "SAML", 4194304: "OIDC"}
METHODS = ("auto", "password", "ldap", "anonymous", "api-token", "sso", "identity-token", "oidc")
BROWSER_METHODS = ("sso", "oidc", "identity-token")
SERVICE = "strategy-automate"
DEFAULT_SSO_PORT = 8753
TIMEOUT = 60


class AuthError(RuntimeError):
    """Sign-in failed. The message is safe to print: it never carries a secret."""


TOKEN_HEADERS = ("X-MSTR-AuthToken", "X-MSTR-IdentityToken")


def same_origin(a: str, b: str) -> bool:
    """Same scheme, host and port, and no credentials in either URL."""
    pa, pb = urllib.parse.urlsplit(a), urllib.parse.urlsplit(b)
    if pa.username or pa.password or pb.username or pb.password or not pa.hostname:
        return False
    ports = {"http": 80, "https": 443}
    try:
        return (pa.scheme == pb.scheme and pa.hostname == pb.hostname
                and (pa.port or ports.get(pa.scheme)) == (pb.port or ports.get(pb.scheme)))
    except ValueError:   # a port that is not a number
        return False


_same_origin = same_origin   # older name, kept for callers and tests


RETRY_STATUS = (502, 503, 504)


def http_timeout() -> tuple[float, float]:
    """(connect, read) seconds for calls that don't pass a timeout; MSTR_HTTP_TIMEOUT sets the read part."""
    return (15.0, float(os.environ.get("MSTR_HTTP_TIMEOUT") or 300))


if requests is not None:
    class CrossOriginRedirect(requests.exceptions.InvalidURL):
        """The server redirected a request to another origin; SafeSession refuses to follow."""

    class SafeSession(requests.Session):
        """requests.Session for talking to a Strategy tenant:

        - never follows a redirect to another origin: a 307/308 would resend the body (passwords,
          identity and refresh tokens), and cookies passed as a dict have no domain, so they
          would follow too; requests itself strips only `Authorization`. Same-origin redirects
          are followed as usual;
        - a default (connect, read) timeout on every call (requests otherwise waits forever,
          which can hang a script while it holds a changeset lock);
        - bounded retries only where repeating is safe: connection failures (nothing reached the
          server) for any verb, and 502/503/504 for GET/HEAD/OPTIONS. Read timeouts and writes that
          reached the server are never repeated. `retries` defaults to MSTR_HTTP_RETRIES or 2;
          pass 0 when the caller runs its own retry loop.
        """

        def __init__(self, retries: int | None = None) -> None:
            super().__init__()
            if retries is None:
                retries = int(os.environ.get("MSTR_HTTP_RETRIES") or 2)
            if retries > 0:
                from urllib3.util.retry import Retry
                policy = Retry(total=retries, connect=retries, read=False, status=retries, other=0,
                               allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
                               status_forcelist=RETRY_STATUS, backoff_factor=0.5,
                               raise_on_status=False, respect_retry_after_header=False)
                adapter = requests.adapters.HTTPAdapter(max_retries=policy)
                self.mount("https://", adapter)
                self.mount("http://", adapter)

        def request(self, method, url, *args, **kwargs):  # type: ignore[override]
            if len(args) < 7 and kwargs.get("timeout") is None:   # timeout = 7th argument after url
                kwargs["timeout"] = http_timeout()
            return super().request(method, url, *args, **kwargs)

        def rebuild_auth(self, prepared_request, response):  # type: ignore[override]
            if not same_origin(response.request.url or "", prepared_request.url or ""):
                for name in TOKEN_HEADERS:          # belt and braces: nothing is sent anyway
                    prepared_request.headers.pop(name, None)
                old = urllib.parse.urlsplit(response.request.url or "")
                new = urllib.parse.urlsplit(prepared_request.url or "")
                raise CrossOriginRedirect(
                    f"{response.request.method} {old.scheme}://{old.netloc}{old.path} was redirected to "
                    f"{new.scheme}://{new.netloc}{new.path}; not following it with credentials. If that host "
                    f"is the real tenant, point MSTR_BASE at it.")
            super().rebuild_auth(prepared_request, response)
else:  # pragma: no cover
    SafeSession = None  # type: ignore[assignment,misc]
    CrossOriginRedirect = None  # type: ignore[assignment,misc]


_HOST = re.compile(r"[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*|\[?[0-9a-f:.]+\]?")


def check_base(base: str) -> None:
    """Refuse to send credentials to a cleartext http:// tenant (localhost aside), and refuse
    base URLs that are not plain host names (they end up in headers and the consent page's CSP)."""
    parts = urllib.parse.urlsplit(base)
    try:
        parts.port   # raises on a non-numeric port
    except ValueError:
        raise AuthError("MSTR_BASE has an invalid port") from None
    if (parts.scheme not in ("https", "http") or not parts.hostname or not _HOST.fullmatch(parts.hostname)
            or any(c.isspace() or c in "\"'<>;\\" for c in base)):
        raise AuthError("MSTR_BASE must be the Library URL, e.g. https://<host>/MicroStrategyLibrary")
    if parts.username or parts.password:
        raise AuthError("MSTR_BASE must not carry credentials in the URL")
    if parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1") \
            and os.environ.get("MSTR_ALLOW_HTTP") != "1":
        raise AuthError("MSTR_BASE uses plain http — credentials and tokens would travel in cleartext. "
                        "Use https (MSTR_ALLOW_HTTP=1 only for an isolated lab)")


@dataclass
class AuthConfig:
    base: str
    method: str = "auto"
    username: str = ""
    password: str = ""
    login_mode: int = 1
    api_token: str = ""
    identity_token: str = ""
    cache: bool = True
    reuse: bool = False   # keep password / API-token sessions for the next command (MSTR_REUSE_SESSION=1)
    sso_port: int = DEFAULT_SSO_PORT
    sso_host: str = "127.0.0.1"
    sso_timeout: float = 300.0
    open_browser: bool = True

    @classmethod
    def from_env(cls, **overrides: Any) -> "AuthConfig":
        """MSTR_* environment values, then any non-empty keyword override (CLI flags)."""
        env = os.environ.get
        cfg = cls(
            base=env("MSTR_BASE", ""),
            method=env("MSTR_AUTH_METHOD") or "auto",
            username=env("MSTR_USER", ""),
            password=env("MSTR_PASSWORD", ""),
            login_mode=int(env("MSTR_LOGIN_MODE") or 1),
            api_token=env("MSTR_API_TOKEN", ""),
            identity_token=env("MSTR_DELEGATE_IDENTITY_TOKEN", ""),
            cache=env("MSTR_SESSION_CACHE", "1").lower() not in ("0", "false", "no", "off"),
            reuse=env("MSTR_REUSE_SESSION", "0").lower() in ("1", "true", "yes", "on"),
            sso_port=int(env("MSTR_SSO_PORT") or DEFAULT_SSO_PORT),
            sso_host=env("MSTR_SSO_HOST") or "127.0.0.1",
            sso_timeout=float(env("MSTR_SSO_TIMEOUT") or 300),
        )
        for key, value in overrides.items():
            if value is not None and value != "":
                setattr(cfg, key, value)
        cfg.base = (cfg.base or "").rstrip("/")
        cfg.method = (cfg.method or "auto").lower()
        if cfg.method == "ldap":
            cfg.method, cfg.login_mode = "password", 16
        if cfg.method not in METHODS:
            raise AuthError(f"unknown auth method {cfg.method!r}; choose one of {', '.join(METHODS)}")
        if cfg.sso_host not in ("127.0.0.1", "localhost"):
            raise AuthError("MSTR_SSO_HOST must be 127.0.0.1 or localhost")
        return cfg


@dataclass
class SignIn:
    """What sign_in() did. owns_session=False means the session is shared with a browser or
    cached for later commands, so sign_out() must leave it alone."""
    method: str
    owns_session: bool
    user: str = ""


# ── Public API ───────────────────────────────────────────────────────────────

def _reuse_account(cfg: AuthConfig, method: str) -> str:
    """Cache slot for a reused password / API-token session: one per tenant and identity, so a
    different user or a different API token never picks up someone else's session."""
    if method in ("password", "ldap") and cfg.username:
        who = cfg.username.lower()
    elif method == "api-token":
        token = cfg.api_token or _saved_api_token(cfg.base)
        who = "api-token:" + hashlib.sha256(token.encode()).hexdigest()[:16] if token else method
    else:
        who = method
    return f"session:{cfg.base}|{who}"


def sign_in(session: "requests.Session", cfg: AuthConfig) -> SignIn:
    """Authenticate `session` (sets X-MSTR-AuthToken and the session cookies). Raises AuthError.

    With cfg.reuse (MSTR_REUSE_SESSION=1) a password / API-token session is cached in the OS
    secret store and left open, so the next command — and the report instances, search results
    and jobs it created — continue in the same session. `strategy_auth.py logout` ends it."""
    if cfg.reuse and cfg.cache and cfg.method in ("auto", "password", "ldap", "api-token"):
        method = cfg.method if cfg.method != "auto" else _auto_method(cfg)
        if method in ("password", "ldap", "api-token"):
            cached = load_cached_session(session, cfg.base, account=_reuse_account(cfg, method))
            if cached is not None:
                return SignIn("cached:" + str(cached.get("method", "")), False, str(cached.get("user", "")))
            result = _sign_in(session, cfg)
            if result.owns_session and save_session(session, cfg.base, result,
                                                    account=_reuse_account(cfg, method)):
                result.owns_session = False
            return result
    return _sign_in(session, cfg)


def _sign_in(session: "requests.Session", cfg: AuthConfig) -> SignIn:
    check_base(cfg.base)
    method = cfg.method
    if method == "auto":
        method = _auto_method(cfg)
        if method == "cached":
            cached = load_cached_session(session, cfg.base)
            if cached is not None:
                return SignIn("cached:" + str(cached.get("method", "")), False, str(cached.get("user", "")))
            method = "api-token" if _saved_api_token(cfg.base) else "sso"
        if method == "sso" and not _browser_possible():
            # auto would wait minutes for a browser nobody can open; say what to set instead
            raise AuthError(f"no credentials for {cfg.base}: set MSTR_API_TOKEN (or MSTR_USER + MSTR_PASSWORD), "
                            "or sign in once from a desktop with `strategy_auth.py login`")
    if method == "password":
        if not (cfg.username and cfg.password):
            raise AuthError("password sign-in needs MSTR_USER and MSTR_PASSWORD (or --user/--password); "
                            "for single sign-on use --auth-method sso")
        mode = cfg.login_mode if cfg.login_mode in (1, 16) else 1
        _login(session, cfg.base, {"username": cfg.username, "password": cfg.password, "loginMode": mode})
        return SignIn("ldap" if mode == 16 else "password", True)
    if method == "anonymous":
        _login(session, cfg.base, {"loginMode": 8})
        return SignIn("anonymous", True)
    if method == "api-token":
        # MSTR_LOGIN_MODE=4096 with the token in MSTR_USER is the older way to pass one
        token = cfg.api_token or (cfg.username if cfg.login_mode == 4096 else "") or _saved_api_token(cfg.base)
        if not token:
            raise AuthError("api-token sign-in needs MSTR_API_TOKEN or a token saved with "
                            "`strategy_auth.py login --save-api-token`")
        _login(session, cfg.base, {"loginMode": 4096, "username": token})
        return SignIn("api-token", True)
    if method == "identity-token":
        if not cfg.identity_token:
            raise AuthError("identity-token sign-in needs MSTR_DELEGATE_IDENTITY_TOKEN")
        _delegate(session, cfg.base, cfg.identity_token, None)
        return _finish_browser_sign_in(session, cfg, SignIn("identity-token", False))
    if method == "sso":
        # a cached browser session is as good as a new one: reuse it rather than ask the browser for every command
        # (an explicit --auth-method sso / MSTR_AUTH_METHOD=sso skipped the cache before; MSTR_SESSION_CACHE=0 still
        # forces a fresh sign-in)
        cached = load_cached_session(session, cfg.base) if cfg.cache else None
        if cached is not None and str(cached.get("method", "")) in BROWSER_METHODS:
            return SignIn("cached:" + str(cached.get("method", "")), False, str(cached.get("user", "")))
        identity_token, verifier, user = browser_sso(cfg)
        _delegate(session, cfg.base, identity_token, verifier)
        return _finish_browser_sign_in(session, cfg, SignIn("sso", False, user))
    if method == "oidc":
        oidc_native_sign_in(session, cfg)
        return _finish_browser_sign_in(session, cfg, SignIn("oidc", False))
    raise AuthError(f"unsupported auth method {method!r}")


def sign_out(session: "requests.Session", base: str, signin: SignIn | None) -> None:
    """End a session this process created. POST /api/auth/logout is the only logout the REST
    API has; shared, borrowed and cached sessions are left running on purpose."""
    if signin is None or not signin.owns_session or "X-MSTR-AuthToken" not in session.headers:
        return
    try:
        session.post(f"{base.rstrip('/')}/api/auth/logout", timeout=20)
    except Exception:  # best effort: the session times out on its own
        pass
    session.headers.pop("X-MSTR-AuthToken", None)


def whoami(session: "requests.Session", base: str) -> str:
    """Display name of the signed-in user, or '' when it can't be read."""
    try:
        r = session.get(f"{base.rstrip('/')}/api/sessions/userInfo", timeout=30)
        if r.ok:
            info = r.json() or {}
            return str(info.get("fullName") or info.get("username") or "")
    except Exception:
        pass
    return ""


def add_auth_method_arg(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--auth-method", default=os.environ.get("MSTR_AUTH_METHOD", "auto"),
                        choices=METHODS,
                        help="how to sign in (env MSTR_AUTH_METHOD): auto (default), password, ldap, "
                             "anonymous, api-token (MSTR_API_TOKEN), sso (browser single sign-on), "
                             "identity-token, oidc. See strategy_auth.py.")
    return parser


# ── REST calls ───────────────────────────────────────────────────────────────

def describe(resp: Any) -> str:
    """'HTTP 401 ERR009 <message>' from a Strategy error response; never echoes request data."""
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        parts = [str(body.get(k)) for k in ("code", "iServerCode", "message") if body.get(k) not in (None, "")]
        return f"HTTP {resp.status_code} " + " ".join(parts)[:240]
    return f"HTTP {resp.status_code}"


def _login(session: "requests.Session", base: str, body: dict) -> None:
    r = session.post(f"{base}/api/auth/login", json=body, timeout=TIMEOUT)
    token = r.headers.get("X-MSTR-AuthToken")
    if r.status_code not in (200, 204) or not token:
        mode = int(body.get("loginMode") or 0)
        raise AuthError(f"sign-in with loginMode {mode} ({LOGIN_MODES.get(mode, '?')}) failed: {describe(r)}")
    session.headers["X-MSTR-AuthToken"] = token


def _delegate(session: "requests.Session", base: str, identity_token: str, code_verifier: str | None) -> None:
    body: dict[str, Any] = {"loginMode": -1, "identityToken": identity_token}
    if code_verifier:
        body["codeVerifier"] = code_verifier
    r = session.post(f"{base}/api/auth/delegate", json=body, timeout=TIMEOUT)
    token = r.headers.get("X-MSTR-AuthToken")
    if r.status_code not in (200, 204) or not token:
        raise AuthError(f"identity-token sign-in (POST /api/auth/delegate) failed: {describe(r)}")
    session.headers["X-MSTR-AuthToken"] = token


def _browser_possible() -> bool:
    """False where a browser sign-in cannot finish: CI runners, MSTR_NO_BROWSER=1, and Linux
    without a display (the loopback page is reachable from this machine only)."""
    if os.environ.get("CI") or os.environ.get("MSTR_NO_BROWSER") == "1":
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return True


def _auto_method(cfg: AuthConfig) -> str:
    if cfg.api_token or cfg.login_mode == 4096:
        return "api-token"
    if cfg.login_mode == 8:
        return "anonymous"
    if cfg.username and cfg.password:
        return "password"
    if cfg.identity_token:
        return "identity-token"
    return "cached" if cfg.cache else ("api-token" if _saved_api_token(cfg.base) else "sso")


def _auto_would_use(cfg: AuthConfig) -> str:
    """What `auto` would do on this machine now: "cached" only when a session is cached (it is checked with the
    server at sign-in), else what it falls back to."""
    method = _auto_method(cfg)
    if method != "cached":
        return method
    if secret_get(f"session:{cfg.base}"):
        return "cached (checked with the server at sign-in; sso if refused)"
    return "api-token (saved)" if _saved_api_token(cfg.base) else "sso (no cached session yet)"


def _finish_browser_sign_in(session: "requests.Session", cfg: AuthConfig, result: SignIn) -> SignIn:
    if not result.user:
        result.user = whoami(session, cfg.base)
    if cfg.cache:
        save_session(session, cfg.base, result)
    return result


# ── PKCE + loopback page ─────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_challenge(verifier: str) -> str:
    """RFC 7636 S256 code challenge."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    return verifier, pkce_challenge(verifier)


class _Loopback(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, state: str, page: bytes | None, csp: str):
        super().__init__(("127.0.0.1", port), _LoopbackHandler)
        self.state, self.page, self.csp = state, page, csp
        self.result: dict | None = None
        self.done = threading.Event()
        actual = self.server_address[1]
        self.port = actual
        self.allowed_hosts = {f"127.0.0.1:{actual}", f"localhost:{actual}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}


class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
    server: _Loopback
    server_version = "strategy-automate"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # keep stderr clean
        pass

    def _reply(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
               extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _finish(self, result: dict, code: int, body: bytes, ctype: str) -> None:
        self.server.result = result
        self._reply(code, body, ctype)
        self.server.done.set()

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self.headers.get("Host", "") not in self.server.allowed_hosts:  # DNS-rebinding guard
            return self._reply(421)
        path, _, query = self.path.partition("?")
        if path == "/" and self.server.page is not None:
            return self._reply(200, self.server.page, "text/html; charset=utf-8",
                               {"Content-Security-Policy": self.server.csp, "X-Frame-Options": "DENY"})
        if path == "/oauth/callback" and self.server.page is None:
            params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
            if not hmac.compare_digest(params.get("state", ""), self.server.state):
                return self._reply(400, b"This sign-in link is stale. Start the sign-in again.")
            text = ("Sign-in failed. Return to the terminal for details." if "error" in params
                    else "Signed in. You can close this tab and return to the terminal.")
            return self._finish(params, 200, _message_page(text), "text/html; charset=utf-8")
        return self._reply(404)

    def do_POST(self) -> None:  # noqa: N802
        if (self.headers.get("Host", "") not in self.server.allowed_hosts or self.path != "/callback"
                or self.server.page is None):
            return self._reply(404)
        if self.headers.get("Origin") not in self.server.allowed_origins:
            return self._reply(403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= 16384:
            return self._reply(413)
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            return self._reply(400)
        if not isinstance(data, dict) or not hmac.compare_digest(str(data.get("state", "")), self.server.state):
            return self._reply(403)
        self._finish(data, 200, b'{"ok":true}', "application/json")


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _bind_loopback(cfg: AuthConfig, state: str, page: bytes | None, csp: str) -> _Loopback:
    try:
        return _Loopback(cfg.sso_port, state, page, csp)
    except OSError:
        server = _Loopback(0, state, page, csp)
        print(f"[auth] port {cfg.sso_port} is busy; using {server.port}. If the tenant's CORS allowlist "
              f"names http://{cfg.sso_host}:{cfg.sso_port} only, free that port or set MSTR_SSO_PORT.",
              file=sys.stderr)
        return server


def _run_loopback(cfg: AuthConfig, server: _Loopback, url: str, what: str) -> dict:
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    worker.start()
    try:
        print(f"[auth] {what}: {url}\n[auth] waiting up to {int(cfg.sso_timeout)} s for the browser…",
              file=sys.stderr)
        if cfg.open_browser:
            browser = os.environ.get("MSTR_SSO_BROWSER")
            try:
                (webbrowser.get(browser) if browser else webbrowser).open(url)
            except webbrowser.Error:
                print("[auth] could not open a browser; open the URL above yourself.", file=sys.stderr)
        if not server.done.wait(cfg.sso_timeout):
            raise AuthError(f"no answer from the browser within {int(cfg.sso_timeout)} s (MSTR_SSO_TIMEOUT)")
        return server.result or {}
    finally:
        server.shutdown()
        server.server_close()


def browser_sso(cfg: AuthConfig) -> tuple[str, str | None, str]:
    """Run the consent page; return (identity token, PKCE verifier or None, user display name)."""
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(16)
    page = _sso_page(cfg.base, challenge, state, nonce, cfg.sso_timeout)
    csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
           f"connect-src 'self' {_origin(cfg.base)}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    server = _bind_loopback(cfg, state, page, csp)
    url = f"http://{cfg.sso_host}:{server.port}/"
    result = _run_loopback(cfg, server, url, "sign in with your browser")
    if result.get("cancelled"):
        raise AuthError("sign-in was cancelled in the browser")
    identity_token = result.get("identityToken")
    if not isinstance(identity_token, str) or not identity_token:
        raise AuthError("the browser did not return an identity token")
    pkce = result.get("pkce", True) is not False
    if not pkce:
        print("[auth] this Library has no PKCE-bound identity tokens (POST /api/v2/auth/identityToken); "
              "used the plain identity token instead.", file=sys.stderr)
    return identity_token, (verifier if pkce else None), str(result.get("user") or "")


def oidc_native_sign_in(session: "requests.Session", cfg: AuthConfig) -> None:
    """Login mode 4194304: authorization code + PKCE with the tenant's identity provider, then
    POST /api/auth/oidc/token. Raises AuthError when the tenant has no native OIDC client."""
    r = session.get(f"{cfg.base}/api/config/oidc/native", timeout=TIMEOUT)
    if not r.ok:
        raise AuthError(f"OIDC sign-in is not available on this tenant ({describe(r)}); "
                        "use --auth-method sso instead")
    iams = (r.json() or {}).get("iams") or []
    iam = next((i for i in iams if i.get("default")), iams[0] if iams else None)
    if not iam or not iam.get("issuer"):
        raise AuthError("the tenant returned no OIDC configuration for native clients")
    client_id = iam.get("nativeClientId") or iam.get("clientId")
    idp = SafeSession()          # no tenant headers or cookies go to the identity provider
    idp.verify = session.verify
    try:
        meta = idp.get(iam["issuer"].rstrip("/") + "/.well-known/openid-configuration", timeout=TIMEOUT).json()
    except ValueError:
        raise AuthError("the identity provider's OpenID configuration is not JSON") from None
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    server = _bind_loopback(cfg, state, None, "")
    redirect = f"http://{cfg.sso_host}:{server.port}/oauth/callback"
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
        "scope": " ".join(iam.get("scopes") or ["openid", "profile", "email"]),
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})
    result = _run_loopback(cfg, server, f"{meta['authorization_endpoint']}?{query}",
                           "sign in with your identity provider")
    if "error" in result or not result.get("code"):
        raise AuthError(f"the identity provider refused sign-in: {result.get('error', 'no code')} "
                        f"{result.get('error_description', '')[:200]}")
    tok = idp.post(meta["token_endpoint"], timeout=TIMEOUT, data={
        "grant_type": "authorization_code", "code": result["code"], "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier})
    idp.close()
    if not tok.ok:
        raise AuthError(f"the identity provider's token endpoint answered HTTP {tok.status_code}")
    tokens = tok.json()
    body = {k: tokens[k] for k in ("access_token", "id_token", "refresh_token", "token_type", "expires_in", "scope")
            if k in tokens}
    r = session.post(f"{cfg.base}/api/auth/oidc/token", json=body, timeout=TIMEOUT,
                     params={"clientId": client_id} if client_id else None)
    token = r.headers.get("X-MSTR-AuthToken")
    if r.status_code not in (200, 204) or not token:
        raise AuthError(f"OIDC token sign-in (POST /api/auth/oidc/token) failed: {describe(r)}")
    session.headers["X-MSTR-AuthToken"] = token


def _message_page(text: str) -> bytes:
    safe = text.replace("&", "&amp;").replace("<", "&lt;")
    return (f'<!doctype html><meta charset="utf-8"><title>strategy-automate sign-in</title>'
            f'<body style="font:16px system-ui;margin:3rem">{safe}</body>').encode("utf-8")


def _sso_page(base: str, challenge: str, state: str, nonce: str, timeout_s: float) -> bytes:
    cfg = json.dumps({"base": base, "challenge": challenge, "state": state,
                      "timeoutMs": int(timeout_s * 1000)}).replace("</", "<\\/")
    return (_SSO_PAGE.replace("__NONCE__", nonce).replace("__CFG__", cfg)).encode("utf-8")


# One page, four states. The page never stores or shows the Strategy token; it trades the
# browser's Library session for a PKCE-bound identity token only after the user clicks Allow.
_SSO_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Strategy sign-in · command line</title>
<style nonce="__NONCE__">
:root{color-scheme:light dark;--bg:#f5f6f8;--card:#fff;--fg:#1c2230;--muted:#5b6474;--line:#d8dce3;--accent:#d9480f}
@media (prefers-color-scheme:dark){:root{--bg:#121418;--card:#1b1e24;--fg:#e7e9ee;--muted:#a3aab6;--line:#323741;--accent:#f06b2d}}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{width:min(460px,calc(100vw - 32px));background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}
h1{font-size:19px;margin:0 0 4px}p{margin:10px 0;color:var(--muted)}#msg{color:var(--fg)}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
button{font:inherit;padding:9px 16px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
small{color:var(--muted)}code{font-size:13px;word-break:break-all}[hidden]{display:none!important}
</style></head><body><main>
<h1>Sign in for the command line</h1>
<p id="msg" role="status" aria-live="polite">Checking your Strategy session…</p>
<div class="row">
<button id="signin" class="primary" type="button" hidden>Sign in with SSO</button>
<button id="allow" class="primary" type="button" hidden>Allow</button>
<button id="cancel" type="button" hidden>Cancel</button>
</div>
<p><small>Tenant: <code id="tenant"></code><br>Allow hands the strategy-automate command-line tools on
this computer a session for your account. They never see your password; they receive a one-time code
that works only with a secret they already hold.</small></p>
</main>
<script nonce="__NONCE__" type="application/json" id="cfg">__CFG__</script>
<script nonce="__NONCE__">
const CFG = JSON.parse(document.getElementById('cfg').textContent);
const $ = (id) => document.getElementById(id);
$('tenant').textContent = CFG.base;
let token = null, user = '', timer = null, lastProbe = '';
function show(msg, buttons) {
  $('msg').textContent = msg;
  for (const b of ['signin', 'allow', 'cancel']) $(b).hidden = !buttons.includes(b);
}
async function probe() {
  const r = await fetch(CFG.base + '/api/auth/token', {credentials: 'include', cache: 'no-store'});
  lastProbe = 'GET /api/auth/token answered HTTP ' + r.status;
  if (r.status === 401 || r.status === 403) return null;
  if (!r.ok) throw new Error('Strategy answered HTTP ' + r.status + ' while checking your session.');
  const t = r.headers.get('X-MSTR-AuthToken');
  if (!t) throw new Error('Signed in, but the tenant does not expose X-MSTR-AuthToken to this page '
    + '(CORS Access-Control-Expose-Headers). Ask the Strategy admin to allow ' + location.origin + '.');
  return t;
}
async function ready(t) {
  token = t;
  try {
    const r = await fetch(CFG.base + '/api/sessions/userInfo', {credentials: 'include', headers: {'X-MSTR-AuthToken': t}});
    if (r.ok) { const u = await r.json(); user = u.fullName || u.username || ''; }
  } catch (e) { /* the name is cosmetic */ }
  show('Signed in to Strategy' + (user ? ' as ' + user : '') + '. Allow the command-line tools on this '
    + 'computer to use this session?', ['allow', 'cancel']);
  $('allow').focus();
}
async function answer(body) {
  const r = await fetch('/callback', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({state: CFG.state}, body))});
  if (!r.ok) throw new Error('The command line did not accept the answer (HTTP ' + r.status + '). Start again from the terminal.');
}
$('allow').addEventListener('click', async () => {
  show('Handing the session to the command line…', []);
  try {
    let r = await fetch(CFG.base + '/api/v2/auth/identityToken', {method: 'POST', credentials: 'include',
      headers: {'X-MSTR-AuthToken': token, 'Content-Type': 'application/json'},
      body: JSON.stringify({codeChallenge: CFG.challenge, codeChallengeMethod: 'S256'})});
    let identityToken = null, pkce = true;
    if (r.ok) identityToken = (await r.json()).identityToken;
    else if (r.status === 404 || r.status === 405) {
      r = await fetch(CFG.base + '/api/auth/identityToken', {method: 'POST', credentials: 'include',
        headers: {'X-MSTR-AuthToken': token}});
      if (r.ok) { identityToken = r.headers.get('X-MSTR-IdentityToken'); pkce = false; }
    }
    if (!identityToken) throw new Error('Strategy could not create an identity token (HTTP ' + r.status + ').');
    await answer({identityToken, pkce, user});
    token = null;
    show('Done. The command line is signed in' + (user ? ' as ' + user : '') + '. You can close this tab.', []);
  } catch (e) { show(String(e.message || e), ['allow', 'cancel']); }
});
$('cancel').addEventListener('click', async () => {
  clearTimeout(timer); token = null;
  try { await answer({cancelled: true}); } catch (e) { /* the terminal times out on its own */ }
  show('Cancelled. You can close this tab.', []);
});
$('signin').addEventListener('click', () => {
  const w = window.open(CFG.base + '/app', 'strategy-automate-sso', 'popup=yes,width=540,height=700');
  show(w ? 'Finish signing in in the Strategy window. This page continues on its own.'
         : 'Your browser blocked the sign-in window. Open ' + CFG.base + '/app in a new tab and sign in; '
           + 'this page continues on its own.', ['cancel']);
  const started = Date.now();
  const tick = async () => {
    let t = null;
    try { t = await probe(); } catch (e) { lastProbe = String(e.message || e); }
    if (t) { try { if (w) w.close(); } catch (e) { /* COOP may sever the handle */ } window.focus(); return ready(t); }
    if (Date.now() - started > CFG.timeoutMs) return show('Sign-in timed out. Try again.', ['signin', 'cancel']);
    if (Date.now() - started > 15000)   // say what Strategy answers instead of waiting silently
      show('Still not signed in as far as this page can tell (' + lastProbe + '). If you are signed in in '
           + 'another tab, reload this page.', ['signin', 'cancel']);
    timer = setTimeout(tick, 1500);
  };
  timer = setTimeout(tick, 1000);
});
(async () => {
  try {
    const t = await probe();
    if (t) ready(t); else show("Sign in with your organization's single sign-on to continue. (" + lastProbe + ')',
                               ['signin', 'cancel']);
  } catch (e) { show(String(e.message || e), ['cancel']); }
})();
</script></body></html>
"""


# ── Secret store (session cache + saved API token) ───────────────────────────

def _store_kind() -> str:
    forced = os.environ.get("MSTR_SECRET_STORE", "").lower()
    if forced in ("keychain", "secret-tool", "file", "none"):
        return forced
    if sys.platform == "darwin" and shutil.which("security"):
        return "keychain"
    if shutil.which("secret-tool") and os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return "secret-tool"
    return "file"


def _secrets_file() -> str:
    root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(root, SERVICE, "secrets.json")


def _read_file_store() -> dict:
    try:
        with open(_secrets_file(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_file_store(data: dict) -> None:
    path = _secrets_file()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".secrets-", suffix=".tmp")  # 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


@contextlib.contextmanager
def _file_store_lock():
    """Exclusive lock around a read-modify-write of the file store (POSIX; no-op elsewhere)."""
    path = _secrets_file() + ".lock"
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows
            pass
        yield
    finally:
        os.close(fd)   # closing releases the lock


def secret_get(account: str) -> str | None:
    kind = _store_kind()
    try:
        if kind == "keychain":
            r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode != 0 or not r.stdout.strip():
                return None
            return base64.b64decode(r.stdout.strip()).decode("utf-8")
        if kind == "secret-tool":
            r = subprocess.run(["secret-tool", "lookup", "service", SERVICE, "account", account],
                               capture_output=True, text=True, timeout=20)
            return r.stdout if r.returncode == 0 and r.stdout else None
        if kind == "file":
            value = _read_file_store().get(account)
            return value if isinstance(value, str) else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


def secret_set(account: str, value: str) -> bool:
    """Store a secret without putting it on any command line. Returns False when not stored."""
    kind = _store_kind()
    try:
        if kind == "keychain":
            if any(ch in account for ch in '"\\\n'):
                return False
            encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
            subprocess.run(["security", "-i"], input=f'add-generic-password -U -s "{SERVICE}" -a "{account}" '
                           f'-w {encoded}\n', capture_output=True, text=True, timeout=20)
            return secret_get(account) == value
        if kind == "secret-tool":
            r = subprocess.run(["secret-tool", "store", f"--label={SERVICE} {account}", "service", SERVICE,
                                "account", account], input=value, capture_output=True, text=True, timeout=20)
            return r.returncode == 0
        if kind == "file":
            with _file_store_lock():
                data = _read_file_store()
                data[account] = value
                _write_file_store(data)
            return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def secret_delete(account: str) -> None:
    kind = _store_kind()
    try:
        if kind == "keychain":
            subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                           capture_output=True, timeout=20)
        elif kind == "secret-tool":
            subprocess.run(["secret-tool", "clear", "service", SERVICE, "account", account],
                           capture_output=True, timeout=20)
        elif kind == "file":
            with _file_store_lock():
                data = _read_file_store()
                if data.pop(account, None) is not None:
                    _write_file_store(data)
    except (OSError, subprocess.SubprocessError):
        pass


def save_session(session: "requests.Session", base: str, signin: SignIn, account: str = "") -> bool:
    data = {"v": 1, "method": signin.method, "user": signin.user, "saved": int(time.time()),
            "token": session.headers.get("X-MSTR-AuthToken", ""),
            "cookies": [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                        for c in session.cookies]}
    stored = secret_set(account or f"session:{base.rstrip('/')}", json.dumps(data))
    if not stored and _store_kind() != "none":
        print("[auth] could not cache the session; the next command will ask the browser again.", file=sys.stderr)
    return stored


def load_cached_session(session: "requests.Session", base: str, account: str = "") -> dict | None:
    """Restore a cached session into `session` when the server still accepts it."""
    account = account or f"session:{base.rstrip('/')}"
    raw = secret_get(account)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        token = data["token"]
        cookies = data.get("cookies") or []
    except (ValueError, KeyError, TypeError):
        secret_delete(account)
        return None
    session.headers["X-MSTR-AuthToken"] = token
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
    try:
        status = session.get(f"{base.rstrip('/')}/api/sessions", timeout=30).status_code
    except Exception:
        status = 0
    if status == 200:
        return data
    session.headers.pop("X-MSTR-AuthToken", None)
    session.cookies.clear()
    if status in (401, 403):   # expired or ended; a 5xx or network blip keeps the cache for next time
        secret_delete(account)
    saved = data.get("saved") if isinstance(data, dict) else None
    age = f", saved {int((time.time() - saved) // 60)} min ago" if isinstance(saved, (int, float)) else ""
    print(f"[auth] the cached session was refused (HTTP {status or 'no answer'}{age})"
          f"{'; forgotten' if status in (401, 403) else '; kept for next time'}. Signing in again.", file=sys.stderr)
    return None


def _epoch(stamp: str) -> float | None:
    """Seconds since the epoch for an ISO 8601 time such as 2026-10-06T01:00:00.000+0000."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$", stamp or "")
    if not m:
        return None
    year, month, day, hour, minute, sec = (int(g) for g in m.groups()[:6])
    offset = 0
    zone = m.group(7)
    if zone and zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        offset = sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return calendar.timegm((year, month, day, hour, minute, sec, 0, 0, 0)) - offset


def _saved_api_token(base: str) -> str:
    """The saved API token for this tenant, or '' when none is saved or it has expired."""
    raw = secret_get(f"api-token:{base.rstrip('/')}")
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    expires = _epoch(str(data.get("expireTime") or ""))
    if expires is not None and expires <= time.time() + 60:
        return ""
    return str(data.get("apiToken") or "")


# ── CLI ──────────────────────────────────────────────────────────────────────

def _session() -> "requests.Session":
    if requests is None:
        raise SystemExit("requests is required (pip install requests)")
    s = SafeSession()
    s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
    return s


def cmd_methods(cfg: AuthConfig) -> dict:
    """What the tenant offers and what this machine can use — no credentials needed."""
    with SafeSession() as s:
        return _methods(cfg, s)


def _methods(cfg: AuthConfig, s: "requests.Session") -> dict:
    out: dict[str, Any] = {"base": cfg.base, "warnings": []}
    r = s.get(f"{cfg.base}/api/config/authModes", timeout=30)
    if r.status_code == 404:
        raise AuthError(f"{cfg.base}/api/config/authModes answered 404; check MSTR_BASE is the Library URL "
                        "(…/MicroStrategyLibrary)")
    try:
        modes = (r.json() or {}).get("modes", []) if r.ok else []
    except ValueError:
        raise AuthError(f"{cfg.base} did not answer like a Strategy Library (GET /api/config/authModes: "
                        f"HTTP {r.status_code}, not JSON); check MSTR_BASE ends in /MicroStrategyLibrary") from None
    out["tenant_login_modes"] = [f"{m} {LOGIN_MODES.get(m, '(unknown)')}" for m in modes]
    origin = f"http://{cfg.sso_host}:{cfg.sso_port}"
    pre = s.options(f"{cfg.base}/api/v2/auth/identityToken", timeout=30, headers={
        "Origin": origin, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-mstr-authtoken,content-type"})
    tok = s.get(f"{cfg.base}/api/auth/token", headers={"Origin": origin}, timeout=30)
    out["sso_browser_handoff"] = {
        "page_origin": origin,
        "cors_allows_origin": pre.headers.get("Access-Control-Allow-Origin") in (origin, "*"),
        "cors_allows_credentials": pre.headers.get("Access-Control-Allow-Credentials") == "true",
        "exposes_X-MSTR-AuthToken": "x-mstr-authtoken" in tok.headers.get("Access-Control-Expose-Headers", "").lower(),
    }
    stranger = "https://cors-check.invalid"
    odd = s.options(f"{cfg.base}/api/auth/token", timeout=30, headers={
        "Origin": stranger, "Access-Control-Request-Method": "GET"})
    if (odd.headers.get("Access-Control-Allow-Origin") == stranger
            and odd.headers.get("Access-Control-Allow-Credentials") == "true"):
        out["warnings"].append("The Library echoes ANY Origin with Access-Control-Allow-Credentials: true. "
                               "Any website a signed-in user visits can then read their session token. "
                               "Ask the Strategy admin to restrict the CORS allowlist to known origins.")
    o = s.get(f"{cfg.base}/api/config/oidc/native", timeout=30)
    out["oidc_native"] = "available" if o.ok else f"not available ({describe(o)})"
    root = _origin(cfg.base)
    pr = s.get(f"{root}/.well-known/oauth-protected-resource", timeout=30)
    if pr.ok and "json" in pr.headers.get("Content-Type", ""):
        meta = pr.json()
        out["mcp_oauth"] = {"resource": meta.get("resource"), "authorization_servers": meta.get("authorization_servers")}
    out["this_machine"] = {
        "http_stack": http_stack(out["warnings"]),
        "MSTR_USER+MSTR_PASSWORD": bool(cfg.username and cfg.password),
        "MSTR_API_TOKEN": bool(cfg.api_token),
        "saved_api_token": bool(_saved_api_token(cfg.base)),
        "cached_session": bool(secret_get(f"session:{cfg.base}")),
        "secret_store": _store_kind(),
        "configured_method": cfg.method,
        "auto_would_use": _auto_would_use(cfg),
    }
    return out


# Oldest versions without known advisories that matter here (see tests/tools/osv_audit.py).
HTTP_FLOORS = {"requests": (2, 32, 5), "urllib3": (2, 6, 3)}


def _version(text: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def http_stack(warnings: list) -> dict:
    """Versions of the pieces every request goes through; warns when one is below its floor."""
    import ssl
    import urllib3
    found = {"python": sys.version.split()[0], "requests": requests.__version__,
             "urllib3": urllib3.__version__, "tls": ssl.OPENSSL_VERSION}
    old = [f"{name} {found[name]}" for name, floor in HTTP_FLOORS.items() if _version(found[name]) < floor]
    if old:
        warnings.append(f"Outdated HTTP libraries in this Python: {', '.join(old)} (known security fixes "
                        "missing). Use the locked versions (`uv sync`, then `uv run python3 ...`) or upgrade them "
                        "in a virtual environment.")
    return found


def cmd_login(cfg: AuthConfig, save_api_token: bool, lifetime: int | None, replace: bool) -> dict:
    with _session() as s:
        return _login_cmd(cfg, s, save_api_token, lifetime, replace)


def _login_cmd(cfg: AuthConfig, s: "requests.Session", save_api_token: bool, lifetime: int | None,
               replace: bool) -> dict:
    result = sign_in(s, cfg)
    out: dict[str, Any] = {"ok": True, "base": cfg.base, "method": result.method,
                           "user": result.user or whoami(s, cfg.base),
                           "session_cached": result.method in BROWSER_METHODS and cfg.cache}
    if save_api_token:
        meta = s.get(f"{cfg.base}/api/auth/apiTokens", timeout=30)
        try:
            has_token = meta.status_code == 200 and bool(meta.json())
        except ValueError:
            has_token = False
        if has_token and not replace:
            out["api_token"] = ("not created: this user already has an API token, and creating one replaces it "
                                "(ending sessions that use it). Re-run with --replace-api-token to do that.")
        else:
            body = {"lifeTimeInMinutes": lifetime} if lifetime else {}
            r = s.post(f"{cfg.base}/api/auth/apiTokens", json=body, timeout=30)
            if r.status_code in (200, 201) and (r.json() or {}).get("apiToken"):
                data = r.json()
                ok = secret_set(f"api-token:{cfg.base}", json.dumps(
                    {"apiToken": data["apiToken"], "expireTime": data.get("expireTime", "")}))
                out["api_token"] = (f"saved to the {_store_kind()} store; expires {data.get('expireTime') or 'per tenant policy'}"
                                    if ok else "created but could not be stored; revoke it with logout --forget-api-token")
            else:
                out["api_token"] = f"not created: {describe(r)}"
    sign_out(s, cfg.base, result)
    return out


def cmd_status(cfg: AuthConfig) -> dict:
    out: dict[str, Any] = {"base": cfg.base, "secret_store": _store_kind()}
    with _session() as s:
        cached = load_cached_session(s, cfg.base)
    out["cached_session"] = ({"valid": True, "method": cached.get("method"), "user": cached.get("user"),
                              "saved": time.strftime("%Y-%m-%d %H:%M", time.localtime(cached.get("saved", 0)))}
                             if cached else {"valid": False})
    raw = secret_get(f"api-token:{cfg.base}")
    try:
        token_meta = json.loads(raw) if raw else None
    except ValueError:
        token_meta = None
    out["saved_api_token"] = ({"expires": token_meta.get("expireTime") or "unknown",
                               "usable": bool(_saved_api_token(cfg.base))} if token_meta else None)
    return out


def cmd_logout(cfg: AuthConfig, forget_api_token: bool) -> dict:
    out: dict[str, Any] = {"base": cfg.base}
    out["session"] = ("ended (this also ends the browser session it was handed from)"
                      if _end_cached(cfg.base, f"session:{cfg.base}") else "none cached")
    reused = [method for method in ("password", "api-token")   # sessions kept by MSTR_REUSE_SESSION=1
              if _end_cached(cfg.base, _reuse_account(cfg, method))]
    if _end_cached(cfg.base, f"session:{cfg.base}|api-token"):   # slot name used before 2026-10
        reused.append("api-token")
    if reused:
        out["reused_sessions_ended"] = sorted(set(reused))
    if forget_api_token:
        token = _saved_api_token(cfg.base)
        if token:
            with _session() as t:
                try:
                    _login(t, cfg.base, {"loginMode": 4096, "username": token})
                    r = t.delete(f"{cfg.base}/api/auth/apiTokens", timeout=30)
                    out["api_token"] = "revoked" if r.status_code in (200, 204) else f"not revoked: {describe(r)}"
                    sign_out(t, cfg.base, SignIn("api-token", True))   # the revocation's own session
                except AuthError as e:
                    out["api_token"] = f"not revoked: {e}"
        secret_delete(f"api-token:{cfg.base}")
        out.setdefault("api_token", "forgotten")
    return out


def _end_cached(base: str, account: str) -> bool:
    """Log out and forget the session cached under `account`; True if one was live."""
    with _session() as s:
        live = load_cached_session(s, base, account=account) is not None
        if live:
            with contextlib.suppress(Exception):
                s.post(f"{base}/api/auth/logout", timeout=20)
    secret_delete(account)
    return live


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="", help="Library URL (env MSTR_BASE)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("methods", help="show the tenant's sign-in options and what this machine can use")
    lp = sub.add_parser("login", help="sign in once; browser sessions are cached for later commands")
    lp.add_argument("--method", choices=METHODS, default=None, help="default: MSTR_AUTH_METHOD or auto")
    lp.add_argument("--save-api-token", action="store_true",
                    help="also create an API token for this user and keep it in the secret store, so later "
                         "commands sign in with loginMode 4096 without the browser")
    lp.add_argument("--lifetime-minutes", type=int, default=None, help="API token lifetime (tenant default otherwise)")
    lp.add_argument("--replace-api-token", action="store_true",
                    help="allow replacing the user's existing API token (it can only have one)")
    sub.add_parser("status", help="show the cached session and saved API token (never their values)")
    op = sub.add_parser("logout", help="end the cached session and forget it")
    op.add_argument("--forget-api-token", action="store_true", help="also revoke and forget the saved API token")
    args = ap.parse_args(argv)
    try:
        cfg = AuthConfig.from_env(base=args.base, method=getattr(args, "method", None))
        if not cfg.base:
            raise AuthError("set MSTR_BASE or pass --base")
        if args.cmd == "methods":
            out = cmd_methods(cfg)
        elif args.cmd == "login":
            out = cmd_login(cfg, args.save_api_token, args.lifetime_minutes, args.replace_api_token)
        elif args.cmd == "status":
            out = cmd_status(cfg)
        else:
            out = cmd_logout(cfg, args.forget_api_token)
    except AuthError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2
    except NETWORK_ERRORS as e:
        print(f"FATAL: network error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
