#!/usr/bin/env python3
"""
build_mosaic.py — discovery + build CLI for Strategy Mosaic semantic models.

Subcommands:
  auth-probe                 Confirm login + identity-token flow + project-scoped access.
  list-datasources           List database instances the user can see.
  list-namespaces            List schemas in a datasource.   --instance / --instance-id
  list-tables                List tables in a schema.         --instance --namespace
  describe-table             Columns + data types for a warehouse table.
  resolve-users              Resolve user IDs from names, usernames, emails, or roster files.
  search-objects             Quick Search wrapper for object IDs.
  get-model-object           Read Mosaic-contained or classic schema object definitions.
  patch-model-object         Changeset-backed object update with before/after support.
  create-users               Dry-run or create users from CSV/JSON/YAML rosters.
  build                      Create & commit a model from one or more sources.
  build-from-schema-objects  Build a Mosaic model from existing classic attribute/fact/metric IDs.
  merge-attributes           Conform differently-named FK columns by merging child expressions
                             into the parent attribute. Required for Kimball warehouses with
                             prefixed surrogate keys (i_item_sk vs ss_item_sk) where auto-
                             conformance via column-name identity won't fire.
  release-locks              Release stuck schemaEdit changesets owned by the current user.
  validate-model             Run the full post-build quality checklist (failures exit non-zero).
  validate-topology          Lightweight topology check — isolated attrs, fact-table coverage, misclassified numerics.
  discover                   Probe endpoint variants when the server version is unknown.
  create-compound-metric     Derived metric from arithmetic over metrics (expression tree; --join outer).
  create-conditional-metric  Filtered derived metric: Sum/Avg/Count of a fact metric or a distinct count
                             of an attribute, ANDed conditions, optional latest/first-period rule.
  delete-derived-metric      Delete one derived metric after a dependents check.
  create-level-metric        Once-per-entity metric: Sum(Max([M]) {~+, [Entity]+}) {~+} (snapshot facts).
  create-weighted-average    Weighted average once per entity: numerator + denominator helpers and the ratio.
  set-column-types           Change column types in the model (table + pipeline), no reload; republish after.
  create-formula-attribute   Attribute computed from columns (prefer a lookup table); republish after.
  add-column-attribute       Attribute that reads one existing column (e.g. a fact metric's) as-is; republish after.
  add-time-attributes        Year / quarter / month (week, day) intervals on a date attribute; republish after.
  rename-describe            Rename / describe attributes and fact metrics by name; no republish.

Authentication
~~~~~~~~~~~~~~
Two modes are supported:

1. **Direct login** (on-prem and tenants that expose POST /api/auth/login):
   pass --user + --password (or env MSTR_USER + MSTR_PASSWORD). The script
   logs in, runs the command, and logs out.

2. **Borrowed session** (Studio Cloud / SSO tenants where direct login isn't
   usable): pass --auth-token + --session-cookie + --ingress-cookie (or env
   MSTR_AUTH_TOKEN, MSTR_SESSION_COOKIE, MSTR_INGRESS_COOKIE), copied out of
   a logged-in browser via DevTools. The script reuses the session in-place;
   /auth/login and /auth/logout are skipped so the human's UI session is left
   intact. If a Modeling Service command needs an identity token and one is
   not provided, the script mints one in the borrowed session.

Other subcommands take optional --base / --project-id or read them from env
vars MSTR_BASE / MSTR_PROJECT_ID. Do not hardcode secrets in this file;
set MSTR_PASSWORD in the shell or keychain-backed environment.

The "build" subcommand can take --source repeatedly, each as "INSTANCE:SCHEMA:T1,T2,...":
    --source "Snowflake Prod:SALES:CUSTOMER,ORDER" --source "Oracle:FIN:INVOICE"

Column-type heuristics (override with --attr-cols / --metric-cols):
  numeric cols (int/float/numeric/decimal/double)     -> fact metric (SUM)
  everything else (varchar/text/date/id)              -> attribute (single key form)

This is the source of truth for the build-mosaic-model skill. When the server
rejects a path with 404, use `discover` to find the current variant on this
tenant's API and update ENDPOINT_CANDIDATES below.
"""
from __future__ import annotations
import argparse, copy, csv, json, os, shutil, subprocess, sys, time, urllib.parse, uuid
from typing import Any
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_PLATFORM = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, "strategy-platform", "scripts"))
sys.path.insert(0, _PLATFORM)   # shared core: sign-in, API client, MCP
import schema_object_translator as sot  # noqa: E402
import mosaic_safety as ms  # noqa: E402
import strategy_auth  # noqa: E402

# ── Configuration: all tenant values come from env vars or CLI flags ──────────
# No hardcoded tenant defaults. Required: MSTR_BASE, MSTR_USER, MSTR_PASSWORD.
# Optional: MSTR_PROJECT_ID (or resolve by name at runtime), MSTR_DEST_FOLDER_ID,
# MSTR_LOGIN_MODE (default 1 = standard). See README.md + .env.example.
DEFAULT_BASE        = os.environ.get("MSTR_BASE", "")
DEFAULT_PROJECT_ID  = os.environ.get("MSTR_PROJECT_ID", "")
DEFAULT_USER        = os.environ.get("MSTR_USER", "")
DEFAULT_PASSWORD    = os.environ.get("MSTR_PASSWORD", "")
DEFAULT_LOGIN_MODE  = int(os.environ.get("MSTR_LOGIN_MODE", "1"))
DEFAULT_DEST_FOLDER = os.environ.get("MSTR_DEST_FOLDER_ID", os.environ.get("MSTR_DEST_FOLDER", ""))
# Borrowed-session (Studio Cloud / SSO tenants where username+password login
# isn't usable). Copy the four values out of a logged-in browser session via
# DevTools → Network (X-MSTR-AuthToken header) and Application → Cookies
# (JSESSIONID + library-ingress). Identity token is optional — when omitted
# and a Modeling-Service-bound command runs, the script mints one using the
# borrowed session. See README.md.
DEFAULT_AUTH_TOKEN     = os.environ.get("MSTR_AUTH_TOKEN", "")
DEFAULT_IDENTITY_TOKEN = os.environ.get("MSTR_IDENTITY_TOKEN", "")
DEFAULT_SESSION_COOKIE = os.environ.get("MSTR_SESSION_COOKIE", "")
DEFAULT_INGRESS_COOKIE = os.environ.get("MSTR_INGRESS_COOKIE", "")
FORM_ID             = "45C11FA478E745FEA08D781CEA190FE5"   # Universal Strategy ID-form constant (all tenants)

# MicroStrategy REST paths have drifted across versions. Try these in order.
ENDPOINT_CANDIDATES = {
    "list_datasources": [
        "/api/datasources",
    ],
    "list_namespaces": [
        "/api/datasources/{id}/catalog/namespaces",
    ],
    # ns_id is base64(json({"ns":"<schemaName>"}))
    "list_tables": [
        "/api/datasources/{id}/catalog/namespaces/{ns_id}/tables",
    ],
    # tb_id is base64(json({"tbn":"<tableName>","ns":"<schemaName>"}))
    "describe_table": [
        "/api/datasources/{id}/catalog/namespaces/{ns_id}/tables/{tb_id}",
    ],
}

OPENAPI_CANDIDATES = [
    "/api/openapi.yaml",
    "/api/openapi.yml",
    "/api/openapi.json",
    "/api-docs/openapi.yaml",
    "/api-docs/swagger.json",
]

import base64 as _b64

def encode_ns_id(ns: str) -> str:
    return _b64.b64encode(json.dumps({"ns": ns}, separators=(",",":")).encode()).decode().rstrip("=") + "=="

def encode_tb_id(ns: str, tbn: str) -> str:
    raw = _b64.b64encode(json.dumps({"tbn": tbn, "ns": ns}, separators=(",",":")).encode()).decode()
    # pad to multiple of 4
    return raw + "="*((4 - len(raw)%4)%4)


def _load_json_arg(value, file_path=None) -> Any:
    if file_path:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    if value:
        return json.loads(value)
    return None


def _load_yaml(path: str) -> Any:
    """Load YAML without making PyYAML a hard dependency on macOS workstations."""
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except ModuleNotFoundError:
        ruby = shutil.which("ruby")
        if not ruby:
            die(f"{path}: YAML requires PyYAML or ruby. Convert the file to JSON or install PyYAML.")
        try:
            proc = subprocess.run(
                [ruby, "-ryaml", "-rjson", "-rdate", "-e",
                 "puts JSON.generate(YAML.safe_load(File.read(ARGV[0]), permitted_classes: [Date, Time], aliases: true))",
                 path],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            die(f"{path}: YAML parse failed: {exc.stderr.strip() or exc.stdout.strip()}")
        return json.loads(proc.stdout or "null")


def load_structured_file(path: str) -> Any:
    """Load JSON or YAML. CSV stays caller-specific because row semantics differ."""
    ext = path.lower().rsplit(".", 1)[-1]
    if ext == "json":
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    if ext in ("yaml", "yml"):
        return _load_yaml(path)
    die(f"{path}: expected .json, .yaml, or .yml")


def _parse_kv(items):
    out = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep:
            die(f"bad key=value argument: {item}")
        out[key] = value
    return out

NUMERIC_TYPES = {"integer","int","bigint","smallint","tinyint","long","short",
                 "decimal","numeric","float","double","real","number","money",
                 "int64","int32","fixed_numeric"}
ID_COLUMN_SUFFIXES = ("_ID", "ID", "_KEY", "KEY", "_CD", "_CODE", "CODE",
                      "_NO", "NO", "_NUM", "NUM", "_NUMBER", "NUMBER",
                      "_SK", "SK")
NATURAL_NUMERIC_DIMS = ("YEAR", "MONTH", "QUARTER", "QTR", "WEEK", "DAY", "FISCAL")

# Bare (no-underscore) suffix matching in _looks_like_identifier_col is
# inherently ambiguous: a column ending in the letters "ID"/"NO"/"KEY"/"SK"/etc.
# might be a compact-style key (Ergast/Kaggle-style "driverid", "raceid") or
# might just be an ordinary English word that happens to end the same way.
# Both readings can't be right at once, and there's no way to tell from the
# suffix alone -- fixing the false negative (compact keys) necessarily risks
# reintroducing false positives on ordinary words. This is a denylist of
# observed real column names that are NOT identifiers despite matching a bare
# suffix; "grid" (F1 starting-grid position, an averageable metric) was
# discovered by diffing a built model's metrics against its source schema.
# Not exhaustive -- for anything else the heuristic gets wrong, override with
# --attr-cols / --metric-cols (build) or the attributes/metrics dictionary.
BARE_ID_SUFFIX_FALSE_POSITIVES = {"GRID", "PAID", "VOID", "AVOID"}

import re as _re

def friendly_col(col: str) -> str:
    """CUSTOMER_NAME -> 'Customer Name', PO_NUMBER -> 'PO Number', ID -> 'ID'."""
    parts = col.replace("-", "_").split("_")
    out = []
    for p in parts:
        up = p.upper()
        if up in {"ID","PO","SKU","URL","API","IP","UID","GUID","USA","UK","EU","YTD","MTD"}:
            out.append(up)
        else:
            out.append(p.capitalize())
    return " ".join(out).strip()

def friendly_table(tname: str) -> str:
    """Strip common timestamp / prefix patterns and title-case. Returns the *short* label."""
    # Strip leading prefixes like SD_TECH_EXAMPLE_20260413_2145_  or similar
    stripped = _re.sub(r"^[A-Z]+(?:_[A-Z]+)*_\d{6,}(?:_\d+)*_", "", tname)
    return friendly_col(stripped)


# ── Session helpers ───────────────────────────────────────────────────────────
# Default (connect, read) timeout, safe retries and no cross-origin token forwarding all live
# in strategy_auth.SafeSession. Override per call with timeout=..., or for all calls with
# MSTR_HTTP_TIMEOUT (read seconds) / MSTR_HTTP_RETRIES.
_TimeoutSession = strategy_auth.SafeSession


class MSTR:
    def __init__(self, args):
        self.base    = (args.base or "").rstrip("/")
        # Same rules as every other sign-in path (strategy_auth.check_base): an absolute
        # http(s) Library URL, no credentials in it, and no plain http except localhost or
        # MSTR_ALLOW_HTTP=1. Borrowed tokens and cookies would otherwise travel in cleartext,
        # and a scheme-less MSTR_BASE would crash deep in requests far from the cause.
        try:
            strategy_auth.check_base(self.base)
        except strategy_auth.AuthError as e:
            die(f"{e} (MSTR_BASE / --base).")
        self.project = args.project_id
        self.user    = args.user
        self.pw      = args.password
        self.mode    = args.login_mode
        self.auth_method = getattr(args, "auth_method", None)
        self.signin: strategy_auth.SignIn | None = None
        # Borrowed-session inputs: when set, MSTR will reuse an externally-held
        # session (e.g. tokens + cookies copied out of a logged-in browser) and
        # skip /auth/login and /auth/logout so the human's UI session is left
        # intact. Required for Studio Cloud (<tenant>.strategy.com) where direct
        # username/password login is not exposed.
        self.preset_auth_token     = getattr(args, "auth_token", "") or ""
        self.preset_identity_token = getattr(args, "identity_token", "") or ""
        self.preset_session_cookie = getattr(args, "session_cookie", "") or ""
        self.preset_ingress_cookie = getattr(args, "ingress_cookie", "") or ""
        self.s       = _TimeoutSession()
        self.s.headers.update({"Content-Type":"application/json","Accept":"application/json"})
        # Changesets opened and not yet committed/discarded; main() discards leftovers.
        self.open_changesets: set[str] = set()
        host = urllib.parse.urlsplit(self.base).hostname or ""
        if self.preset_session_cookie:
            self.s.cookies.set("JSESSIONID", self.preset_session_cookie, domain=host)
        if self.preset_ingress_cookie:
            self.s.cookies.set("library-ingress", self.preset_ingress_cookie, domain=host)
        self.verbose = args.verbose
        self.logged_in = False
        # True when this session was bootstrapped from caller-supplied tokens.
        # logout() and any "kill stale sessions" helpers must NOT delete this
        # session — it is owned by an external client (browser tab, CI runner).
        self.borrowed_session = False

    def login(self, *, identity: bool = False):
        # Borrowed-session mode: caller supplied an auth token already. Skip
        # /auth/login entirely; only mint an identity token if asked and not
        # already provided. /auth/logout is also disabled to protect the
        # external owner's session.
        if self.preset_auth_token:
            self.s.headers["X-MSTR-AuthToken"] = self.preset_auth_token
            if self.project:
                self.s.headers["X-MSTR-ProjectID"] = self.project
            if self.preset_identity_token:
                self.s.headers["X-MSTR-IdentityToken"] = self.preset_identity_token
            elif identity:
                r2 = self.s.post(f"{self.base}/api/auth/identityToken")
                if r2.ok:
                    id_tok = r2.headers.get("X-Mstr-Identitytoken") or r2.headers.get("X-MSTR-IdentityToken","")
                    if id_tok:
                        self.s.headers["X-MSTR-IdentityToken"] = id_tok
                    else:
                        print("[auth] WARN: /auth/identityToken returned 2xx but no token header. "
                              "Modeling Service changesets may fail. Pass --identity-token explicitly.",
                              file=sys.stderr)
                else:
                    print(f"[auth] WARN: identity-token mint failed ({r2.status_code}). "
                          "Modeling Service changesets will fail. Re-grab the cookies "
                          "(JSESSIONID + library-ingress) from a fresh browser session, "
                          "or pass --identity-token explicitly.",
                          file=sys.stderr)
            self.logged_in        = True
            self.borrowed_session = True
            if self.verbose:
                has_id = "X-MSTR-IdentityToken" in self.s.headers
                print(f"[auth] borrowed session in use; identity={'yes' if has_id else 'no'}",
                      file=sys.stderr)
            return
        try:
            self.signin = strategy_auth.sign_in(self.s, strategy_auth.AuthConfig.from_env(
                base=self.base, method=self.auth_method, username=self.user, password=self.pw,
                login_mode=self.mode))
        except strategy_auth.AuthError as e:
            die(f"{e}. Single sign-on tenants: --auth-method sso (see strategy_auth.py).")
        self.s.headers["X-MSTR-ProjectID"] = self.project
        self.logged_in = True
        if identity:
            # Identity token is required for Mosaic data-model Modeling Service changesets,
            # but can break classic/project Modeling reads on some tenants.
            r2 = self.s.post(f"{self.base}/api/auth/identityToken")
            if r2.ok:
                id_tok = r2.headers.get("X-Mstr-Identitytoken") or r2.headers.get("X-MSTR-IdentityToken","")
                if id_tok:
                    self.s.headers["X-MSTR-IdentityToken"] = id_tok
                else:
                    print("[auth] WARN: /auth/identityToken returned 2xx but no token header. "
                          "Modeling Service changesets may fail.", file=sys.stderr)
            else:
                print(f"[auth] WARN: identity-token mint failed ({r2.status_code} {r2.text[:120]}). "
                      "Modeling Service changesets will fail.", file=sys.stderr)
        if self.verbose:
            print(f"[auth] signed in via {self.signin.method}"
                  f"{' as ' + self.signin.user if self.signin.user else ''}; "
                  f"identity={'yes' if 'X-MSTR-IdentityToken' in self.s.headers else 'no'}", file=sys.stderr)

    # raw helpers
    def get(self, path, **kw):  return self.s.get(f"{self.base}{path}", **kw)
    def post(self, path, **kw): return self.s.post(f"{self.base}{path}", **kw)
    def put(self, path, **kw):  return self.s.put(f"{self.base}{path}", **kw)
    def patch(self, path, **kw): return self.s.patch(f"{self.base}{path}", **kw)
    def delete(self, path, **kw): return self.s.delete(f"{self.base}{path}", **kw)

    def logout(self):
        if not self.logged_in:
            return
        if self.borrowed_session:
            # The session belongs to whoever lent us the token (typically a
            # human's browser tab). Logging it out would end their UI session
            # mid-task — never do that.
            self.logged_in = False
            return
        # POST /api/auth/logout for sessions this process created; sessions
        # shared with a browser or cached for later commands stay open.
        strategy_auth.sign_out(self.s, self.base, self.signin)
        self.logged_in = False

    def try_candidates(self, kind, *, required: bool = True, **fmt) -> tuple[str, Any]:
        """Walk ENDPOINT_CANDIDATES[kind]; return (path_used, json_body) on first 2xx.
        With required=False, return (None, None) instead of stopping the run."""
        last_err = None
        for tmpl in ENDPOINT_CANDIDATES[kind]:
            path = tmpl.format(**fmt)
            r = self.get(path)
            # a source database that is waking up (serverless Postgres suspends when idle) answers a connection error
            # first: retry the same endpoint a few times before treating it as not working
            for wait in (5, 10, 20):
                if r.ok or not _source_waking(r):
                    break
                print(f"  ↻ {kind}: the source database is not reachable yet; retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                r = self.get(path)
            if r.ok:
                return path, (r.json() if r.text else {})
            last_err = (path, r.status_code, r.text[:200])
            if self.verbose:
                print(f"[probe] {path} -> {r.status_code}", file=sys.stderr)
        if not required:
            print(f"  WARN {kind}: no working endpoint. last attempt: {last_err}", file=sys.stderr)
            return None, None
        die(f"{kind}: no working endpoint. last attempt: {last_err}")


def _source_waking(r) -> bool:
    """Strategy's answer when it cannot open a connection to a source database (ERR001 'Database error connection')"""
    return r.status_code in (500, 502, 503) and ("Database error connection" in r.text or "-2147216380" in r.text)


def die(msg):
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(2)

def new_uuid() -> str:
    return str(uuid.uuid4()).upper().replace("-","")


# ── Discovery ─────────────────────────────────────────────────────────────────
def cmd_auth_probe(m: MSTR, args):
    """Confirm login + identity-token flow + project-scoped access.

    Studio Cloud (and some SSO tenants) accept the X-MSTR-AuthToken on
    /auth/* endpoints but reject project-scoped reads without the right
    cookies. Without a project-scoped probe, auth-probe will pass and
    list-datasources will 401 — a frustrating loop. So the probe also
    hits a project-scoped read and reports whether it succeeded.
    """
    m.login(identity=True)
    out: dict[str, object] = {
        "ok": True,
        "base": m.base,
        "project_id": m.project,
        "user": (m.signin.user if m.signin and m.signin.user else m.user),
        "auth_method": (m.signin.method if m.signin else "borrowed"),
        "has_auth_token": "X-MSTR-AuthToken" in m.s.headers,
        "has_identity_token": "X-MSTR-IdentityToken" in m.s.headers,
    }
    if m.project:
        # GET /api/folders/preDefined/7 = project-public root; cheap + present on every tenant.
        r = m.get(f"/api/folders/preDefined/7")
        out["project_access"] = bool(r.ok)
        if not r.ok:
            out["project_access_status"] = r.status_code
            try:
                body = r.json()
            except ValueError:
                body = r.text[:200]
            out["project_access_error"] = body
            out["project_access_hint"] = (
                "If using --auth-token, also pass --session-cookie (JSESSIONID) and "
                "--ingress-cookie (library-ingress) — Studio Cloud requires both."
            )
    else:
        out["project_access"] = "skipped"
        out["project_access_hint"] = "no --project-id supplied; cannot probe project-scoped APIs"
    print(json.dumps(out, indent=2))


def cmd_list_datasources(m: MSTR, args):
    m.login()
    path, body = m.try_candidates("list_datasources")
    # Normalize: most endpoints return {"datasources":[{id,name,...}]} or {"databaseInstances":[...]}
    items = body.get("datasources") or body.get("databaseInstances") or (body if isinstance(body,list) else [])
    rows = [{"id": it.get("id") or it.get("objectId"),
             "name": it.get("name"),
             "description": it.get("description",""),
             "databaseType": it.get("databaseType") or it.get("dbmsType") or ""}
            for it in items if isinstance(it, dict)]
    if args.name:
        rows = [r for r in rows if args.name.lower() in (r["name"] or "").lower()]
    print(f"# endpoint used: {path}", file=sys.stderr)
    print(json.dumps(rows, indent=2))


def resolve_instance_id(m: MSTR, name_or_id: str) -> str:
    """Accept either an ID (32-hex) or a name; return ID."""
    if len(name_or_id) == 32 and all(c in "0123456789ABCDEFabcdef" for c in name_or_id):
        return name_or_id.upper()
    _, body = m.try_candidates("list_datasources")
    items = body.get("datasources") or body.get("databaseInstances") or (body if isinstance(body,list) else [])
    hits = [it for it in items if isinstance(it, dict)
            and (it.get("name","").lower() == name_or_id.lower())]
    if not hits:
        # fuzzy
        hits = [it for it in items if isinstance(it, dict)
                and name_or_id.lower() in it.get("name","").lower()]
    if not hits:
        die(f"no datasource matches '{name_or_id}'")
    if len(hits) > 1:
        die(f"'{name_or_id}' is ambiguous: {[h.get('name') for h in hits]}. Pass --instance-id.")
    return (hits[0].get("id") or hits[0].get("objectId")).upper()


def cmd_list_namespaces(m: MSTR, args):
    m.login()
    ds_id = args.instance_id or resolve_instance_id(m, args.instance)
    path, body = m.try_candidates("list_namespaces", id=ds_id)
    # Normalize
    items = body.get("namespaces") or (body if isinstance(body, list) else [])
    rows = [{"name": it.get("name") if isinstance(it,dict) else it} for it in items]
    print(f"# endpoint used: {path}", file=sys.stderr)
    print(json.dumps(rows, indent=2))


_NAMESPACE_IDS: dict[tuple[str, str], str] = {}


def resolve_namespace_id(m: MSTR, ds_id: str, ns_name: str) -> str:
    """Look up namespaceId via catalog/namespaces (once per datasource+name), fall back to b64."""
    key = (ds_id, ns_name.lower())
    if key not in _NAMESPACE_IDS:
        _NAMESPACE_IDS[key] = encode_ns_id(ns_name)
        r = m.get(f"/api/datasources/{ds_id}/catalog/namespaces")
        if r.ok:
            for ns in (r.json().get("namespaces") or []):
                if ns.get("name","").lower() == ns_name.lower():
                    _NAMESPACE_IDS[key] = ns["id"]
                    break
    return _NAMESPACE_IDS[key]

def cmd_list_tables(m: MSTR, args):
    m.login()
    ds_id = args.instance_id or resolve_instance_id(m, args.instance)
    ns_id = resolve_namespace_id(m, ds_id, args.namespace)
    path, body = m.try_candidates("list_tables", id=ds_id, ns_id=ns_id)
    items = body.get("tables") or (body if isinstance(body, list) else [])
    rows = [{"name": it.get("name"),
             "id": it.get("id"),
             "namespace": it.get("namespace", args.namespace),
             "type": it.get("type","")} for it in items if isinstance(it,dict)]
    if args.match:
        rows = [r for r in rows if args.match.lower() in (r["name"] or "").lower()]
    print(f"# endpoint used: {path}", file=sys.stderr)
    print(json.dumps(rows, indent=2))


def cmd_describe_table(m: MSTR, args):
    m.login()
    ds_id = args.instance_id or resolve_instance_id(m, args.instance)
    ns_id = resolve_namespace_id(m, ds_id, args.namespace)
    tb_id = encode_tb_id(args.namespace, args.table)
    path, body = m.try_candidates("describe_table", id=ds_id, ns_id=ns_id, tb_id=tb_id)
    print(f"# endpoint used: {path}", file=sys.stderr)
    print(json.dumps(body, indent=2))


def cmd_describe_tables(m: MSTR, args):
    """Describe many tables in ONE login/logout cycle to avoid the project session cap.

    --source accepts `instanceId:namespace:table` (repeatable). Output is a JSON
    dict keyed by `namespace.table` with the raw describe payload per table.
    """
    m.login()
    out = {}
    for spec in args.source:
        try:
            ins, ns, tb = spec.split(":", 2)
        except ValueError:
            die(f"--source must be instanceId:namespace:table, got {spec!r}")
        ns_id = resolve_namespace_id(m, ins, ns)
        tb_id = encode_tb_id(ns, tb)
        path, body = m.try_candidates("describe_table", id=ins, ns_id=ns_id, tb_id=tb_id)
        out[f"{ns}.{tb}"] = body
        print(f"# {ns}.{tb} via {path}", file=sys.stderr)
    print(json.dumps(out, indent=2, default=str))


def cmd_kill_sessions(m: MSTR, args):
    """List this user's open Intelligence Server connections and, with --yes, disconnect
    them — the way out of the per-project session cap (8004cb0a / -2147072486) without
    waiting for the ~30-minute idle timer. Uses the user-connection monitor
    (GET /api/monitors/userConnections, DELETE /api/monitors/userConnections/{id}),
    which needs a monitoring/administration privilege; without it the server answers
    403 and stale sessions only end on the idle timer. Your browser sessions are user
    connections too: filter with --project / --idle-minutes before --yes.
    """
    m.login()
    info = _json_or(m.get("/api/sessions/userInfo"), {})   # id + fullName; no login name here
    me, me_full = "", str(info.get("fullName") or "")
    if info.get("id"):
        u = m.get(f"/api/users/{info['id']}")
        if u.ok:
            me = str(_json_or(u, {}).get("username") or "")
    if args.yes and not me:
        # Without the login name the only key left is the display name, which other users can
        # share — disconnecting by it could end someone else's sessions.
        die("could not read your login name (GET /api/users/{id} failed); refusing to disconnect "
            "connections matched by display name only. Run without --yes to list them.")
    nodes_r = m.get("/api/monitors/iServer/nodes")
    if nodes_r.status_code in (401, 403):
        die("listing user connections needs a monitoring privilege on this tenant; stale sessions "
            "will end on the idle timer (~30 min)")
    nodes = [n.get("name") for n in (_json_or(nodes_r, {}).get("nodes") or [])
             if isinstance(n, dict) and n.get("name")] or [None]
    conns = []
    for node in nodes:
        offset = 0
        for _page in range(50):                     # the monitor pages at `limit` (1000) rows
            params: dict = {"limit": 1000, "offset": offset}
            if me:
                params["username"] = me
            if node:
                params["clusterNode"] = node
            r = m.get("/api/monitors/userConnections", params=params)
            if r.status_code in (401, 403):
                die("listing user connections needs a monitoring privilege on this tenant")
            if not r.ok:
                die(f"list user connections: {format_mstr_error(r)}")
            body = _json_or(r, {})
            page = body.get("userConnections") or []
            conns.extend(page)
            offset += len(page)
            if len(page) < 1000 or offset >= int(body.get("totalFiltered") or 0):
                break
    now = time.time()

    def mine(c: dict) -> bool:
        if me:
            return (c.get("username") or "").lower() == me.lower()
        return bool(me_full) and (c.get("userFullName") or "") == me_full

    def idle(c: dict) -> float:
        minutes = _connection_idle_minutes(c, now)
        return minutes if minutes is not None else 0.0

    picked = [c for c in conns if mine(c)
              and (not args.project or args.project in (c.get("projectName"), c.get("projectId")))
              and idle(c) >= args.idle_minutes]
    rows = [{"id": c.get("id"), "project": c.get("projectName"), "application": c.get("applicationType"),
             "created": c.get("dateConnectionCreated"), "idle_minutes": round(idle(c)),
             "open_jobs": c.get("openJobsCount")} for c in picked]
    disconnected = 0
    if args.yes:
        for c in picked:
            if m.delete(f"/api/monitors/userConnections/{c.get('id')}").status_code in (200, 204):
                disconnected += 1
    print(json.dumps({"user": me or me_full, "matched_by": "login" if me else "display_name",
                      "connections": rows, "disconnected": disconnected,
                      "dry_run": not args.yes}, indent=2))


def _json_or(r, default: Any = None) -> Any:
    """r.json(), or `default` (an empty dict when omitted) when the body is empty, not JSON,
    or not the expected kind of value."""
    if default is None:
        default = {}
    try:
        body = r.json() if getattr(r, "text", "x") else None
    except ValueError:
        return default
    if body is None or not isinstance(body, type(default)):
        return default
    return body


def _connection_idle_minutes(c: dict, now: float) -> float | None:
    """Minutes since a user connection's last job (or its creation). The monitor's timestamps
    carry a UTC offset (…+0000, …-0500); it must be kept — dropping it skews idle time by the
    offset (hours), which once made every connection look stale. No offset means UTC. None when
    the stamp can't be read."""
    last = str(c.get("dateLastJobSubmitted") or c.get("dateConnectionCreated") or "").strip()
    if not last:
        return None
    stamp = strategy_auth._epoch(last.replace(" ", "T", 1))
    return (now - stamp) / 60 if stamp is not None else None


def cmd_release_locks(m: MSTR, args):
    """Release stuck Modeling Service changesets owned by the current user.

    If a build/wire script dies between open_cs() and commit_cs() (network blip,
    KeyboardInterrupt, killed process), the schemaEdit lock persists on the
    project and every subsequent open returns 8004cc41 until it ages out.
    There is no way to enumerate "all open changesets" via the public API, so
    this helper provokes the lock conflict, parses the LOCKID out of the error,
    and releases it only when GET /api/model/schema/lock says the current user owns
    it (see _release_own_lock). Repeats until open succeeds (in which case it
    discards the freshly-opened one too, leaving the project clean).

    "Owned by the current user" means the same account, not this process: run it only
    when no other job is using this account against the project.
    """
    m.login()
    released: list[str] = []
    for _ in range(int(args.max_iters)):
        # schemaEdit must be in BOTH the body and the query param. Strategy
        # parses one or the other depending on path version; body-only opens
        # a non-schema changeset that never collides with the stale lock.
        r = m.post("/api/model/changesets?schemaEdit=true", json={"schemaEdit": True})
        if r.ok:
            cs = _json_or(r, {}).get("id")
            if cs:
                if isinstance(getattr(m, "open_changesets", None), set):
                    m.open_changesets.add(cs)        # discarded on any exit path
                discard_cs(m, cs)
            break
        body = _json_or(r, {})
        if not body:
            print(f"release-locks: unexpected response {r.status_code}: {r.text[:200]}", file=sys.stderr)
            break
        lockid = _release_own_lock(m, body)
        if not lockid:
            break
        released.append(lockid)
        print(f"release-locks: released {lockid}", file=sys.stderr)
    print(json.dumps({"released": released, "count": len(released)}, indent=2))


def cmd_discover(m: MSTR, args):
    """Probe every endpoint variant and print which one worked.
    Useful when porting this skill to a new MSTR version/tenant."""
    m.login()
    results = {}
    for kind, candidates in ENDPOINT_CANDIDATES.items():
        results[kind] = []
        for tmpl in candidates:
            # substitute with args if provided, else skip templated ones
            try:
                ns_id = encode_ns_id(args.namespace) if args.namespace else "PLACEHOLDER"
                tb_id = encode_tb_id(args.namespace, args.table) if (args.namespace and args.table) else "PLACEHOLDER"
                path = tmpl.format(id=args.instance_id or "PLACEHOLDER",
                                   ns_id=ns_id, tb_id=tb_id)
            except Exception:
                path = tmpl
            if "PLACEHOLDER" in path and kind != "list_datasources":
                results[kind].append({"path": tmpl, "skipped": "missing --instance-id/--namespace/--table"})
                continue
            r = m.get(path)
            results[kind].append({"path": path, "status": r.status_code,
                                  "ok": r.ok,
                                  "body_sample": (r.text[:200] if r.text else "")})
    print(json.dumps(results, indent=2))


def cmd_openapi_summary(m: MSTR, args):
    """Fetch the instance's machine-readable OpenAPI spec and summarize useful paths.

    The Swagger UI at /api-docs/ is a JS app. Current Strategy Library servers also
    expose the raw spec at /api/openapi.yaml; keep this probe because exact locations
    can differ across releases and customer-managed deployments.
    """
    last = None
    spec_text = None
    spec_path = None
    for path in OPENAPI_CANDIDATES:
        r = m.get(path)
        if r.ok and ("openapi:" in r.text[:1000] or '"openapi"' in r.text[:1000]):
            spec_text = r.text
            spec_path = path
            break
        last = {"path": path, "status": r.status_code, "body_sample": r.text[:120]}
    if spec_text is None:
        die(f"no OpenAPI spec found. last attempt: {last}")

    if args.out:
        _write_private(args.out, spec_text)

    title = _re.search(r"^\s*title:\s*(.+)$", spec_text, _re.MULTILINE)
    version = _re.search(r"^\s*version:\s*[\"']?([^\"'\n]+)", spec_text, _re.MULTILINE)
    paths = _re.findall(r"^  (/api/[^:]+):\s*$", spec_text, _re.MULTILINE)
    tags = _re.findall(r"^- name:\s*(.+)$", spec_text, _re.MULTILINE)
    filters = args.path_filter or [
        "/api/model/dataModels",
        "/api/model/changesets",
        "/api/model/securityFilters",
        "/api/model/attributes",
        "/api/model/facts",
        "/api/model/tables",
        "/api/cubes",
        "/api/datasources",
        "/api/auth",
    ]
    selected = [p for p in paths if any(p.startswith(prefix) for prefix in filters)]
    print(json.dumps({
        "ok": True,
        "url": f"{m.base}{spec_path}",
        "saved_to": args.out,
        "openapi": bool(_re.search(r"^openapi:", spec_text, _re.MULTILINE)),
        "title": title.group(1).strip().strip('"') if title else None,
        "version": version.group(1).strip() if version else None,
        "path_count": len(paths),
        "tag_count": len(tags),
        "key_tags": [t for t in tags if t in {
            "Authentication", "Changesets", "Data Models", "Datasource Management",
            "Schema Modeling", "Security Filters", "Cubes", "Objects"
        }],
        "selected_paths": selected[: args.limit],
    }, indent=2))


def _get_openapi_text(m: MSTR, local_path=None):
    if local_path and os.path.exists(local_path):
        with open(local_path, encoding="utf-8") as f:
            return local_path, f.read()
    default_local = os.path.join(os.getcwd(), "openapi.yaml")
    if os.path.exists(default_local):
        with open(default_local, encoding="utf-8") as f:
            return default_local, f.read()
    for path in OPENAPI_CANDIDATES:
        r = m.get(path)
        if r.ok and ("openapi:" in r.text[:1000] or '"openapi"' in r.text[:1000]):
            return f"{m.base}{path}", r.text
    die("no OpenAPI spec found locally or at known tenant paths")


def cmd_openapi_search(m: MSTR, args):
    """Search the Strategy OpenAPI YAML without loading the entire spec into context."""
    source, text = _get_openapi_text(m, args.file)
    flags = 0 if args.case_sensitive else _re.IGNORECASE
    pat = _re.compile(args.pattern, flags)
    lines = text.splitlines()
    matches = []
    for idx, line in enumerate(lines, start=1):
        if pat.search(line):
            start = max(1, idx - args.context)
            end = min(len(lines), idx + args.context)
            matches.append({
                "line": idx,
                "text": line,
                "context": [{"line": i, "text": lines[i-1]} for i in range(start, end + 1)] if args.context else None,
            })
            if len(matches) >= args.limit:
                break
    print(json.dumps({
        "ok": True,
        "source": source,
        "pattern": args.pattern,
        "matches": matches,
        "truncated": len(matches) >= args.limit,
    }, indent=2))


def cmd_api_call(m: MSTR, args):
    """Generic Strategy REST call for workflows not yet wrapped by a subcommand."""
    if not args.no_auth:
        m.login(identity=args.with_identity_token)
    method = args.method.upper()
    if method == "DELETE" and not args.yes:
        die("DELETE requires --yes")
    path = args.path if args.path.startswith("/") else f"/{args.path}"
    params = _parse_kv(args.param)
    headers = _parse_kv(args.header)
    body = _load_json_arg(args.json, args.json_file)
    request = getattr(m.s, method.lower(), None)
    if request is None:
        die(f"unsupported method {method}")
    files = {}
    for spec in args.file or []:
        field, _, fpath = spec.partition("=")
        if not (field and fpath):
            die(f"bad --file '{spec}'. expected FIELD=PATH")
        if not os.path.isfile(fpath):
            die(f"--file {field}: no such file: {fpath}")
        files[field] = (os.path.basename(fpath), open(fpath, "rb"))
    form = _parse_kv(args.form)
    if files or form:
        if body is not None:
            die("--json/--json-file cannot be combined with --file/--form (multipart body)")
        # The session default Content-Type: application/json must not override
        # the multipart boundary; a None value tells requests to drop the header.
        headers["Content-Type"] = None
        try:
            r = request(f"{m.base}{path}", params=params or None, headers=headers,
                        data=form or None, files=files or None)
        finally:
            for _name, handle in files.values():
                handle.close()
    else:
        r = request(f"{m.base}{path}", params=params or None, headers=headers or None, json=body)
    out = {
        "ok": r.ok,
        "status": r.status_code,
        "url": r.url,
        "headers": {k: ("<redacted>" if k.lower() in {"x-mstr-authtoken", "x-mstr-identitytoken"} else v)
                    for k, v in r.headers.items()
                    if k.lower().startswith("x-mstr") or k.lower() in {"content-type", "location"}},
    }
    try:
        out["body"] = r.json()
    except ValueError:
        out["body"] = r.text[: args.text_limit]
        out["body_truncated"] = len(r.text) > args.text_limit
    if args.out:
        # The file keeps the full body (tokens included) and is readable by this user only.
        _write_private(args.out, out.get("body", ""))
        out["saved_to"] = args.out
    if not getattr(args, "show_secrets", False):
        # Some responses carry credentials in the body (POST /api/v2/auth/identityToken,
        # POST /api/auth/apiTokens); terminal output and logs must not.
        out["body"] = _redact_body_secrets(out.get("body"))
    print(json.dumps(out, indent=2))


_SECRET_BODY_KEYS = {"apitoken", "identitytoken", "authtoken", "accesstoken", "refreshtoken", "idtoken",
                     "token", "password", "newpassword", "oldpassword", "clientsecret"}


def _redact_body_secrets(value):
    """Copy of a JSON value with credential-looking fields replaced by "<redacted>"."""
    if isinstance(value, dict):
        return {k: ("<redacted>" if str(k).lower() in _SECRET_BODY_KEYS and v not in (None, "")
                    else _redact_body_secrets(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_body_secrets(v) for v in value]
    return value


def _write_private(path: str, data) -> None:
    """Write JSON (dict/list) or text to `path`, readable by this user only (0600): responses
    and reports can carry tokens or user data."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)          # an existing file keeps its old mode otherwise
    except (AttributeError, OSError):
        pass
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        if isinstance(data, (dict, list)):
            json.dump(data, f, indent=2, default=str)
        else:
            f.write(str(data))


# ── Build ─────────────────────────────────────────────────────────────────────
def parse_source(src: str):
    inst, _, rest = src.partition(":")
    schema, _, tables = rest.partition(":")
    if not (inst and schema and tables):
        die(f"bad --source '{src}'. expected INSTANCE:SCHEMA:T1,T2,...")
    return inst.strip(), schema.strip(), [t.strip() for t in tables.split(",") if t.strip()]


def load_dictionary(path: str) -> dict:
    """Load a JSON or YAML dictionary of name/description overrides + explicit relationships.

    Expected shape (JSON or YAML):
    {
      "attributes": {
        "<TABLE>.<COLUMN>": {"name": "Friendly Name", "description": "..."}
      },
      "metrics": {
        "<TABLE>.<COLUMN>": {"name": "...", "description": "...", "function": "sum|avg|...",
                             "format": "<METRIC_FORMAT_PRESETS key> | [format.values tokens]"}
      },
      "relationships": [
        {"parent":"<TABLE>.<COL>","child":"<TABLE>.<COL>",
         "relationship_table":"<TABLE>",
         "type":"one_to_many|many_to_many|one_to_one"}
      ],
      "tables": {
        "<TABLE>": {"description": "optional table-level description"}
      }
    }

    CSV form (simpler) — columns: table,column,kind,name,description,function
    """
    if not path:
        return {"attributes":{},"metrics":{},"relationships":[],"tables":{}}
    ext = path.lower().rsplit(".",1)[-1]
    if ext in ("json","yaml","yml"):
        d = load_structured_file(path) or {}
    elif ext == "csv":
        d = {"attributes":{},"metrics":{},"relationships":[],"tables":{}}
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                row = {str(k).strip().lower(): (v or "") for k, v in row.items()}
                if not row.get("table") or not row.get("column"):
                    continue
                key = f"{row['table'].strip()}.{row['column'].strip()}"
                kind = (row.get("kind") or "attribute").strip().lower()
                bucket = d["metrics"] if kind in ("metric","fact_metric") else d["attributes"]
                entry = {"name": row.get("name","").strip(), "description": row.get("description","").strip()}
                if row.get("function"): entry["function"] = row["function"].strip()
                bucket[key] = {k:v for k,v in entry.items() if v}
    else:
        die(f"{path}: dictionary must be JSON, YAML, or CSV")
    # Guardrail: a FLAT plan dictionary (TABLE.COLUMN -> {kind, metric_function, ...})
    # is NOT this native shape. If passed here it yields empty overrides and the
    # build silently falls back to column heuristics (every metric -> SUM,
    # kind:skip dropped, business names lost). Refuse instead of failing silently —
    # compile the plan dict to the native shape first (e.g. plan_to_native.py).
    if isinstance(d, dict) and not ({"attributes","metrics","relationships","tables"} & set(d)):
        flatish = [k for k, v in d.items()
                   if isinstance(k, str) and "." in k and isinstance(v, dict)
                   and ({"kind","metric_function","business_name"} & set(v))]
        if flatish:
            die(f"{path}: this looks like a FLAT plan dictionary (TABLE.COLUMN keys "
                f"carrying 'kind'/'metric_function'/'business_name', e.g. {flatish[0]!r}), "
                f"not the native load_dictionary shape ({{\"attributes\":{{...}}, "
                f"\"metrics\":{{...}}}}). build-from-config would silently ignore it and "
                f"default every metric to SUM. Compile it to the native shape first and "
                f"pass that file (mosaic-migration-skills: scripts/lib/plan_to_native.py).")
    # Normalize
    d.setdefault("attributes",{}); d.setdefault("metrics",{})
    d.setdefault("relationships",[]); d.setdefault("tables",{})
    return d


def load_erd(path: str) -> list[dict]:
    """Parse an ERD file → list of relationship specs.
    Supports: JSON/YAML list of relationships, DBML (Ref lines), Mermaid erDiagram,
    and SQL DDL (best-effort REFERENCES clause extraction).
    For PNG/JPG ERDs, let the calling agent convert to one of the text formats first."""
    if not path: return []
    ext = path.lower().rsplit(".",1)[-1]
    txt = None
    if ext in ("json","yaml","yml"):
        d = load_structured_file(path) or {}
        if isinstance(d, dict): d = d.get("relationships", [])
        return d
    with open(path, encoding="utf-8") as f: txt = f.read()
    rels = []
    # Every pattern below starts at a word boundary (lookbehind): an unanchored leading
    # `\w+` / `Ref` makes the scan quadratic on long tokens (a 40 KB word took 15 s).
    # DBML:  Ref: "posts"."user_id" > "users"."id"   or  Ref: posts.user_id > users.id
    for m in _DBML_REF.finditer(txt):
        src_t, src_c, op, dst_t, dst_c = m.groups()
        if op == "<":     # src is the one side, dst the many side
            rels.append({"parent": f"{src_t}.{src_c}", "child": f"{dst_t}.{dst_c}",
                         "relationship_table": dst_t, "type": "one_to_many"})
        elif op == "<>":
            rels.append({"parent": f"{src_t}.{src_c}", "child": f"{dst_t}.{dst_c}",
                         "relationship_table": src_t, "type": "many_to_many"})
        else:             # ">" src many → dst one;  "-" one-to-one
            rels.append({"parent": f"{dst_t}.{dst_c}", "child": f"{src_t}.{src_c}",
                         "relationship_table": src_t,
                         "type": "one_to_one" if op == "-" else "one_to_many"})
    # Mermaid: TABLE1 ||--o{ TABLE2 : label   (one-to-many)
    for m in _MERMAID_ONE_TO_MANY.finditer(txt):
        parent_t, child_t, col = m.groups()
        rels.append({"parent": f"{parent_t}.{col}", "child": f"{child_t}.{col}",
                     "relationship_table": child_t, "type": "one_to_many"})
    rels.extend(_ddl_foreign_keys(txt))
    return rels


_DBML_REF = _re.compile(
    r'(?<![\w"])Ref:?\s*"?([^".\s]+)"?\."?([^".\s]+)"?\s*([<>\-]+)\s*"?([^".\s]+)"?\."?([^".\s]+)"?')
_MERMAID_ONE_TO_MANY = _re.compile(r'(?<!\w)(\w+)\s*\|\|--o\{\s*(\w+)\s*:\s*"?(\w+)"?')
_DDL_IDENT = r'(?:"[^"\n]+"|`[^`\n]+`|\[[^\]\n]+\]|\w+)'
_DDL_QNAME = rf'(?:{_DDL_IDENT}\s*\.\s*){{0,2}}({_DDL_IDENT})'     # [db.][schema.]name -> name
_DDL_CREATE = _re.compile(
    rf'(?<!\w)CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:GLOBAL|LOCAL)\s+)?(?:TEMP(?:ORARY)?\s+)?(?:EXTERNAL\s+)?'
    rf'TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{_DDL_QNAME}\s*\(', _re.I)
_DDL_ALTER_FK = _re.compile(
    rf'(?<!\w)ALTER\s+TABLE\s+(?:ONLY\s+)?(?:IF\s+EXISTS\s+)?{_DDL_QNAME}\s+ADD\s+(?:CONSTRAINT\s+{_DDL_IDENT}\s+)?'
    rf'FOREIGN\s+KEY\s*\(\s*({_DDL_IDENT})\s*\)\s*REFERENCES\s+{_DDL_QNAME}\s*\(\s*({_DDL_IDENT})\s*\)', _re.I)
_DDL_TABLE_FK = _re.compile(
    rf'^(?:CONSTRAINT\s+{_DDL_IDENT}\s+)?FOREIGN\s+KEY\s*\(\s*({_DDL_IDENT})\s*\)\s*'
    rf'REFERENCES\s+{_DDL_QNAME}\s*\(\s*({_DDL_IDENT})\s*\)', _re.I)
_DDL_COLUMN_FK = _re.compile(
    rf'^({_DDL_IDENT})\s.*?\bREFERENCES\s+{_DDL_QNAME}\s*\(\s*({_DDL_IDENT})\s*\)', _re.I | _re.S)
_DDL_CONSTRAINT_WORDS = {"constraint", "primary", "foreign", "unique", "check", "key", "index", "exclude", "like"}


def _ddl_name(ident: str) -> str:
    ident = ident.strip()
    if len(ident) >= 2 and ident[0] + ident[-1] in ('""', "``", "[]"):
        return ident[1:-1]
    return ident


def _ddl_split_items(body: str) -> list[str]:
    """Top-level comma-separated items of a CREATE TABLE body (parentheses and quotes respected)."""
    items, cur, depth, quote = [], [], 0, None
    for ch in body:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    items.append("".join(cur).strip())
    return [i for i in items if i]


def _ddl_foreign_keys(txt: str) -> list[dict]:
    """Single-column foreign keys from SQL DDL: column-level REFERENCES and table-level
    FOREIGN KEY clauses inside CREATE TABLE (with IF NOT EXISTS, schema-qualified and quoted
    names, statements with or without `;`), and ALTER TABLE … ADD … FOREIGN KEY. Composite
    keys are skipped: one attribute relationship can't express them."""
    txt = _re.sub(r"/\*.*?\*/", " ", txt, flags=_re.S)
    txt = _re.sub(r"--[^\n]*", " ", txt)
    rels = []

    def add(child_t, child_c, parent_t, parent_c):
        child_t, child_c, parent_t, parent_c = map(_ddl_name, (child_t, child_c, parent_t, parent_c))
        rels.append({"parent": f"{parent_t}.{parent_c}", "child": f"{child_t}.{child_c}",
                     "relationship_table": child_t, "type": "one_to_many"})

    for tm in _DDL_CREATE.finditer(txt):
        child_t = tm.group(1)
        depth, quote, end = 1, None, len(txt)        # body = text up to the matching ")"
        for i in range(tm.end(), len(txt)):
            ch = txt[i]
            if quote:
                quote = None if ch == quote else quote
            elif ch in "'\"`":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        for item in _ddl_split_items(txt[tm.end():end]):
            fk = _DDL_TABLE_FK.match(item)
            if fk:
                add(child_t, fk.group(1), fk.group(2), fk.group(3))
                continue
            first = item.split(None, 1)[0].lower() if item.split() else ""
            if first in _DDL_CONSTRAINT_WORDS:
                continue
            col = _DDL_COLUMN_FK.match(item)
            if col:
                add(child_t, col.group(1), col.group(2), col.group(3))
    for am in _DDL_ALTER_FK.finditer(txt):
        add(am.group(1), am.group(2), am.group(3), am.group(4))
    return rels


# Strategy's catalog probe leaves dataType fields it didn't compute as
# INT32_MIN (a "no value" sentinel). When the build flow forwards those into
# the model body, Strategy stores them, and subsequent UI previews fail with
# "DssDataType '4' is invalid or not supported" (and similar codes) — the
# render layer can't map an integer with scale=INT_MIN to a known DssDataType.
# Postgres, MySQL, and SQL Server backends have all been observed to surface
# integer + char/varchar (fixed_length_string) columns with this sentinel.
# See memory/reference_mosaic_publish_path.md (DataType preconditions).
_DATATYPE_INT_MIN_SENTINEL = -2147483648


def _normalize_catalog_datatype(dt: dict | None) -> dict | None:
    """Sanitize INT32_MIN sentinels in a column's dataType so Strategy doesn't
    store them as-is. Idempotent; safe to call on already-clean dataTypes.

    - scale == INT32_MIN  → 0 (universal safe default; integers and strings
      both expect 0 from the engine's POV) -- EXCEPT for `decimal`, see below.
    - precision == INT32_MIN → 0 (rare; only seen on unknown-precision probes)
    - type == 'date' and scale is missing or negative → 0 (the catalog reports
      scale=-1 for dates which some preview paths reject)
    Other fields pass through untouched.

    `decimal` scale is deliberately NOT zeroed here. An INT32_MIN scale on a
    decimal column usually means the warehouse genuinely could not report a
    fixed scale (e.g. an unconstrained Postgres NUMERIC column, whose rows may
    mix whole numbers and fractional ones -- an F1 "points" column with
    half-point rows, like 4820.5, is a real observed case). Zeroing it here
    would make it indistinguishable from a *confirmed* scale of 0, and
    schema_object_translator.normalize_datatype() maps confirmed-scale-0
    decimals to `int64`, which cannot hold a fractional value -- silently
    discarding real data with no error. Leaving the sentinel in place lets
    normalize_datatype() tell "confirmed whole-number decimal" apart from
    "unknown scale" and pick a type that can safely hold either (`double`).
    """
    if not isinstance(dt, dict):
        return dt
    out = dict(dt)
    if out.get("scale") == _DATATYPE_INT_MIN_SENTINEL and out.get("type") != "decimal":
        out["scale"] = 0
    if out.get("precision") == _DATATYPE_INT_MIN_SENTINEL:
        out["precision"] = 0
    if out.get("type") == "date":
        sc = out.get("scale")
        if sc is None or (isinstance(sc, int) and sc < 0):
            out["scale"] = 0
    return out


# Fact-metric number formats as Modeling `format.values[]` tokens. number_category
# enum verified 2026-09-17 against UI-built metrics: 0=Fixed, 1=Currency, 2=Date,
# 3=Time, 4=Percentage, 5=Fraction, 6=Scientific, 7=Custom, 8=Special, 9=General
# (an earlier table used 2 for currency and 7 for scientific, which render as Date
# and Special). See memory/feedback_mosaic_ship_bar.md "Metric formats".
def _fmt_tokens(category: int, decimals: int, pattern: str, *extra: dict) -> list[dict]:
    return [{"type": "number_category", "value": str(category)},
            {"type": "number_decimal_places", "value": str(decimals)},
            {"type": "number_format", "value": pattern}, *extra]


METRIC_FORMAT_PRESETS: dict[str, list[dict]] = {
    "currency":      _fmt_tokens(1, 2, '"$"#,##0.00;"$"-#,##0.00',
                                 {"type": "number_currency_symbol", "value": "$"}),
    "percent":       _fmt_tokens(4, 1, "0.0%"),         # column holds a 0-1 fraction
    "percent_0_100": _fmt_tokens(0, 1, '#,##0.0"%"'),   # column holds 0-100; category 4 would show 9444%
    "integer":       _fmt_tokens(0, 0, "#,##0"),
    "fixed2":        _fmt_tokens(0, 2, "#,##0.00"),
    "scientific":    _fmt_tokens(6, 2, "0.00E+00"),
}
_CURRENCY_NAME_WORDS = ("revenue", "cost", "price", "amount", "spend", "expense", "profit", "usd", "dollar")
_INTEGER_DTYPES = {"integer", "int64"}


def _resolve_metric_format(value: Any, presets: dict | None = None) -> list[dict]:
    """Preset name (built-in or caller-defined) or a raw format.values token list."""
    if isinstance(value, list):
        return [dict(v) for v in value]
    table = {**METRIC_FORMAT_PRESETS, **(presets or {})}
    if value not in table:
        die(f"unknown format preset '{value}'. known: {sorted(table)}")
    return [dict(v) for v in table[value]]


def _default_metric_format(metric_name: str, dt_obj: dict | None, function: str = "sum") -> list[dict]:
    """Format for a newly created fact metric so it never ships with the generic
    default. Percent is deliberately not guessed: a name can't tell a 0-1 column
    from a 0-100 one. Set it with the dictionary `format` key or patch-fact-metrics."""
    name = (metric_name or "").lower()
    if any(w in name for w in _CURRENCY_NAME_WORDS):
        return _resolve_metric_format("currency")
    dtype = str((dt_obj or {}).get("type") or "").lower()
    if dtype in _INTEGER_DTYPES and function in ("sum", "count", "min", "max"):
        return _resolve_metric_format("integer")
    return _resolve_metric_format("fixed2")


def _format_matches(current: dict | None, want: list[dict]) -> bool:
    """True when every wanted format token already has the wanted value."""
    have = {v.get("type"): v.get("value") for v in (current or {}).get("values") or []}
    return all(have.get(t["type"]) == t["value"] for t in want)


def fetch_table_metadata(m: MSTR, ds_id: str, namespace: str, tname: str, *,
                         required: bool = True) -> dict | None:
    """Catalog columns of a warehouse table. required=False returns None instead of
    stopping the run when no describe endpoint answers."""
    ns_id = resolve_namespace_id(m, ds_id, namespace)
    tb_id = encode_tb_id(namespace, tname)
    path, body = m.try_candidates("describe_table", required=required, id=ds_id, ns_id=ns_id, tb_id=tb_id)
    if body is None:
        return None
    cols = body.get("columns") or body.get("physicalTable",{}).get("columns") or []
    # Sanitize the catalog's INT32_MIN sentinel in dataType before any consumer
    # forwards these columns into a model-body create/patch. See
    # _normalize_catalog_datatype above.
    for c in cols:
        if "dataType" in c:
            c["dataType"] = _normalize_catalog_datatype(c.get("dataType"))
    return {"raw": body, "columns": cols, "tb_id": tb_id, "ns_id": ns_id}


def _col_dtype(c: dict) -> str:
    """Return a lowercase string dataType, handling both flat string and nested {type,scale,precision} shapes."""
    dt = c.get("dataType") or c.get("type") or ""
    if isinstance(dt, dict): dt = dt.get("type","")
    return str(dt).lower()


def _looks_like_identifier_col(name: str) -> bool:
    upper = (name or "").upper()
    if upper in BARE_ID_SUFFIX_FALSE_POSITIVES:
        return False
    return any(upper.endswith(suffix) or upper == suffix.lstrip("_") for suffix in ID_COLUMN_SUFFIXES)


def _looks_like_numeric_dimension(name: str) -> bool:
    upper = (name or "").upper()
    return any(token in upper for token in NATURAL_NUMERIC_DIMS)


def classify_columns(cols, attr_override: set[str], metric_override: set[str]):
    attrs, metrics = [], []
    for c in cols:
        name = c.get("name") or c.get("columnName")
        if not name: continue
        if name.lower() in attr_override:
            attrs.append(c); continue
        if name.lower() in metric_override:
            metrics.append(c); continue
        dtype = _col_dtype(c)
        if _looks_like_identifier_col(name) or _looks_like_numeric_dimension(name):
            attrs.append(c)
        elif any(t in dtype for t in NUMERIC_TYPES):
            metrics.append(c)
        else:
            attrs.append(c)
    return attrs, metrics


_STALE_LOCK_HINT = (
    "A prior Modeling Service session left an open changeset on this project. "
    "Run `build_mosaic.py release-locks` to free locks owned by the current "
    "user, then retry. Locks held by other users age out on their own."
)


def _extract_lockid_from_error(body) -> str | None:
    """Strategy 8004cc41 (schemaEdit lock conflict) reports the existing
    lock's id embedded in XML inside additionalProperties.existingLock.comment:
        '<LOCKID>D7FEB354B8D14678B78363BB3964C811</LOCKID>'
    Return the LOCKID or None.
    """
    try:
        errs = (body or {}).get("errors") or []
        if not errs:
            return None
        comment = (
            (errs[0] or {}).get("additionalProperties", {})
            .get("existingLock", {}).get("comment", "")
        )
        match = _re.search(r"<LOCKID>([A-F0-9]+)</LOCKID>", comment or "")
        return match.group(1) if match else None
    except Exception:
        return None


def _lock_owned_by_self(body, my_user_id: str | None) -> bool:
    try:
        errs = (body or {}).get("errors") or []
        if not errs:
            return False
        owner = (
            (errs[0] or {}).get("additionalProperties", {})
            .get("existingLock", {}).get("ownerId", "")
        )
        return bool(my_user_id) and owner == my_user_id
    except Exception:
        return False


def open_cs(m: MSTR, *, schema_edit: bool = False, release_self_locks: bool = False) -> str:
    """Open a Modeling Service changeset.

    schema_edit=False — relationships, security filters, ACLs, post-build edits.
    schema_edit=True  — adding/modifying form expressions on attributes, or
                        otherwise touching the schema graph itself. Using the
                        wrong type silently produces 8004ccde or no-ops the
                        write.

    release_self_locks=True — if open fails with 8004cc41 (schemaEdit lock
                              conflict) and GET /api/model/schema/lock says the
                              current user owns the lock, release it and retry
                              once (see _release_own_lock). Opt-in only: the same
                              account running another job against the project
                              owns that lock too.

    Implementation note: Strategy ignores `schemaEdit: true` in the JSON body
    on this endpoint; it has to be passed as a query param (`?schemaEdit=true`)
    for the lock to actually be acquired. We send both so old and new server
    builds work — Strategy ignores the spare on whichever side doesn't need it.
    """
    body: dict = {}
    path = "/api/model/changesets"
    if schema_edit:
        body["schemaEdit"] = True
        path = "/api/model/changesets?schemaEdit=true"
    r = m.post(path, json=body)
    if not r.ok:
        # Lock-conflict recovery path — 8004cc41 with a lock this user provably owns.
        err_body = _json_or(r, {})
        if release_self_locks and _extract_lockid_from_error(err_body) and _release_own_lock(m, err_body):
            r = m.post(path, json=body)
        if not r.ok:
            hint = ""
            try:
                if "8004cc41" in r.text:
                    hint = f"\n  {_STALE_LOCK_HINT}"
            except Exception:
                pass
            die(f"open_cs: {format_mstr_error(r)}{hint}")
    d = _json_or(r, {})
    cs = d.get("id") or d.get("changesetId", "")
    if not cs:
        die(f"open_cs: {d}")
    m.s.headers["X-MSTR-MS-Changeset"] = cs
    if isinstance(getattr(m, "open_changesets", None), set):
        m.open_changesets.add(cs)
    return cs


def commit_cs(m: MSTR, cs: str):
    try:
        r = m.post(f"/api/model/changesets/{cs}/commit")
    except requests.ReadTimeout:
        # The commit reached the server (connect failures are retried; read timeouts are
        # not) and may still be applying. Discarding it now could race the commit, so stop
        # tracking it and say so instead.
        m.s.headers.pop("X-MSTR-MS-Changeset", None)
        if isinstance(getattr(m, "open_changesets", None), set):
            m.open_changesets.discard(cs)
        die(f"commit {cs} timed out; it may still be applying. Check the model before retrying "
            f"(raise MSTR_HTTP_TIMEOUT for large models); if the changes are missing, "
            f"`build_mosaic.py release-locks` frees a lock left behind.")
    m.s.headers.pop("X-MSTR-MS-Changeset", None)
    if not r.ok:
        discard_cs(m, cs)   # a failed commit leaves the changeset (and any schema lock) open
        if r.status_code == 400 and not (r.text or "").strip():
            # Strategy answers an empty 400 when it will not accept the changeset as a whole: a definition it cannot
            # hold together (an attribute keyed on the wrong table, a form whose columns disagree in type) or a session
            # without an identity token. Nothing in the changeset was kept.
            die(f"commit {cs}: HTTP 400 with no message: Strategy refused the changeset as a whole, so nothing in it was "
                f"kept. Check the plan's findings (attribute keys and their tables, form column types) and build again.")
        die(f"commit {cs}: {format_mstr_error(r)}")
    if isinstance(getattr(m, "open_changesets", None), set):
        m.open_changesets.discard(cs)


def _lock_owner_from_error(body) -> str:
    try:
        errs = (body or {}).get("errors") or []
        return str((((errs[0] or {}).get("additionalProperties") or {}).get("existingLock") or {})
                   .get("ownerId") or "") if errs else ""
    except (AttributeError, TypeError):
        return ""


def _my_user_id(m: MSTR) -> str:
    """The signed-in user's id (GET /api/sessions/userInfo); '' when it can't be read."""
    cached = getattr(m, "_user_id", None)
    if cached is None:
        r = m.get("/api/sessions/userInfo")
        cached = str(_json_or(r, {}).get("id") or "") if r.ok else ""
        try:
            m._user_id = cached
        except AttributeError:
            pass
    return cached


def _release_own_lock(m: MSTR, err_body) -> str | None:
    """Release the stale schema lock named in an 8004cc41 error, but only when the lock is
    provably this user's (GET /api/model/schema/lock owner == GET /api/sessions/userInfo id)
    and is not one of this run's own changesets. Tries DELETE /api/model/changesets/{lockId}
    first, then DELETE /api/model/schema/lock: a committed schemaEdit changeset's lock
    outlives its session, and deleting that changeset from a new session fails (8004cb15).
    Returns the released LOCKID, or None after saying why on stderr.

    Ownership is per account, not per process: another job signed in as the same user owns
    the lock too, so callers make this opt-in."""
    lockid = _extract_lockid_from_error(err_body)
    if not lockid:
        print("  [locks] the lock-conflict error names no LOCKID; nothing released", file=sys.stderr)
        return None
    if lockid in (getattr(m, "open_changesets", None) or set()):
        print(f"  [locks] {lockid} is one of this run's own changesets; not releasing it", file=sys.stderr)
        return None
    me = _my_user_id(m)
    lock_r = m.get("/api/model/schema/lock")
    lock = _json_or(lock_r, {}) if lock_r.ok else {}
    owner = str(lock.get("ownerId") or "") or _lock_owner_from_error(err_body)
    held = _re.search(r"<LOCKID>([A-F0-9]+)</LOCKID>", str(lock.get("comment") or ""))
    if not me or owner != me:
        who = lock.get("ownerName") or owner or "an unknown user"
        print(f"  [locks] lock {lockid} belongs to {who}, not the signed-in user; it ages out on its own",
              file=sys.stderr)
        return None
    print(f"  [locks] releasing lock {lockid} held by this user since {lock.get('dateCreated') or '?'}",
          file=sys.stderr)
    dr = m.delete(f"/api/model/changesets/{lockid}")
    if dr.status_code in (200, 204):
        return lockid
    if held and held.group(1) != lockid:
        print(f"  [locks] the schema lock is now {held.group(1)}, not {lockid}; leaving it", file=sys.stderr)
        return None
    lr = m.delete("/api/model/schema/lock")
    if lr.status_code in (200, 204):
        return lockid
    print(f"  [locks] could not release {lockid}: changeset {format_mstr_error(dr)}; "
          f"schema lock {format_mstr_error(lr)}", file=sys.stderr)
    return None


def format_mstr_error(response, prefix: str = "") -> str:
    """Thin wrapper around mosaic_safety.format_mstr_error so the rest of this
    module can call a one-arg helper without the import-prefix dance. Also
    detects the 8004cb0a session-cap and appends the wait advisory."""
    line = ms.format_mstr_error(response, prefix=prefix)
    if ms.is_session_cap_error(response):
        line += "  ← " + ms.SESSION_CAP_MESSAGE
    return line


def discard_cs(m: MSTR, cs: str) -> None:
    """Best-effort discard of a changeset. Used in error paths. A changeset this run tracks
    but has already committed or discarded is not DELETEd again."""
    if not cs:
        return
    tracked = getattr(m, "open_changesets", None)
    if not isinstance(tracked, set) or cs in tracked:
        try:
            m.delete(f"/api/model/changesets/{cs}")
        except Exception:
            pass
    m.s.headers.pop("X-MSTR-MS-Changeset", None)
    if isinstance(tracked, set):
        tracked.discard(cs)


# ── Relationship safety: merge-aware PUT + join-table preflight ──────────────
#
# WARNING: PUT /api/model/dataModels/{model_id}/attributes/{attr_id}/relationships
# REPLACES every relationship attached to that attribute — in BOTH directions
# (incoming AND outgoing). It is NOT append-only. Issuing it with only your new
# rels silently deletes everything that was wired previously. Always go through
# put_relationships_merged() unless you explicitly intend the destructive wipe.

def get_attribute_relationships(
    m: MSTR, model_id: str, attr_id: str,
) -> list[dict] | None:
    """Read the current set of relationships on a Mosaic attribute. Returns []
    when the attribute has none and None when the read fails or the answer has a
    shape this helper doesn't know — callers must not treat either as "no
    relationships" before a destructive PUT.

    Accepts `relationships` as a list (what tenants return today) or wrapped as
    {"relationships": [...]} (how the spec types ms-DataModelAttribute.relationships)."""
    r = m.get(f"/api/model/dataModels/{model_id}/attributes/{attr_id}")
    if not r.ok:
        if m.verbose:
            print(f"[rel-merge] read {attr_id}: {format_mstr_error(r)}", file=sys.stderr)
        return None
    try:
        body = r.json() if r.text else {}
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    rels = body.get("relationships")
    if isinstance(rels, dict):
        rels = rels.get("relationships")
    if rels is None:
        return []
    if not isinstance(rels, list) or not all(isinstance(x, dict) for x in rels):
        return None
    return rels


def _rel_key(rel: dict) -> tuple[str, str, str]:
    """Stable identity for a relationship row: (parent_id, child_id, rel_table_id).
    Used to dedup when merging existing + new relationships."""
    parent = ((rel.get("parent") or {}).get("objectId")
              or (rel.get("parent") or {}).get("id") or "")
    child = ((rel.get("child") or {}).get("objectId")
             or (rel.get("child") or {}).get("id") or "")
    rtable = ((rel.get("relationshipTable") or {}).get("objectId")
              or (rel.get("relationshipTable") or {}).get("id") or "")
    return (str(parent), str(child), str(rtable))


def put_relationships_merged(
    m: MSTR,
    model_id: str,
    attr_id: str,
    new_rels: list[dict],
    cs_id: str,
    *,
    replace: bool = False,
) -> tuple[bool, int, int, str]:
    """Safely write relationships for an attribute without wiping existing ones.

    Strategy's PUT /attributes/{id}/relationships is destructive — it REPLACES
    the attribute's full relationship set in both directions. This helper
    fetches the existing relationships first, dedupes by (parent, child,
    relationship_table) against `new_rels`, and PUTs the union.

    Set `replace=True` only when you explicitly want the wipe (e.g. cleanup).

    Refuses (no PUT) when a new relationship contradicts the set: its reverse
    (child → parent) already exists or is also being added — the pair would form a
    cycle — or the same parent/child/table already exists with a different
    relationshipType. Those need a deliberate decision (`--replace`), not a merge.

    Returns (ok, added_count, total_count, error_or_empty). `added_count` is
    the number of relationships actually new; `total_count` is the size of the
    final set written.
    """
    if not new_rels:
        return True, 0, 0, ""

    if replace:
        merged = list(new_rels)
        added = len(new_rels)
    else:
        existing = get_attribute_relationships(m, model_id, attr_id)
        if existing is None:
            return (False, 0, 0, f"could not read {attr_id}'s current relationships; refusing a PUT "
                                 "that would replace them")
        problem = _relationship_conflicts(existing, new_rels)
        if problem:
            return False, 0, len(existing), problem
        existing_keys = {_rel_key(r) for r in existing}
        merged = list(existing)
        added = 0
        for rel in new_rels:
            if _rel_key(rel) in existing_keys:
                continue
            merged.append(rel)
            existing_keys.add(_rel_key(rel))
            added += 1

    body = {"relationships": merged}
    r = m.put(f"/api/model/dataModels/{model_id}/attributes/{attr_id}/relationships",
              json=body, headers={"X-MSTR-MS-Changeset": cs_id})
    if not r.ok:
        return False, added, len(merged), format_mstr_error(r)
    return True, added, len(merged), ""


def _relationship_conflicts(existing: list[dict], new_rels: list[dict]) -> str:
    """'' when new_rels merge cleanly into existing; otherwise what contradicts what."""
    def ends(rel):
        key = _rel_key(rel)
        return key[0], key[1]

    have = {}
    for rel in existing:
        have.setdefault(_rel_key(rel), rel.get("relationshipType"))
    pairs = {ends(rel) for rel in existing}
    for rel in new_rels:
        parent, child = ends(rel)
        if parent and parent == child:
            return f"{parent} → {child} relates an attribute to itself (8004ccdb); refusing"
        if (child, parent) in pairs:
            return (f"{parent} → {child} would reverse the existing {child} → {parent}; "
                    "refusing to write a cycle")
        key = _rel_key(rel)
        if key in have and rel.get("relationshipType") and have[key] \
                and rel.get("relationshipType") != have[key]:
            return (f"{parent} → {child} already exists as {have[key]}, not {rel.get('relationshipType')}; "
                    "change it with --replace")
        pairs.add((parent, child))
        have.setdefault(key, rel.get("relationshipType"))
    return ""


# ── Post-build topology validation ───────────────────────────────────────────

def _attribute_expression_table_names(a: dict) -> set[str]:
    """Distinct table names referenced across all of an attribute's
    forms[].expressions[].tables[] -- i.e. every table this attribute is
    directly expressed on, conformed or not. A set with 2+ entries means this
    attribute IS the join path between those tables (the Kimball conformed-
    dimension pattern), independent of whether a formal relationships[] entry
    also exists.
    """
    names: set[str] = set()
    for form in a.get("forms") or []:
        for expr in form.get("expressions") or []:
            for t in expr.get("tables") or []:
                nm = t.get("name")
                if nm:
                    names.add(nm)
    return names


def _attribute_is_isolated(relationships: list, expression_table_names: set[str]) -> bool:
    """An attribute has a valid join path if it has a formal relationship OR
    its expressions span 2+ tables (conformed dimension). Isolated only when
    neither is true. Pure predicate, extracted for direct unit testing."""
    return not relationships and len(expression_table_names) < 2


_PAGE_SIZE = 1000


def _list_all(m: MSTR, path: str, key: str, *, page_size: int = _PAGE_SIZE,
              params: dict | None = None):
    """Every item of a Modeling list endpoint (the `key` array), paging with limit/offset so
    large models are never silently truncated. Stops at the reported `total` when there is one
    (a server may cap its page size below `limit`), otherwise on a short page, and always when
    a page adds nothing new (a server that ignores offset). Returns (items, None), or
    (items_so_far, failed_response)."""
    items: list = []
    seen: set = set()
    offset = 0
    for _ in range(500):
        r = m.get(path, params={**(params or {}), "limit": page_size, "offset": offset})
        for wait in (3, 10, 30):                        # seen live: a model's table list answered 500 once, right after a publish
            if r.ok or r.status_code < 500:
                break
            time.sleep(wait)
            r = m.get(path, params={**(params or {}), "limit": page_size, "offset": offset})
        if not r.ok:
            return items, r
        body = _json_or(r, {})
        page = body.get(key) or body.get("items") or []
        fresh = 0
        for it in page:
            ident = ((it.get("information") or {}).get("objectId") or it.get("id")) if isinstance(it, dict) else None
            if ident is not None:
                if ident in seen:
                    continue
                seen.add(ident)
            items.append(it)
            fresh += 1
        offset += len(page)
        total = body.get("total")
        if not fresh:
            break
        if isinstance(total, int) and not isinstance(total, bool):
            if offset >= total:
                break
        elif len(page) < page_size:
            break
    return items, None


def post_build_validate_topology(
    m: MSTR,
    model_id: str,
    *,
    expected_tables: list[str] | None = None,
    attrs: list | None = None,
    tables: list | None = None,
) -> dict:
    """Return a structured report on model topology health.

    Detects:
      - Isolated attributes: no formal relationships[] AND no multi-table
        expression coverage. Either one alone is a valid join path in
        Mosaic (see reference_mosaic_relationship_archetypes.md /
        feedback_mosaic_relationship_wiring.md) -- a conformed attribute
        whose expressions span 2+ tables joins those tables with no
        relationship object needed. A plain single-table descriptor with
        neither is still reported; whether that's meaningful depends on
        whether the attribute's table is fact-like (this function does not
        currently filter by that -- see NOTE below).
      - Tables present in the model but with zero relationships, or absent
        when `expected_tables` is provided.
      - Numeric attributes whose only expression is on a fact-like table
        (likely misclassified — should probably be metrics).

    NOTE (pre-existing, separate from the fix above): a plain single-table
    dimension descriptor (e.g. "Circuit Country" on just the circuits table)
    is expected to have neither relationships nor multi-table expressions --
    it's a normal, self-contained descriptor, not a broken join. This
    function's own docstring has long said isolated attributes are only
    meaningful "on a fact-like or expected table", but the check does not
    actually scope by that -- every attribute is evaluated regardless of its
    table. In practice this only under-reports noise on schemas where
    _is_fact_like's name-pattern heuristic (FACT/LINEITEM/DETAIL/REL/
    TRANSACTIONS/ACTIVITY/EVENTS) doesn't match the fact tables' actual
    names (it doesn't match "results", "lap_times", "pit_stops", etc. in the
    F1 schema this was tested against), so it wasn't fixed here to avoid
    conflating two independent, differently-evidenced problems in one change.

    Output shape:
      {"model_id": "...",
       "counts": {"attributes": N, "relationships": M, "tables": T},
       "isolated_attributes": [{"id": "...", "name": "...", "table": "..."}],
       "tables_without_relationships": ["..."],
       "missing_expected_tables": ["..."],
       "numeric_attribute_warnings": [{"id": "...", "name": "...", "table": "..."}],
       "ok": bool}

    Any caller can use this — it does not exit the process. Pair with
    cmd_validate_model for CLI usage with non-zero exit-on-failure semantics.
    Pass `attrs` / `tables` when the caller already listed them (saves the reads).
    """
    if attrs is None:
        attrs, failed = _list_all(m, f"/api/model/dataModels/{model_id}/attributes", "attributes")
        if failed is not None:
            return {
                "ok": False,
                "error": format_mstr_error(failed, "topology attributes read"),
            }
    if tables is None:
        tables, _failed = _list_all(m, f"/api/model/dataModels/{model_id}/tables", "tables")

    rel_count = 0
    by_table_rels: dict[str, int] = {}
    isolated: list[dict] = []
    numeric_warnings: list[dict] = []

    lookup_map = ms.attribute_lookup_table_map(attrs)
    lookup_name_map = ms.attribute_table_name_map(attrs)

    for a in attrs:
        info = a.get("information") or {}
        aid = info.get("objectId") or a.get("id")
        nm = info.get("name") or "?"
        rels = a.get("relationships") or []
        rel_count += len(rels)
        table_name = lookup_name_map.get(aid, "")
        if table_name:
            by_table_rels[table_name] = by_table_rels.get(table_name, 0) + len(rels)
        # A conformed attribute (one attribute with forms[].expressions[]
        # spanning multiple tables) is an equally-valid join path in Mosaic --
        # reference_mosaic_relationship_archetypes.md and
        # feedback_mosaic_relationship_wiring.md both document this as the
        # PRIMARY conformance mechanism, with formal relationships[] as the
        # alternative for cases conformance can't express. Checking only
        # `relationships` here (as this used to) flags every correctly-
        # conformed attribute as isolated: verified on a real build where a
        # Driver attribute spanning 7 tables via expressions, with zero
        # relationships[] entries, was reported isolated even though a live
        # Trino rollup query through it returned correct per-driver totals
        # matching the source database exactly.
        expr_tables = _attribute_expression_table_names(a)
        if _attribute_is_isolated(rels, expr_tables):
            isolated.append({"id": aid, "name": nm, "table": table_name})

        # Numeric-attribute heuristic: if the column-name looks numeric/metric-y
        # AND its lookup table is fact-like AND it carries no relationship,
        # flag as likely-misclassified.
        if not rels and _looks_like_metric_name(nm) and _is_fact_like(table_name):
            numeric_warnings.append({"id": aid, "name": nm, "table": table_name})

    declared_tables = {((t.get("information") or {}).get("name") or "")
                       for t in tables}
    declared_tables.discard("")

    # Tables that exist in the model but have zero relationships touching them.
    # Skip dim tables (a single-table dim can legitimately have no rels — the
    # join lives on the fact-table side).
    fact_like_in_model = {n for n in declared_tables if _is_fact_like(n)}
    tables_without_rels = sorted(
        n for n in fact_like_in_model
        if by_table_rels.get(n, 0) == 0
    )

    missing_expected = []
    if expected_tables:
        missing_expected = sorted(set(expected_tables) - declared_tables)

    ok = (not isolated and not tables_without_rels
          and not missing_expected and not numeric_warnings)

    return {
        "model_id": model_id,
        "counts": {
            "attributes": len(attrs),
            "relationships": rel_count,
            "tables": len(tables),
        },
        "isolated_attributes": isolated,
        "tables_without_relationships": tables_without_rels,
        "missing_expected_tables": missing_expected,
        "numeric_attribute_warnings": numeric_warnings,
        "ok": ok,
    }


_METRIC_NAME_HINTS = ("amount", "amt", "total", "qty", "quantity", "price",
                      "cost", "revenue", "paid", "balance", "discount",
                      "net_paid", "sales", "profit", "value")


def _looks_like_metric_name(name: str) -> bool:
    """Heuristic: column/attribute name reads like a measure column."""
    if not name:
        return False
    n = name.lower()
    return any(h in n for h in _METRIC_NAME_HINTS)


def _parse_id_list(raw: str | None) -> list[str]:
    """Parse comma-separated IDs or @filepath of one ID per line."""
    if not raw:
        return []
    if raw.startswith("@"):
        with open(raw[1:], encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return [x.strip() for x in raw.split(",") if x.strip()]


def batch_call(
    m: MSTR,
    model_id: str,
    changeset_id: str,
    ops: list[dict],
    *,
    atomic: bool = True,
) -> tuple[list[dict], list[dict]]:
    """POST /api/model/batch with a list of sub-operations.

    atomic=True  → allowPartialSuccess=false (rollback on any failure).
    atomic=False → allowPartialSuccess=true  (207 Multi-Status, per-op results).

    Returns (passed, failed); each result carries `_index`, the position of its op, or None
    when it can't be told (a result that names no object and doesn't line up with the ops).
    The endpoint is a UI internal absent from the spec, so it is opt-in (create_objects);
    when the tenant has no such route (404/405/501) the ops go out one by one instead.
    """
    allow_partial = "false" if atomic else "true"
    r = m.post(
        f"/api/model/batch?allowPartialSuccess={allow_partial}&showChanges=true",
        headers={"X-MSTR-MS-Changeset": changeset_id},
        json={"operations": ops},
    )
    if r.status_code in (404, 405, 501):
        if m.verbose:
            print(f"[batch] endpoint {r.status_code} — falling back to per-op individual calls",
                  file=sys.stderr)
        return _batch_fallback(m, model_id, changeset_id, ops)
    if r.status_code not in (200, 207, 400):
        raise RuntimeError(f"batch_call HTTP {r.status_code}: {r.text[:400]}")
    body = _json_or(r, {})
    results = body.get("results") or body.get("operations") or body.get("ops") or []
    if r.status_code == 400 and not results:   # atomic batch rejected as a whole
        return [], [{"_index": i, "op": op, "status": 400, "error": str(body)[:400]} for i, op in enumerate(ops)]
    # Map each result to its op by the object name it reports; fall back to position only when
    # the counts line up. A partial-success answer that omits results must not shift IDs onto
    # the wrong objects.
    names: dict[str, int | None] = {}
    for i, op in enumerate(ops):
        name = ((op.get("value") or {}).get("information") or {}).get("name") if isinstance(op, dict) else None
        if name:
            names[name] = None if name in names else i          # duplicate names are ambiguous
    aligned = len(results) == len(ops)
    for i, res in enumerate(results):
        if not isinstance(res, dict) or "_index" in res:
            continue
        resp = res.get("response") if isinstance(res.get("response"), dict) else res
        name = (resp.get("information") or {}).get("name") if isinstance(resp.get("information"), dict) else None
        if name and names.get(name) is not None:
            res["_index"] = names[name]
        else:
            res["_index"] = i if aligned else None
    if not aligned:
        print(f"  WARN batch: {len(results)} result(s) for {len(ops)} op(s); results matched by name only",
              file=sys.stderr)
    passed = [res for res in results if isinstance(res, dict) and 200 <= res.get("status", 500) < 300]
    failed = [res for res in results if res not in passed]
    return passed, failed


def create_objects(m: MSTR, model_id: str, changeset_id: str, ops: list[dict], *,
                   use_batch: bool = False) -> tuple[list[dict], list[dict]]:
    """Create model objects in one changeset: one documented POST per object by default, or
    the UI's undocumented POST /api/model/batch when use_batch is set."""
    if use_batch:
        return batch_call(m, model_id, changeset_id, ops, atomic=False)
    return _batch_fallback(m, model_id, changeset_id, ops)


def _batch_fallback(
    m: MSTR,
    model_id: str,
    changeset_id: str,
    ops: list[dict],
) -> tuple[list[dict], list[dict]]:
    """One documented POST per op (the default write path; also the fallback for tenants
    without /api/model/batch). Results carry `_index`, the op's position."""
    PATH_MAP = {
        "/attributes":  f"/api/model/dataModels/{model_id}/attributes",
        "/factMetrics": f"/api/model/dataModels/{model_id}/factMetrics",
        "/metrics":     f"/api/model/dataModels/{model_id}/metrics",
    }
    passed, failed = [], []
    for index, op in enumerate(ops):
        path_suffix = op.get("path", "")
        endpoint = PATH_MAP.get(path_suffix)
        if not endpoint:
            failed.append({"_index": index, "op": op, "status": 400,
                           "error": f"unknown path {path_suffix}"})
            continue
        r = m.post(endpoint, json=op.get("value", {}),
                   headers={"X-MSTR-MS-Changeset": changeset_id})
        result = {"_index": index, "op": op, "status": r.status_code}
        if r.ok:
            result["response"] = r.json() if r.text else {}
            passed.append(result)
        else:
            result["error"] = r.text[:400]
            failed.append(result)
    return passed, failed


def _make_pipeline_table_body(
    m: MSTR,
    ds_id: str,
    schema: str,
    tname: str,
) -> dict | None:
    """Fetch warehouse table metadata and return the physicalTable pipeline body
    used by POST /api/model/dataModels/{id}/tables. Returns None if the table
    cannot be fetched. Datatypes are normalized via schema_object_translator
    so the resulting model is publishable in-memory.
    """
    md = fetch_table_metadata(m, ds_id, schema, tname, required=False)
    if md is None:
        return None
    cols_raw = md.get("columns", [])
    outer_cols, pipe_cols = [], []
    for c in cols_raw:
        cname = c.get("name") or c.get("columnName")
        dt = c.get("dataType")
        if isinstance(dt, str):
            dt = {"type": dt}
        if not isinstance(dt, dict):
            dt = {"type": "utf8_char"}
        dt = sot.normalize_datatype(dt)
        outer_cols.append({
            "information": {"name": cname},
            "dataType": dt,
            "columnName": cname,
        })
        pipe_cols.append({
            "id": new_uuid(),
            "name": cname,
            "dataType": dt,
            "sourceDataType": dt,
        })
    pipeline_obj = {
        "id": new_uuid(),
        "rootTable": {
            "id": new_uuid(),
            "type": "root",
            "children": [{
                "id": new_uuid(),
                "name": tname,
                "type": "source",
                "columns": pipe_cols,
                "importSource": {
                    "type": "single_table",
                    "dataSourceId": ds_id,
                    "namespace": schema,
                    "tableName": tname,
                    "sql": "",
                },
            }],
        },
    }
    return {
        "information": {"name": tname},
        "physicalTable": {
            "columns": outer_cols,
            "type": "pipeline",
            "pipeline": json.dumps(pipeline_obj),
        },
    }


def _apply_conformance_map(dictionary: dict, path: str) -> None:
    """Merge a conformance-map file into the dictionary's attributes block.

    File shape (JSON or YAML) — one entry per logical attribute:
        {"<Logical Name>": ["<TABLE>.<COLUMN>", "<TABLE>.<COLUMN>", ...]}

    Each table.column listed will receive an attributes entry with the shared
    `name`, which triggers the conformance-via-identical-name grouping at build
    time (see feedback_mosaic_relationship_wiring.md step 2). Existing dictionary
    entries keep their description; only `name` is overwritten.
    """
    if not path: return
    data = load_structured_file(path) or {}
    if not isinstance(data, dict):
        die(f"--conformance-map: {path} must be a mapping of logical_name → [table.col, …]")
    attrs = dictionary.setdefault("attributes", {})
    added = 0
    for logical, cols in data.items():
        if not isinstance(cols, list): continue
        for col in cols:
            key = str(col).strip()
            if not key: continue
            entry = attrs.get(key) or {}
            entry["name"] = logical
            attrs[key] = entry
            added += 1
    if added:
        print(f"→ --conformance-map: applied {added} TABLE.COLUMN → logical-name overrides",
              file=sys.stderr)


def _apply_fk_map(dictionary: dict, path: str) -> None:
    """Merge an fk-map file: maps child FKs to their parent PK so differently-named
    columns conform to the parent's logical attribute.

    File shape (JSON or YAML):
        {"<CHILD_TABLE>.<CHILD_COL>": "<PARENT_TABLE>.<PARENT_COL>", ...}

    For each mapping, the child column inherits the parent column's logical name
    (resolved via the dictionary's existing attributes entry, falling back to
    title-cased parent column name if no entry exists yet). Relationships still
    need to be declared separately if the conformance grouping doesn't join the
    tables on its own.
    """
    if not path: return
    data = load_structured_file(path) or {}
    if not isinstance(data, dict):
        die(f"--fk-map: {path} must be a mapping of child.col → parent.col")
    attrs = dictionary.setdefault("attributes", {})
    added = 0
    for child, parent in data.items():
        child_key  = str(child).strip()
        parent_key = str(parent).strip()
        if not (child_key and parent_key): continue
        parent_entry = attrs.get(parent_key) or {}
        parent_name = parent_entry.get("name")
        if not parent_name:
            # Fall back to the parent's column name, title-cased (mirrors the
            # helper's default inference). Keep simple.
            parent_col = parent_key.rsplit(".", 1)[-1]
            parent_name = parent_col.replace("_", " ").title()
        child_entry = attrs.get(child_key) or {}
        child_entry["name"] = parent_name
        attrs[child_key] = child_entry
        # Also set the parent's name if it's not already set, so the two sides
        # conform on the same string.
        if not parent_entry.get("name"):
            parent_entry["name"] = parent_name
            attrs[parent_key] = parent_entry
        added += 1
    if added:
        print(f"→ --fk-map: applied {added} child→parent FK conformance hint(s)",
              file=sys.stderr)


def resolve_dest_folder(m: "MSTR", dest: str) -> str:
    """Return a usable destinationFolderId for create-model.

    When none is supplied, fall back to the user's personal "My Objects"
    (reference_mosaic_rest_gotchas.md). Some tenants' /api/folders/myPersonalObjects
    returns the folder object (usable id); others return its contents — there we
    can't derive the id, so fail fast with an actionable message rather than POST
    an empty destinationFolderId (the cryptic 8004cc07 "folder '' invalid")."""
    if dest:
        return dest
    try:
        r = m.get("/api/folders/myPersonalObjects")
        if r.ok:
            body = r.json()
            if isinstance(body, dict) and body.get("id"):
                print(f"  no dest folder set — using My Objects ({body['id']})", file=sys.stderr)
                return body["id"]
    except Exception:
        pass
    die("No destination folder. Set MSTR_DEST_FOLDER_ID (or config destination_folder / "
        "--dest-folder) to a writable folder id — find one with "
        "`GET /api/folders/preDefined/7`. (Auto-fallback to My Objects unavailable here: "
        "this tenant's /api/folders/myPersonalObjects returns folder contents, not the "
        "folder id.)")


# Identify each table's "entity key" column: the column whose prefix matches the
# table's singular form (PRODUCTS -> PRODUCT_*, OPPORTUNITIES -> OPPORTUNITY_*).
# Fallback: the first *_ID column. Hoisted to module level (out of cmd_build's
# closure) so it's independently unit-testable -- see
# tests/test_build_mosaic_classification.py.
def _strip_prefix(tname: str) -> str:
    return _re.sub(r"^[A-Z]+(?:_[A-Z]+)*_\d{6,}(?:_\d+)*_", "", tname)


def _entity_prefix(tname: str) -> str:
    # Uppercase before the plural-stripping checks below: they compare against
    # literal uppercase suffixes ("IES", "S", ...), so a lowercase table name
    # (the normal case for Postgres, e.g. "drivers") previously never matched
    # any of them and was returned still-plural ("DRIVERS" instead of
    # "DRIVER") after the final .upper() in _entity_candidates. That silently
    # broke entity-key detection for every lowercase-named table, independent
    # of the separate underscore-suffix gap fixed in _find_entity_key.
    base = _strip_prefix(tname).split("_")[-1].upper()
    if base.endswith("IES"): return base[:-3] + "Y"
    if base.endswith("SES"): return base[:-2]
    if base.endswith("S") and len(base) > 2: return base[:-1]
    return base


def _entity_candidates(tname: str) -> list[str]:
    """Possible PK-column prefixes for a table: singular, acronym, full-compound-singular.

    Also includes the table name UN-singularized. _entity_prefix's plural
    stripping is a blunt "ends in S -> strip it" heuristic and gets English's
    many already-singular-but-ends-in-s nouns wrong (status, bias, campus,
    atlas, virus, corps, ...) -- e.g. "status" -> "STATU", so "STATUSID"
    would never even be tried as a candidate. Rather than hand-maintain a
    denylist of exceptions (as done for the *_ID suffix ambiguity in
    _looks_like_identifier_col), just try both forms and let
    _find_entity_key's suffix search be the arbiter of which one is real:
    at most one candidate can match an actual column, so trying an extra
    wrong guess costs nothing.
    """
    stripped = _strip_prefix(tname)
    words = stripped.split("_")
    cands = [_entity_prefix(tname).upper(), stripped.upper()]
    if len(words) >= 2:
        # acronym: first letter of each word, e.g. PURCHASE_ORDERS -> PO
        cands.append("".join(w[0] for w in words if w).upper())
        # all words smashed together with no separator at all, e.g.
        # driver_standings -> DRIVERSTANDINGS (+ID -> DRIVERSTANDINGSID).
        # Same compact-naming convention as the single-word case (Finding 2
        # above), just for a multi-word table name -- the Ergast schema's
        # driver_standings/constructor_standings/constructor_results tables
        # all use this exact pattern for their own surrogate key.
        cands.append("".join(words).upper())
    return list(dict.fromkeys(cands))


def _find_entity_key(tname: str, names: list[str]) -> str | None:
    """Find the entity/primary-key column for `tname` among its column `names`
    (already upper-cased). Tries underscored suffixes first (DRIVER_ID, the
    more explicit convention), then the compact no-separator form (DRIVERID),
    which is common in public/Kaggle-style datasets (e.g. the Ergast F1 schema:
    driverid, raceid, constructorid). Before this fallback existed, compact
    naming was never matched, silently disabling entity-key detection --
    and therefore cross-table conformance -- for every table in such a schema.
    """
    for pref in _entity_candidates(tname):
        for suf in ["_ID", "_NUMBER", "_KEY", "_NO", "ID", "NUMBER", "KEY", "NO"]:
            target = f"{pref}{suf}"
            if target in names:
                return target
    return None


def _add_table_expression_to_form(forms: list[dict], tid: str, tname: str, cname: str) -> bool:
    """Add a (table, column) expression to the ID-category form in `forms`
    (mutates the form's expressions list in place), unless `tname` is already
    covered. Pure/no I/O -- separated out so the merge logic used by both the
    conform-by-name path and the post-build merge-attributes command is
    independently unit-testable without mocking the REST session.

    Returns True if the form now covers `tname` (whether just added or
    already present), False if `forms` has no usable ID-category form.
    """
    idform = next((f for f in forms if f.get("category") == "ID"), forms[0] if forms else None)
    if idform is None:
        return False
    exprs = idform.setdefault("expressions", [])
    if any(t.get("name") == tname for e in exprs for t in e.get("tables", [])):
        return True
    exprs.append({
        "expression": {"tokens": [{"type": "column_reference", "value": cname}]},
        "tables": [{"objectId": tid, "subType": "logical_table", "name": tname}],
    })
    return True


def cmd_build(m: MSTR, args):
    m.login(identity=True)
    sources = [parse_source(s) for s in args.source]
    if args.instance and args.schema and args.tables:
        sources.append((args.instance, args.schema, args.tables))
    if not sources:
        die("provide at least one --source INSTANCE:SCHEMA:T1,T2 or --instance/--schema/--tables")

    # Load optional overrides / ERD
    dictionary = load_dictionary(getattr(args,"dictionary",None))
    _apply_conformance_map(dictionary, getattr(args, "conformance_map", None))
    _apply_fk_map(dictionary, getattr(args, "fk_map", None))
    explicit_rels = list(dictionary.get("relationships",[]))
    for erd_path in (getattr(args,"erd",[]) or []):
        explicit_rels.extend(load_erd(erd_path))
    if explicit_rels:
        print(f"→ Using {len(explicit_rels)} explicit relationships from dictionary/ERD "
              f"(added to the inferred ones; an explicit relationship wins over an inferred one between the same "
              f"two attributes).", file=sys.stderr)

    # Resolve instance IDs and fetch table metadata
    print(f"→ Resolving {len(sources)} source(s)…", file=sys.stderr)
    hydrated = []
    for inst_name, schema, tables in sources:
        ds_id = resolve_instance_id(m, inst_name)
        for t in tables:
            md = fetch_table_metadata(m, ds_id, schema, t)
            print(f"  {inst_name}/{schema}.{t}: {len(md['columns'])} cols", file=sys.stderr)
            hydrated.append({"instance_id": ds_id, "instance_name": inst_name,
                             "schema": schema, "table": t, "metadata": md})

    attr_override  = {c.lower() for c in (args.attr_cols or [])}
    metric_override = {c.lower() for c in (args.metric_cols or [])}
    # Columns to exclude from modeling entirely (the plan's kind:skip). Accepts
    # either "TABLE.COLUMN" or bare "COLUMN" (case-insensitive). They stay physical
    # on the table; they just don't become attributes or metrics.
    skip_set = {c.lower() for c in (getattr(args, "skip_cols", None) or [])}

    # Everything the post-build steps can fail on — security-filter syntax and members, ACL
    # rights and trustee types, translation entries — is checked now, before any write, so a
    # bad spec can't leave a half-configured model behind.
    post_build = _prepare_post_build(m, args)

    # ── Create model ──
    print(f"→ Creating model '{args.name}'…", file=sys.stderr)
    dest_folder_id = resolve_dest_folder(m, args.dest_folder)   # may die(); before any changeset
    failures = {"tables": 0, "attributes": 0, "displays": 0, "metrics": 0, "relationships": 0,
                "post_build": 0}
    cs = open_cs(m)
    r = m.post("/api/model/dataModels", json={
        "information": {"name": args.name, "destinationFolderId": dest_folder_id},
        "dataServeMode": args.data_serve_mode,
    })
    model_id = (_json_or(r, {}).get("information") or {}).get("objectId") if r.ok else None
    if not model_id: die(f"create model: {r.status_code} {r.text[:400]}")
    print(f"  model_id={model_id}", file=sys.stderr)

    # ── Add tables (pipeline/importSource shape) ──
    table_id_map = {}   # (inst_id, schema, table) -> logical_table_id
    table_cols_map = {} # same -> [cols]
    for h in hydrated:
        cols_raw = h["metadata"]["columns"]
        # Outer physicalTable.columns shape: {information:{name}, dataType, columnName}
        outer_cols = []
        # Inner pipeline columns shape: {id, name, dataType, sourceDataType}
        pipe_cols  = []
        for c in cols_raw:
            cname = c.get("name") or c.get("columnName")
            dt    = c.get("dataType")
            if isinstance(dt, str): dt = {"type": dt}
            if not isinstance(dt, dict): dt = {"type": "utf8_char"}
            # Publishable-dataType mapping — without this, warehouse-catalog
            # sentinel types stall the in-memory publish (-2147212544); see
            # memory/reference_mosaic_publish_path.md "DataType preconditions".
            dt = sot.normalize_datatype(dt)
            outer_cols.append({
                "information": {"name": cname},
                "dataType": dt,
                "columnName": cname,
            })
            pipe_cols.append({
                "id": new_uuid(),
                "name": cname,
                "dataType": dt,
                "sourceDataType": dt,
            })
        pipeline_obj = {
            "id": new_uuid(),
            "rootTable": {
                "id": new_uuid(),
                "type": "root",
                "children": [{
                    "id": new_uuid(),
                    "name": h["table"],
                    "type": "source",
                    "columns": pipe_cols,
                    "importSource": {
                        "type": "single_table",
                        "dataSourceId": h["instance_id"],
                        "namespace": h["schema"],
                        "tableName": h["table"],
                        "sql": "",
                    },
                }],
            },
        }
        body = {
            "information": {"name": h["table"]},
            "physicalTable": {
                "columns": outer_cols,
                "type": "pipeline",
                "pipeline": json.dumps(pipeline_obj),
            },
        }
        r = m.post(f"/api/model/dataModels/{model_id}/tables", json=body)
        new_tid = (_json_or(r, {}).get("information") or {}).get("objectId") if r.ok else None
        if not new_tid:
            print(f"  WARN add table {h['table']}: {r.status_code} {r.text[:400]}", file=sys.stderr)
            failures["tables"] += 1
            continue
        key = (h["instance_id"], h["schema"], h["table"])
        table_id_map[key] = new_tid
        table_cols_map[key] = cols_raw
        print(f"  + table {h['table']} -> {new_tid}", file=sys.stderr)

    # ── Exclude skip_cols from modeling (plan kind:skip). The columns were already
    #    added to the physical table above; here we just drop them from the source
    #    that drives attribute + metric creation, so they are never modeled. ──
    if skip_set:
        def _is_skipped(tname, cname):
            cl = (cname or "").lower()
            return cl in skip_set or f"{tname.lower()}.{cl}" in skip_set
        for key in list(table_cols_map):
            _, _, tname = key
            kept = [c for c in table_cols_map[key]
                    if not _is_skipped(tname, c.get("name") or c.get("columnName"))]
            n_drop = len(table_cols_map[key]) - len(kept)
            if n_drop:
                print(f"  skip_cols: excluded {n_drop} column(s) from {tname} (not modeled)",
                      file=sys.stderr)
            table_cols_map[key] = kept

    # ── Per-column attribute + metric creation (entity-first pattern) ──
    # This matches how MSTR Mosaic UI builds models: one "entity" attribute per table
    # identity, with text-form expressions on every table that carries the same column.
    # Non-key columns become single-table attributes with a parent→child relationship
    # to their table's entity attribute.

    # Pre-pass: column → [(table_name, table_id), ...]
    col_tables = {}
    for key, tid in table_id_map.items():
        _, _, tname = key
        for c in table_cols_map[key]:
            cn = (c.get("name") or c.get("columnName") or "").strip()
            if cn:
                dt = c.get("dataType") or {}
                col_tables.setdefault(cn, []).append({"table": tname, "table_id": tid, "dtype": dt})

    table_entity_col = {}
    for key in table_id_map:
        _, _, tname = key
        names = [((c.get("name") or c.get("columnName") or "").upper()) for c in table_cols_map[key]]
        pk = _find_entity_key(tname, names)
        if not pk:
            # Fallback: any *_ID / *_NUMBER / *_KEY not on the NOISE list
            for n in names:
                if any(n.endswith(s) for s in ["_ID","_NUMBER","_KEY"]) and n != "SOURCE_SYSTEM":
                    pk = n; break
        table_entity_col[key] = pk

    # Decide the "home" table (lookup) for each shared column:
    #   - If only one table carries it -> that table.
    #   - Else if one of the tables uses it as entity PK -> that table.
    #   - Else first occurrence.
    col_home = {}
    for col, occ in col_tables.items():
        if len(occ) == 1:
            col_home[col] = occ[0]; continue
        for o in occ:
            for key, pk in table_entity_col.items():
                if pk == col.upper() and key[2] == o["table"]:
                    col_home[col] = o; break
            if col in col_home: break
        if col not in col_home: col_home[col] = occ[0]

    # Skip auto-creating attributes for "noise" columns like SOURCE_SYSTEM that
    # appear in every table but aren't a real dimension.
    NOISE_COLS = {"SOURCE_SYSTEM","LOAD_TIMESTAMP","LAST_UPDATED_AT","INGESTION_DATE",
                  "LOAD_DATE","ETL_BATCH_ID","DW_CREATED_AT","DW_UPDATED_AT"}
    SKIP_AS_ATTRIBUTE = {c for c in col_tables if c.upper() in NOISE_COLS and len(col_tables[c]) >= 3}

    # Compute entity PK set early (conformed-dim detection needs it)
    entity_pks = {c for c in (table_entity_col.values()) if c}

    # Snowflake schema support: CONFORMED DIMENSIONS.
    # A column that appears in ≥2 tables, is NOT any table's PK, and isn't noise,
    # is likely a conformed dimension (e.g., REGION in both SUPPLIERS and CUSTOMERS).
    # Create ONE multi-table attribute for it (rather than per-table duplicates).
    entity_pk_set = {p.upper() for p in entity_pks if p}
    conformed_cols = set()
    for col, occ in col_tables.items():
        if len(occ) < 2: continue
        if col.upper() in entity_pk_set: continue
        if col.upper() in NOISE_COLS:    continue
        # If the column is numeric in any occurrence, skip (it's probably a metric)
        is_numeric = False
        for o in occ:
            dt = o.get("dtype") or {}
            dtype_s = (dt.get("type") if isinstance(dt, dict) else str(dt)).lower()
            if any(t in dtype_s for t in NUMERIC_TYPES):
                is_numeric = True; break
        if is_numeric: continue
        conformed_cols.add(col)

    # Entity attributes (one per shared-key column): multi-table expressions.
    entity_attr_of_table = {}   # tname -> attr_id (the PK attr whose lookup is tname)
    created_attrs = {}          # column_lower -> list of {id, table, table_id, name, role: "entity"|"descriptor"}
    total_attrs = total_metrics = 0

    # Case-insensitive "TABLE.COLUMN" lookups, built once (first spelling wins, as before).
    attr_overrides: dict = {}
    for k, v in dictionary["attributes"].items():
        attr_overrides.setdefault(str(k).lower(), v)
    metric_overrides: dict = {}
    for k, v in dictionary["metrics"].items():
        metric_overrides.setdefault(str(k).lower(), v)

    def _dict_override(dkey):
        return attr_overrides.get(dkey.lower()) or {}

    def _patch_displays(aid: str, fids: list) -> None:
        """Report and browse displays on the attribute's forms; a failure leaves blank labels."""
        if not fids:
            return
        rd = m.patch(f"/api/model/dataModels/{model_id}/attributes/{aid}",
                     json={"displays": {"reportDisplays": [{"id": f} for f in fids],
                                        "browseDisplays": [{"id": f} for f in fids]}})
        if not rd.ok:
            print(f"    WARN displays on attribute {aid}: {format_mstr_error(rd)}", file=sys.stderr)
            failures["displays"] += 1

    # Every attribute created below, by its resolved name: a later column the dictionary gives the same name joins it
    # (conformance by identical name) instead of POSTing a duplicate, which Studio refuses with 8004e409. Entity keys
    # and conformed dimensions register here too, so a differently named FK (EPICOR_PRODUCT_ID named "Product")
    # conforms to the entity attribute built from PRODUCT_ID.
    attrs_by_resolved_name: dict[str, dict] = {}   # lower(resolved name) -> {"id","table_id"}

    def _append_expression_to_attribute(aid: str, tid: str, tname: str, cname: str) -> bool:
        """Add a (table, column) expression to an existing attribute's ID form.
        Must fetch with showExpressionAs=tokens first -- without it, pre-existing
        expressions come back as read-only `text` with no `tokens`/`tree`, and
        PATCHing the form back as-is fails with 8004ccde "the tree or token is
        required for expression"."""
        rr = m.get(f"/api/model/dataModels/{model_id}/attributes/{aid}?showExpressionAs=tokens")
        if not rr.ok:
            print(f"    WARN conform-by-name GET {aid}: {rr.status_code} {rr.text[:200]}", file=sys.stderr)
            return False
        cur = rr.json()
        forms = cur.get("forms", [])
        if not _add_table_expression_to_form(forms, tid, tname, cname):
            return False
        rp = m.patch(f"/api/model/dataModels/{model_id}/attributes/{aid}", json={"forms": forms})
        if not rp.ok:
            print(f"    WARN conform-by-name PATCH {aid}: {rp.status_code} {rp.text[:200]}", file=sys.stderr)
            return False
        return True

    # 1) Create entity attributes for each unique PK column, expressions on all occurrences
    # Build a case-insensitive reverse map so Postgres (lowercase) col names match UPPERCASE entity_pks.
    col_tables_upper = {c.upper(): c for c in col_tables}
    for pk_col in sorted(entity_pks):          # sorted: the same build every run
        actual_col = col_tables_upper.get(pk_col)
        if actual_col is None: continue
        # Every table carrying the key in any case (Snowflake SHIPMENT_ID, Postgres shipment_id), each
        # expression naming its own table's column: the relationships below match case-insensitively,
        # and a relationship through a table the attribute is not on is refused (HTTP 400).
        occs = [dict(o, col=c) for c in sorted(col_tables) if c.upper() == pk_col.upper() for o in col_tables[c]]
        home = col_home[actual_col]
        # Pick a short-friendly name — e.g. "PRODUCT_ID" -> "Product"
        base_entity_name = friendly_col(pk_col).replace(" Id","").replace(" ID","").strip() or friendly_col(pk_col)
        # Override with dictionary (home-table key)
        ov = _dict_override(f"{home['table']}.{pk_col}")
        name = ov.get("name") or base_entity_name
        desc = ov.get("description") or f"Unique {friendly_col(pk_col)} identifier; key of the {friendly_table(home['table'])} entity."
        expressions = [
            {"expression": {"tokens":[{"type":"column_reference","value": o["col"]}]},
             "tables": [{"objectId": o["table_id"], "subType":"logical_table", "name": o["table"]}]}
            for o in occs
        ]
        attr_body = {
            "information": {"name": name, "description": desc},
            "forms": [{
                "id": FORM_ID, "category": "ID", "type": "system",
                "displayFormat": "text", "name": f"{friendly_col(pk_col)}",
                "expressions": expressions,
                "lookupTable": {"objectId": home["table_id"], "subType":"logical_table", "name": home["table"]},
            }],
            "keyForm": {"id": FORM_ID},
            "attributeLookupTable": {"objectId": home["table_id"], "subType":"logical_table", "name": home["table"]},
        }
        r = m.post(f"/api/model/dataModels/{model_id}/attributes", json=attr_body)
        resp = _json_or(r, {}) if r.ok else {}
        aid = (resp.get("information") or {}).get("objectId")
        if not aid:
            print(f"    WARN entity attr {name}: {r.status_code} {r.text[:200]}", file=sys.stderr)
            failures["attributes"] += 1
            continue
        _patch_displays(aid, [f["id"] for f in resp.get("forms",[]) if f.get("id")])
        entity_attr_of_table[home["table"]] = aid
        attrs_by_resolved_name[name.lower()] = {"id": aid, "table_id": home["table_id"]}
        created_attrs[pk_col.lower()] = [{"id": aid, "table": home["table"],
                                          "table_id": home["table_id"], "name": name, "role":"entity"}]
        total_attrs += 1
        print(f"  + entity attr '{name}' on {len(occs)} tables (lookup={friendly_table(home['table'])})", file=sys.stderr)

    # 1b) Conformed dimensions: one multi-table attribute per shared descriptor column.
    for col in sorted(conformed_cols):
        occs = col_tables[col]
        home = occs[0]   # first occurrence wins for lookup
        name = friendly_col(col)
        desc = f"Conformed dimension: {name} (shared across {', '.join(friendly_table(o['table']) for o in occs)})."
        ov = _dict_override(f"{home['table']}.{col}")
        if ov.get("name"):        name = ov["name"]
        if ov.get("description"): desc = ov["description"]
        same = attrs_by_resolved_name.get(name.lower())
        if same:                     # the dictionary names it like an entity built above: conform, never duplicate
            joined = [o for o in occs if _append_expression_to_attribute(same["id"], o["table_id"], o["table"], col)]
            if len(joined) < len(occs):
                failures["attributes"] += 1
            created_attrs[col.lower()] = [{"id": same["id"], "table": o["table"], "table_id": o["table_id"], "name": name, "role": "conformed_by_name"} for o in joined]
            print(f"  + conformed-by-name '{name}' += {col} on {len(joined)} tables", file=sys.stderr)
            continue
        expressions = [
            {"expression": {"tokens":[{"type":"column_reference","value": col}]},
             "tables": [{"objectId": o["table_id"], "subType":"logical_table", "name": o["table"]}]}
            for o in occs
        ]
        attr_body = {
            "information": {"name": name, "description": desc},
            "forms": [{
                "id": FORM_ID, "category": "ID", "type": "system",
                "displayFormat": "text", "name": friendly_col(col),
                "expressions": expressions,
                "lookupTable": {"objectId": home["table_id"], "subType":"logical_table", "name": home["table"]},
            }],
            "keyForm": {"id": FORM_ID},
            "attributeLookupTable": {"objectId": home["table_id"], "subType":"logical_table", "name": home["table"]},
        }
        r = m.post(f"/api/model/dataModels/{model_id}/attributes", json=attr_body)
        resp = _json_or(r, {}) if r.ok else {}
        aid = (resp.get("information") or {}).get("objectId")
        if not aid:
            print(f"    WARN conformed {name}: {r.status_code} {r.text[:200]}", file=sys.stderr)
            failures["attributes"] += 1
            continue
        _patch_displays(aid, [f["id"] for f in resp.get("forms",[]) if f.get("id")])
        attrs_by_resolved_name[name.lower()] = {"id": aid, "table_id": home["table_id"]}
        created_attrs[col.lower()] = [{"id": aid, "table": home["table"],
                                        "table_id": home["table_id"], "name": name, "role":"conformed"}]
        total_attrs += 1
        print(f"  + conformed dim '{name}' spans {len(occs)} tables", file=sys.stderr)

    # 2) Descriptor attributes: one per remaining column, single-table --
    # UNLESS the dictionary gives this table.column the same `name` as an
    # attribute already created earlier in this loop (from a different
    # table, possibly a differently-named column), in which case the
    # documented conformance-by-identical-name behavior applies: append an
    # expression to the EXISTING attribute instead of POSTing a duplicate.
    # (feedback_mosaic_relationship_wiring.md step 2 describes this as the
    # mechanism for conforming semantically-same-but-differently-named FKs,
    # e.g. "primary_customer_id" and "customer_id" both given name:
    # "Customer" -- verified against a real build that this previously did
    # NOT happen: the second occurrence 400'd as a duplicate attribute name
    # and was silently dropped, not merged.)

    for key, tid in table_id_map.items():
        inst_id, schema, tname = key
        cols = table_cols_map[key]
        attrs, metrics = classify_columns(cols, attr_override, metric_override)
        short_table = friendly_table(tname)
        for c in attrs:
            cname = c.get("name") or c.get("columnName")
            if not cname: continue
            if cname.upper() in entity_pks:      continue   # handled as entity above
            if cname in SKIP_AS_ATTRIBUTE:       continue
            if cname in conformed_cols:          continue   # handled as conformed above
            base = friendly_col(cname)
            name = base
            desc = f"{base} from the {short_table} table."
            ov = _dict_override(f"{tname}.{cname}")
            if ov.get("name"): name = ov["name"]
            if ov.get("description"): desc = ov["description"]

            existing = attrs_by_resolved_name.get(name.lower())
            if existing:
                if _append_expression_to_attribute(existing["id"], tid, tname, cname):
                    created_attrs.setdefault(cname.lower(), []).append(
                        {"id": existing["id"], "table": tname, "table_id": tid,
                         "name": name, "role": "conformed_by_name"})
                    print(f"  + conformed-by-name '{name}' += {tname}.{cname}", file=sys.stderr)
                else:
                    failures["attributes"] += 1      # the column is not modeled at all
                continue

            attr_body = {
                "information": {"name": name, "description": desc},
                "forms": [{
                    "id": FORM_ID, "category": "ID", "type": "system",
                    "displayFormat": "text", "name": f"{base} ID",
                    "expressions": [{
                        "expression": {"tokens":[{"type":"column_reference","value": cname}]},
                        "tables": [{"objectId": tid, "subType":"logical_table", "name": tname}],
                    }],
                    "lookupTable": {"objectId": tid, "subType":"logical_table", "name": tname},
                }],
                "keyForm": {"id": FORM_ID},
                "attributeLookupTable": {"objectId": tid, "subType":"logical_table", "name": tname},
            }
            r = m.post(f"/api/model/dataModels/{model_id}/attributes", json=attr_body)
            resp = _json_or(r, {}) if r.ok else {}
            aid = (resp.get("information") or {}).get("objectId")
            if not aid:
                print(f"    WARN attr {tname}.{cname}: {r.status_code} {r.text[:200]}", file=sys.stderr)
                failures["attributes"] += 1
                continue
            _patch_displays(aid, [f["id"] for f in resp.get("forms",[]) if f.get("id")])
            attrs_by_resolved_name[name.lower()] = {"id": aid, "table_id": tid}
            created_attrs.setdefault(cname.lower(), []).append(
                {"id": aid, "table": tname, "table_id": tid, "name": name, "role":"descriptor"})
            total_attrs += 1

        # Metrics (text-form expression, dictionary overrides)
        for c in metrics:
            cname = c.get("name") or c.get("columnName")
            dt_obj = c.get("dataType")
            if not isinstance(dt_obj, dict):
                dt_obj = {"type": str(dt_obj or "double"), "precision": 15, "scale": 4}
            dt_obj = sot.normalize_datatype(dt_obj)
            base = friendly_col(cname)
            metric_name = f"Total {base}"
            metric_desc = f"SUM of {cname} from the {short_table} table."
            metric_func = "sum"
            metric_fmt = None
            v = metric_overrides.get(f"{tname}.{cname}".lower())
            if v:
                if v.get("name"):        metric_name = v["name"]
                if v.get("description"): metric_desc = v["description"]
                if v.get("function"):    metric_func = v["function"].lower()
                if v.get("format"):      metric_fmt = _resolve_metric_format(v["format"])
            if not metric_fmt:
                metric_fmt = _default_metric_format(metric_name, dt_obj, metric_func)
            metric_body = {
                "information": {"name": metric_name, "description": metric_desc},
                "fact": {
                    "dataType": dt_obj,
                    "expressions": [{
                        "expression": {"tokens":[{"type":"column_reference","value": cname}]},
                        "tables": [{"objectId": tid, "subType":"logical_table","name": tname}],
                    }],
                    "extensions": [], "entryLevel": [],
                },
                "function": metric_func,
                "functionProperties": [{"name":"UseLookupForAttributes","value":{"type":"boolean","value":"false"}}],
                "dimty": {
                    "dimtyUnits":[{"dimtyUnitType":"report_base_level","aggregation":"normal",
                                   "filtering":"apply","groupBy":True}],
                    "excludeAttribute": False, "allowAddingUnit": True,
                },
                "format": {"header":[], "values": metric_fmt},
            }
            r = m.post(f"/api/model/dataModels/{model_id}/factMetrics", json=metric_body)
            if r.status_code == 400:
                # Format tokens are cosmetic; never lose the metric over them. Only a 400
                # (request rejected, nothing created) is safe to retry — a 5xx or timeout
                # may have created the metric already.
                print(f"    WARN metric {metric_name}: create with format failed ({r.status_code}); "
                      f"retrying without format", file=sys.stderr)
                metric_body["format"]["values"] = []
                r = m.post(f"/api/model/dataModels/{model_id}/factMetrics", json=metric_body)
            if not r.ok:
                print(f"    WARN metric {metric_name}: {r.status_code} {r.text[:200]}", file=sys.stderr)
                failures["metrics"] += 1
                continue
            total_metrics += 1

    # (No auto-created hierarchy object: the data-model hierarchy endpoints this used to call are
    # not in the spec and answered 404 8004cc04; relationships alone drive drilling.)
    print("→ Committing model changeset…", file=sys.stderr)
    commit_cs(m, cs)
    print(f"[build] model {model_id} committed", file=sys.stderr)

    # ── Relationships (entity-first pattern) ──
    # 1) descriptor → entity: within each table, every descriptor attribute is a parent
    #    of the table's entity attribute (MSTR canonical "lookup" shape).
    # 2) entity A → entity B: whenever entity A's key column exists on B's table, B is a fact of A.
    inferred_rels = []
    # entity_attr_of_table: table_name -> entity_attr_id
    entity_attr_by_table_id = {info["table_id"]: info
                               for entries in created_attrs.values() for info in entries
                               if info.get("role") == "entity"}
    # Descriptor → entity
    for key, tid in table_id_map.items():
        _, _, tname = key
        # find entity attr whose lookup table is this table
        entity = next((info for entries in created_attrs.values() for info in entries
                       if info.get("role")=="entity" and info["table"]==tname), None)
        if not entity: continue
        for entries in created_attrs.values():
            for info in entries:
                if info.get("role")=="descriptor" and info["table"]==tname:
                    inferred_rels.append((info, entity, info["name"], tid, "one_to_many"))
    # Entity → entity (fact tables reference dim entities)
    for ent_col, entries in created_attrs.items():
        if not entries or entries[0].get("role") != "entity": continue
        parent = entries[0]
        occs = col_tables.get(ent_col.upper(), []) + col_tables.get(ent_col, [])
        seen_tables = set()
        for o in occs:
            tname = o["table"]
            if tname == parent["table"] or tname in seen_tables: continue
            seen_tables.add(tname)
            # Find the fact table's entity attribute (its own PK)
            child = next((info for entries2 in created_attrs.values() for info in entries2
                          if info.get("role")=="entity" and info["table"]==tname), None)
            if not child or child["id"] == parent["id"]: continue
            inferred_rels.append((parent, child, f"{parent['name']}→{child['name']}",
                                   o["table_id"], "one_to_many"))
    # Deduplicate; drop the reverse of a pair already planned (two tables carrying each
    # other's keys) — writing both would make a cycle, which the merge helper refuses for
    # the whole child.
    seen = set(); pairs = set(); deduped = []
    for p,c,label,rt,rty in inferred_rels:
        sig = (p["id"], c["id"], rt)
        if sig in seen: continue
        if (c["id"], p["id"]) in pairs:
            print(f"  WARN skipped {label}: the reverse relationship is already planned "
                  f"(the two tables carry each other's keys)", file=sys.stderr)
            continue
        seen.add(sig); pairs.add((p["id"], c["id"])); deduped.append((p,c,label,rt,rty))
    inferred_rels = deduped

    # Explicit (ERD/dictionary) relationships join the inferred ones; where both relate the same two attributes, in
    # either direction, the explicit one wins. They used to replace inference wholesale, so a single explicit
    # hierarchy (Category → Product) silently dropped every descriptor → key and key → fact relationship.
    if explicit_rels:
        def _find_attr(ref: str):
            t, _, col = (ref or "").partition(".")
            entries = created_attrs.get(col.lower(), [])
            for entry in entries:
                if entry["table"].lower() == t.lower(): return entry
            # Another table's attribute stands in only when it IS that column everywhere (an
            # entity key or a conformed attribute spanning the tables). A same-named descriptor
            # on a different table is a different attribute — wiring it would be wrong.
            shared = [e for e in entries if e.get("role") in ("entity", "conformed")]
            return shared[0] if shared else None
        inferred_kept = inferred_rels
        inferred_rels = []
        unmapped = []
        for rel in explicit_rels:
            p = _find_attr(rel.get("parent",""))
            c = _find_attr(rel.get("child",""))
            if not (p and c) or p["id"] == c["id"]:
                unmapped.append(f"{rel.get('parent')} → {rel.get('child')}"
                                + (" (one attribute)" if p and c else ""))
                continue
            rtbl_name = rel.get("relationship_table") or c["table"]
            rtbl_id = next((tid for (_,_,tn),tid in table_id_map.items()
                            if tn.lower()==rtbl_name.lower()), c["table_id"])
            inferred_rels.append((p, c, rel.get("parent"), rtbl_id, rel.get("type","one_to_many")))
        if unmapped:
            print(f"  WARN {len(unmapped)} explicit relationship(s) don't map to two model attributes "
                  f"(table or column not modeled here, or both ends are one attribute): "
                  f"{'; '.join(unmapped[:5])}{' …' if len(unmapped) > 5 else ''}", file=sys.stderr)
        explicit_pairs = {frozenset((p["id"], c["id"])) for p, c, *_ in inferred_rels}
        inferred_rels += [rel for rel in inferred_kept if frozenset((rel[0]["id"], rel[1]["id"])) not in explicit_pairs]

    rels_ok = 0
    if inferred_rels and not args.skip_relationships:
        print(f"→ Setting {len(inferred_rels)} relationships…", file=sys.stderr)
        try:
            cs2 = open_cs(m)
            # The relationships PUT replaces the child's whole relationship set (both
            # directions), so one PUT per relationship keeps only the last one. Group by
            # child and write each child once, merged with what is already there.
            by_child: dict[str, list] = {}
            for parent, child, label, rtbl_id, rtype in inferred_rels:
                by_child.setdefault(child["id"], []).append((parent, child, label, {
                    "parent":{"objectId":parent["id"],"subType":"attribute"},
                    "child":{"objectId":child["id"],"subType":"attribute"},
                    "relationshipType": rtype,
                    "relationshipTable":{"objectId": rtbl_id,"subType":"logical_table"},
                }))
            for child_id, items in by_child.items():
                ok, _added, _total, err = put_relationships_merged(
                    m, model_id, child_id, [rel for *_, rel in items], cs2)
                if ok:
                    rels_ok += len(items)
                    for parent, child, label, _ in items:
                        print(f"  {parent['table']}→{child['table']} [{label}]", file=sys.stderr)
                else:
                    failures["relationships"] += len(items)
                    print(f"  WARN rels into {items[0][1]['name']}: {err}", file=sys.stderr)
            commit_cs(m, cs2)
        except (SystemExit, requests.RequestException) as e:
            # The model is committed; report the lost relationships instead of exiting without
            # a summary (the run's `finally` discards the changeset if it is still open). Nothing
            # in an uncommitted changeset survives, so every relationship counts as failed.
            failures["relationships"] = len(inferred_rels)
            rels_ok = 0
            print(f"  WARN relationships not written: "
                  f"{type(e).__name__ if isinstance(e, requests.RequestException) else 'see FATAL above'}",
                  file=sys.stderr)

    summary = {
        "ok": True,
        "model_id": model_id,
        "failures": failures,
        "url": f"{m.base}/app/library#/model/{model_id}",
        "tables": len(table_id_map),
        "attributes": total_attrs,
        "metrics": total_metrics,
        "inferred_relationships": rels_ok,
        "inferred_relationships_attempted": len(inferred_rels),
        "conformed_dimensions": len(conformed_cols),
        "post_build": {},
        "data_validation": {
            "status": "not_run",
            "required": True,
            "reason": "Data correctness validation is reference-dependent. Choose a trusted comparator such as another Mosaic model, a classic report/model, warehouse SQL, a flat file, or a REST fixture before marking the build shippable.",
        },
    }

    def step(label: str, fn) -> bool:
        """One post-build step. A failure is recorded and the build goes on, so the summary —
        with the model id — is always printed."""
        try:
            ok = bool(fn())
        except (SystemExit, requests.RequestException) as e:
            ok = False
            print(f"  WARN {label}: "
                  f"{type(e).__name__ if isinstance(e, requests.RequestException) else 'stopped (see FATAL above)'}",
                  file=sys.stderr)
        summary["post_build"][label] = "ok" if ok else "failed"
        if not ok:
            failures["post_build"] += 1
        return ok

    # ── Security filters (validated and members resolved before the model was created) ──
    for prepared in post_build["security_filters"]:
        step(f"security filter {prepared['name']}", lambda p=prepared: _create_security_filter(m, model_id, p))
    # ── ACL grants ──
    if args.grant or getattr(args, "deny", None):
        step("acl", lambda: _apply_acl(m, model_id, args.grant, model_id=model_id, sub_type="report_emma_cube",
                                       denies=getattr(args, "deny", [])))
    # ── Translations ──
    if args.translate:
        def _translate():
            done, wanted = _apply_translations(m, model_id, args.translate)
            return done == wanted
        step("translations", _translate)
    # ── Publish (for in_memory) — not over a model whose structure failed to build ──
    if args.data_serve_mode == "in_memory" and args.publish:
        core = failures["tables"] + failures["attributes"] + failures["metrics"] + failures["relationships"]
        if core:
            summary["post_build"]["publish"] = "skipped: the model has build failures"
            summary["published"] = False
        else:
            summary["published"] = step("publish", lambda: _publish(m, model_id, skip_classify=True,
                                                                  poll_seconds=getattr(args, "publish_wait", 180)) or True)
    # ── Certify — last, and only a model that built cleanly: certified models are what the
    #    Mosaic MCP server offers to agents ──
    if args.certify:
        if any(failures.values()):
            summary["post_build"]["certify"] = "skipped: the build had failures"
            failures["post_build"] += 1
        else:
            step("certify", lambda: _certify(m, model_id))

    summary["ok"] = not any(failures.values())
    print(json.dumps(summary, indent=2))
    if not summary["ok"]:
        print(f"[build] finished with failures: {failures} — see the WARN lines above.", file=sys.stderr)
        sys.exit(1)


def _prepare_post_build(m: MSTR, args) -> dict:
    """Check every post-build spec of a build before anything is created: security-filter
    syntax and exact member resolution, ACL entries (rights, conflicts, trustees that exist),
    translation entries. Dies on the first problem."""
    prepared = {"security_filters": [_prepare_security_filter(m, sf)
                                     for sf in (getattr(args, "security_filter", None) or [])]}
    changes = _acl_changes(getattr(args, "grant", None) or [], getattr(args, "deny", None) or [])
    for tid, ace in changes.items():
        if not ace.get("subType"):
            _trustee_subtype(m, tid)                      # dies when the trustee can't be typed
    _parse_translation_entries(getattr(args, "translate", None) or [])
    return prepared


# ── Lifecycle / governance ops ────────────────────────────────────────────────
def _is_mstr_id(value: str) -> bool:
    return len(value or "") == 32 and all(c in "0123456789ABCDEFabcdef" for c in value)


def _items_from_response(body: Any, *keys) -> list:
    if isinstance(body, list):
        return body
    if not isinstance(body, dict):
        return []
    for key in keys:
        value = body.get(key)
        if isinstance(value, list):
            return value
    for key in ("items", "result", "results", "users", "objects"):
        value = body.get(key)
        if isinstance(value, list):
            return value
    return []


def _shape_user_candidate(raw: dict, source: str, input_value: str) -> dict:
    info = raw if isinstance(raw, dict) else {}
    return {k: v for k, v in {
        "input": input_value,
        "id": info.get("id") or info.get("objectId"),
        "username": info.get("username") or info.get("abbreviation"),
        "name": info.get("name"),
        "fullName": info.get("fullName"),
        "email": info.get("defaultEmailAddress") or info.get("email"),
        "type": info.get("type"),
        "subtype": info.get("subtype") or info.get("subType"),
        "source": source,
    }.items() if v not in (None, "")}


def _user_candidate_matches(candidate: dict, value: str) -> bool:
    needle = (value or "").strip().lower()
    if not needle:
        return False
    for key in ("id", "username", "name", "fullName", "email"):
        cur = str(candidate.get(key) or "").strip().lower()
        if cur and (cur == needle or needle in cur):
            return True
    return False


def _dedupe_by_id(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        sig = item.get("id") or json.dumps(item, sort_keys=True)
        if sig in seen:
            continue
        seen.add(sig)
        out.append(item)
    return out


def _resolve_member_candidates(m: MSTR, name_or_id: str, limit: int = 10) -> list[dict]:
    """Resolve user/group-ish inputs to candidate user records. Read-only."""
    raw = (name_or_id or "").strip()
    if not raw:
        return []

    if _is_mstr_id(raw):
        r = m.get(f"/api/users/{raw.upper()}")
        if r.ok:
            return [_shape_user_candidate(r.json(), "users/{id}", raw)]
        return [{"input": raw, "id": raw.upper(), "source": "literal_id"}]

    terms = [raw]
    if "@" in raw:
        terms.append(raw.split("@", 1)[0])
    candidates = []

    for term in list(dict.fromkeys(t for t in terms if t)):
        for param_name in ("nameBegins", "abbreviationBegins"):
            r = m.get("/api/users", params={param_name: term, "limit": limit})
            if not r.ok:
                continue
            for item in _items_from_response(r.json(), "users"):
                cand = _shape_user_candidate(item, f"users?{param_name}", raw)
                if _user_candidate_matches(cand, raw) or _user_candidate_matches(cand, term):
                    candidates.append(cand)

        # Search fallback is useful on tenants where /users is restricted but
        # Quick Search exposes owner metadata for objects the person owns.
        r = m.get("/api/searches/results", params={
            "name": term,
            "getAncestors": "false",
            "limit": limit,
        })
        if r.ok:
            for item in _items_from_response(r.json(), "result"):
                owner = item.get("owner") or {}
                if not owner:
                    continue
                cand = _shape_user_candidate(owner, "search.owner", raw)
                if _user_candidate_matches(cand, raw) or _user_candidate_matches(cand, term):
                    candidates.append(cand)

    return _dedupe_by_id(candidates)


def _exact_member_matches(candidates: list[dict], raw: str) -> list[dict]:
    needle = raw.strip().lower()
    return _dedupe_by_id([c for c in candidates if c.get("id") and any(
        str(c.get(k) or "").strip().lower() == needle for k in ("id", "username", "name", "fullName", "email"))])


def _json_any(r) -> Any:
    """Parsed JSON body of any shape, or None when the body is empty or not JSON."""
    try:
        return r.json() if getattr(r, "text", "x") else None
    except ValueError:
        return None


def _principal_kind(m: MSTR, object_id: str) -> str | None:
    """'user' or 'user_group' for an existing principal id; None when both lookups say it
    doesn't exist; 'unknown' when this account may not read users or groups (401/403), so
    existence can't be checked."""
    ru = m.get(f"/api/users/{object_id}")
    if ru.ok:
        return "user"
    rg = m.get(f"/api/usergroups/{object_id}")
    if rg.ok:
        return "user_group"
    if ru.status_code in (401, 403) or rg.status_code in (401, 403):
        return "unknown"
    return None


def _resolve_member_ids(m: MSTR, names_or_ids: list[str]) -> list[str]:
    """Resolve user / user-group names to IDs. A security filter bound to the wrong
    account leaves the intended one unrestricted, so only an exact match on id / login /
    full name / email that is unique across users AND user groups counts (a user and a
    group with the same name is ambiguous), and a literal ID must exist as a user or a
    group; anything else stops the run."""
    ids, problems = [], []
    for raw in names_or_ids:
        name = raw.strip()
        if not name:
            continue
        if _is_mstr_id(name):
            kind = _principal_kind(m, name.upper())
            if kind == "unknown":
                print(f"  WARN member {name}: this account can't read users/groups, so the ID is used "
                      f"as given — check it binds the intended principal", file=sys.stderr)
            if kind:
                ids.append(name.upper())
            else:
                problems.append(f"'{name}' is not the ID of a user or user group")
            continue
        candidates = _resolve_member_candidates(m, name, limit=25)
        exact = _exact_member_matches(candidates, name)
        r = m.get("/api/usergroups", params={"nameBegins": name, "limit": 25})
        if not r.ok:
            print(f"  WARN member '{name}': user groups could not be searched ({r.status_code}); "
                  f"a group with the same name would go unnoticed — pass IDs to be sure", file=sys.stderr)
        groups = _items_from_response(_json_any(r), "usergroups") if r.ok else []
        exact = _dedupe_by_id(exact + [{"id": g.get("id"), "name": g.get("name"), "type": "user_group"}
                                       for g in groups if isinstance(g, dict) and g.get("id")
                                       and (g.get("name") or "").strip().lower() == name.lower()])
        if len(exact) == 1:
            ids.append(exact[0]["id"])
        elif exact:
            problems.append(f"'{name}' matches {len(exact)} users/groups "
                            f"({', '.join(c['id'] for c in exact[:5])}); pass the ID")
        else:
            near = ", ".join(str(c.get("username") or c.get("name") or c.get("id")) for c in candidates[:5])
            problems.append(f"'{name}' has no exact user or group match" + (f" (similar: {near})" if near else ""))
    if problems:
        die("security-filter members not resolved — refusing to bind a filter to a guessed account: "
            + "; ".join(problems))
    return list(dict.fromkeys(ids))


def _assign_security_filter_members(m: MSTR, model_id: str, sf_id: str, member_ids: list[str]) -> bool:
    """Bind members to a Mosaic security filter. The PATCH path is "/Members" on the tenant
    family this was verified against and "/members" in the spec, so a 400 on the first is
    retried with the second. False (and a WARN) when neither binds."""
    if not member_ids:
        return False
    url = f"/api/dataModels/{model_id}/securityFilters/{sf_id}/members"
    errors = []
    for op_path in ("/Members", "/members"):
        r = m.patch(url, json={"operationList": [{"op": "addElements", "path": op_path, "value": member_ids}]})
        if r.ok:
            return True
        errors.append(f"{op_path}: {format_mstr_error(r)}")
        if r.status_code != 400:
            break
    print(f"  WARN security-filter members: PATCH {' | '.join(errors)}", file=sys.stderr)
    return False


def _normalize_mosaic_security_filter_qualification(value: Any) -> dict:
    """Return a Mosaic security-filter qualification object.

    Accepts either {"qualification": {...}} or the qualification object itself.
    """
    if not isinstance(value, dict):
        die("Mosaic security filter qualification must be a JSON object")
    if isinstance(value.get("qualification"), dict):
        value = value["qualification"]
    if isinstance(value.get("tree"), dict):
        return value
    die("Mosaic security filter qualification must contain a top-level 'tree' object")


def _infer_constant(value: str) -> dict:
    text = str(value).strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    if _re.fullmatch(r"-?\d+", text):
        return {"parameterType": "constant", "constant": {"type": "int32", "value": text}}
    if _re.fullmatch(r"-?(?:\d+\.\d*|\.\d+)(?:[eE][+-]?\d+)?|-?\d+[eE][+-]?\d+", text):
        return {"parameterType": "constant", "constant": {"type": "double", "value": text}}
    return {"parameterType": "constant", "constant": {"type": "string", "value": text}}


def _parse_mosaic_security_filter_qualification(raw: str) -> dict:
    """Parse the Mosaic-only qualification half of --security-filter.

    Supported minimal form:
      <attributeId>[:<formId>]=<constant>

    For anything richer, pass @path/to/qualification.json where the file contains
    either {"qualification": {...}} or {"tree": {...}}. Classic/project security
    filters use a different endpoint family and should not call this helper.
    """
    qual = (raw or "").strip()
    if not qual:
        die("Mosaic security filter requires a qualification after NAME=")
    if qual.startswith("@"):
        return _normalize_mosaic_security_filter_qualification(load_structured_file(qual[1:]))
    if qual.startswith("{"):
        try:
            payload = json.loads(qual)
        except json.JSONDecodeError as exc:
            die(f"Mosaic security filter JSON qualification is invalid: {exc}")
        return _normalize_mosaic_security_filter_qualification(payload)

    left, sep, value = qual.partition("=")
    if sep and value:
        attr, _, form = left.strip().partition(":")
        if _is_mstr_id(attr) and (not form or _is_mstr_id(form)):
            return {"tree": {
                "type": "predicate_form_qualification",
                "predicateTree": {
                    "function": "equals",
                    "attribute": {"objectId": attr.upper(), "subType": "attribute"},
                    "form": {"objectId": (form.upper() if form else FORM_ID),
                             "subType": "attribute_form_system"},
                    "parameters": [_infer_constant(value)],
                },
            }}

    die("Mosaic security filter qualification must be @file.json, JSON, or ATTR_ID[:FORM_ID]=VALUE. "
        "Classic/project security filters use /api/model/securityFilters and /api/securityFilters/{id}/members.")


def _security_filter_parts(spec) -> tuple[str, Any, list[str]]:
    """(name, qualification, member names) from a CLI spec 'NAME=QUALIFICATION|USER,USER' or
    a build-from-config mapping {name, qualification (object or string), members (list or
    comma string)}."""
    if isinstance(spec, dict):
        members = spec.get("members") or []
        if isinstance(members, str):
            members = members.split(",")
        return str(spec.get("name") or "").strip(), spec.get("qualification"), [str(u) for u in members]
    parts = str(spec).split("|", 1)
    nq, users = parts[0], (parts[1].split(",") if len(parts) > 1 else [])
    name, _, qual = nq.partition("=")
    return name.strip(), qual, users


def _prepare_security_filter(m: MSTR, spec) -> dict:
    """Every check a security filter can fail — name, qualification syntax, exact member
    resolution — with no writes, so a build can run it before creating anything."""
    name, qual, users = _security_filter_parts(spec)
    if not name:
        die(f"security filter {spec!r}: a name is required (NAME=QUALIFICATION|USERS)")
    if qual in (None, ""):
        die(f"security filter '{name}': a qualification is required")
    qualification = (_normalize_mosaic_security_filter_qualification(qual) if isinstance(qual, dict)
                     else _parse_mosaic_security_filter_qualification(str(qual)))
    member_ids = _resolve_member_ids(m, users) if any(u.strip() for u in users) else []
    return {"name": name, "qualification": qualification, "member_ids": member_ids}


def _create_security_filter(m: MSTR, model_id: str, prepared: dict) -> bool:
    """Create a prepared Mosaic security filter, then bind its members. True only when the
    filter exists and every requested member is bound: a filter without its members
    restricts no one, which is the failure that matters."""
    name = prepared["name"]
    cs = open_cs(m)
    r = m.post(f"/api/model/dataModels/{model_id}/securityFilters",
               json={"information": {"name": name, "subType": "md_security_filter"},
                     "qualification": prepared["qualification"],
                     "topLevel": [], "bottomLevel": []})
    sf_id = (_json_or(r, {}).get("information") or {}).get("objectId") if r.ok else None
    if not sf_id:
        discard_cs(m, cs)
        die(f"security filter '{name}': {format_mstr_error(r) if not r.ok else 'no objectId in the response'}")
    commit_cs(m, cs)
    print(f"  ✓ security filter '{name}' -> {sf_id}", file=sys.stderr)
    if not prepared["member_ids"]:
        return True
    if _assign_security_filter_members(m, model_id, sf_id, prepared["member_ids"]):
        print(f"    {len(prepared['member_ids'])} member(s) bound", file=sys.stderr)
        return True
    print(f"  WARN security filter '{name}' ({sf_id}) exists but its members are NOT bound, so they "
          f"are unrestricted until they are: PATCH /api/dataModels/{model_id}/securityFilters/{sf_id}/members",
          file=sys.stderr)
    return False


def _apply_security_filter(m: MSTR, model_id: str, spec) -> bool:
    """Create a Mosaic data-model security filter and optionally assign members.
    Returns False when the filter was created but its members could not be bound.

    This is not the classic/project security-filter helper. Classic filters are
    created through /api/model/securityFilters and assigned through
    /api/securityFilters/{id}/members.
    """
    return _create_security_filter(m, model_id, _prepare_security_filter(m, spec))


# EnumDSSXMLAccessRightFlags (the bit order of the spec's ms-EnumAccessRight). The usual
# bundles: view = browse+read+use+execute (197), modify = view+write+delete (221), full = 255.
_RIGHT_FLAGS = {"browse":1, "use_execute":2, "read":4, "write":8, "delete":16, "control":32,
                "use":64, "execute":128, "view":197, "modify":221, "full":255, "full_control":255}

def _rights_mask(rights: str) -> int:
    mask = 0
    for right in rights.split(","):
        right = right.strip().lower()
        if not right:
            continue
        if right.isdigit():
            mask |= int(right)
        elif right in _RIGHT_FLAGS:
            mask |= _RIGHT_FLAGS[right]
        else:
            die(f"unknown access right '{right}'. use {', '.join(_RIGHT_FLAGS)} or a number")
    return mask


_TRUSTEE_SUBTYPES = ("user", "user_group")


def _parse_acl_entries(entries: list[str], mode: str) -> dict[str, dict]:
    """Parse 'trusteeId:rights[:user|user_group]' specs into {trustee: {"granted", "denied",
    "subType"}}. subType is None unless the spec names it (the current ACL or a lookup fills
    it in later). Dies on a malformed spec, an unknown right or an unknown trustee type."""
    acl = {}
    for entry in entries or []:
        parts = entry.split(":")
        if len(parts) < 2:
            die(f"bad ACL spec '{entry}'. expected trusteeId:rights[:user|user_group]")
        trustee_id, rights = parts[0].strip(), parts[1].strip()
        trustee_subtype = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
        if trustee_subtype is not None and trustee_subtype not in _TRUSTEE_SUBTYPES:
            die(f"bad ACL spec '{entry}'. the trustee type must be user or user_group")
        mask = _rights_mask(rights)
        if not trustee_id or not mask:
            die(f"bad ACL spec '{entry}'. trustee and non-empty rights are required")
        cur = acl.setdefault(trustee_id, {"granted": 0, "denied": 0, "subType": None})
        if trustee_subtype:
            cur["subType"] = trustee_subtype
        if mode == "deny":
            cur["denied"] |= mask
        else:
            cur["granted"] |= mask
    return acl


def _acl_changes(grants: list[str], denies: list[str] | None) -> dict[str, dict]:
    """Grants and denies per trustee from the command line; dies when one command both grants
    and denies the same right to the same trustee."""
    changes = _parse_acl_entries(grants, "grant")
    for tid, ace in _parse_acl_entries(denies or [], "deny").items():
        cur = changes.setdefault(tid, {"granted": 0, "denied": 0, "subType": None})
        cur["denied"] |= ace["denied"]
        cur["subType"] = ace["subType"] or cur["subType"]
    for tid, ace in changes.items():
        clash = ace["granted"] & ace["denied"]
        if clash:
            die(f"ACL for {tid}: rights mask {clash} is both granted and denied")
    return changes


def _trustee_subtype(m: MSTR, tid: str) -> str:
    """user / user_group for an ACL trustee given without a type; dies when it can't be told."""
    kind = _principal_kind(m, tid)
    if kind in _TRUSTEE_SUBTYPES:
        return kind
    reason = ("this account can't read users or groups" if kind == "unknown"
              else "it is neither a user nor a user group")
    die(f"ACL trustee {tid}: {reason}; pass it as {tid}:<rights>:user or {tid}:<rights>:user_group")


def _merge_acl(current: dict, changes: dict[str, dict], *, replace: bool = False) -> dict:
    """The ACL to PATCH (the body is the whole ACL): `current` with each named trustee updated.
    By default the named rights are granted (or denied) and every other right the trustee had
    is kept; granting a right clears it from the trustee's deny set and vice versa. With
    replace=True a named trustee's entry becomes exactly the given rights. Trustees that
    aren't named, their names and their subtypes are left as they are."""
    merged = {tid: dict(ace) for tid, ace in (current or {}).items() if isinstance(ace, dict)}
    for tid, change in changes.items():
        old = merged.get(tid, {})
        grant, deny = change["granted"], change["denied"]
        if replace:
            granted, denied = grant, deny
        else:
            granted = (int(old.get("granted") or 0) | grant) & ~deny
            denied = (int(old.get("denied") or 0) | deny) & ~grant
        entry = {k: v for k, v in old.items() if k not in ("granted", "denied", "subType")}
        entry.update({"granted": granted, "denied": denied,
                      "subType": change.get("subType") or old.get("subType")})
        merged[tid] = entry
    return merged


def _apply_acl(m: MSTR, object_id: str, grants: list[str], model_id=None,
               sub_type: str = "report_emma_cube", denies=None, *, replace: bool = False) -> bool:
    """Apply ACLs.

    Data-model-contained objects use the Modeling endpoint:
      PATCH /api/model/dataModels/{modelId}/objects/{objectId}/acl?subType=<objectSubType>

    The model root takes subType=report_emma_cube (logical_table fails with
    8004e403; the classic POST /api/objects/{id}/acl returns 404 for a model).

    Entry syntax: 'trusteeId:rights[:user|user_group]'. Rights can be names
    (read,browse,execute,...) or numeric masks. Changes merge into the current ACL
    (see _merge_acl); a trustee new to the ACL without a type is looked up.
    """
    changes = _acl_changes(grants, denies)
    if not changes:
        return True
    if not model_id:
        # Objects outside a data model: the REST API has no /api/objects/{id}/acl; classic ACLs
        # are read with GET /api/objects/{id}?type=<t> and written with PUT on the same path.
        # Not wrapped yet — say so instead of calling an endpoint that 404s.
        die(f"set-acl without --model-id (a classic object) is not wrapped yet. Read the ACL with "
            f"`api-call --path /api/objects/{object_id} --param type=<objectType>` and write it with "
            f"PUT on the same path (see memory/reference_mosaic_acl.md).")

    acl_path = f"/api/model/dataModels/{model_id}/objects/{object_id}/acl?subType={sub_type}"
    cur = m.get(acl_path)
    if not cur.ok:
        die(f"read current ACL of {object_id}: {format_mstr_error(cur)}")
    acl = _merge_acl(_json_or(cur, {}).get("acl") or {}, changes, replace=replace)
    for tid in changes:
        if not acl[tid].get("subType"):
            acl[tid]["subType"] = _trustee_subtype(m, tid)
    cs = open_cs(m)
    r = m.patch(acl_path, json={"acl": acl})
    if not r.ok:
        discard_cs(m, cs)
        die(f"data model ACL on {object_id}: {format_mstr_error(r)}")
    commit_cs(m, cs)
    print(f"  ✓ ACL set on {object_id} ({len(changes)} trustee(s) changed, {len(acl)} in the ACL)",
          file=sys.stderr)
    return True


# The model root's subtype in the spec's ms-EnumObjectSubType; "data_model" is not in it.
_SUBTYPE_ALIASES = {"data_model": "report_emma_cube"}


def _parse_translation_entries(entries: list[str], default_sub_type: str = "report_emma_cube") -> dict:
    """{(objectId, subType): PATCH body} from 'objectId[:subType]:locale[:name|description]=text'
    entries. Dies on a malformed entry."""
    by_obj: dict = {}
    for entry in entries or []:
        left, sep, text = entry.partition("=")
        parts = left.split(":")
        if not sep:
            parts = []
        if len(parts) == 2:
            obj, loc = parts
            sub_type, field = default_sub_type, "name"
        elif len(parts) == 3:
            obj, sub_type, loc = parts
            field = "name"
        elif len(parts) >= 4:
            obj, sub_type, loc, field = parts[:4]
        else:
            die(f"bad translation entry '{entry}'. expected objectId[:subType]:locale[:field]=text")
        if field not in {"name", "description"}:
            die(f"bad translation field '{field}'. use name or description")
        sub_type = _SUBTYPE_ALIASES.get(sub_type, sub_type)
        body = by_obj.setdefault((obj, sub_type), {})
        body.setdefault(field, {"translationValues": {}})
        body[field]["translationValues"][loc] = {"translation": text}
    return by_obj


def _apply_translations(m: MSTR, model_id: str, entries: list[str],
                        default_sub_type: str = "report_emma_cube") -> tuple[int, int]:
    """Apply name/description translations for data-model-contained objects.
    Returns (objects updated, objects requested).

    Entry syntax:
      objectId:locale=text
      objectId:subType:locale=text
      objectId:subType:locale:description=text

    The default subType is the model root's (report_emma_cube; "data_model" is accepted as
    an alias). Locale keys can be numeric Strategy locale IDs (for example 1033) or the
    locale tokens accepted by the tenant. Field defaults to name.
    """
    by_obj = _parse_translation_entries(entries, _SUBTYPE_ALIASES.get(default_sub_type, default_sub_type))
    if not by_obj:
        return 0, 0

    if model_id and model_id != "_":
        cs = open_cs(m)
        ok = 0
        for (obj, sub_type), body in by_obj.items():
            r = m.patch(f"/api/model/dataModels/{model_id}/objects/{obj}/translations?subType={sub_type}",
                        json=body)
            if r.ok:
                ok += 1
            else:
                print(f"  WARN translate {obj}: {format_mstr_error(r)}", file=sys.stderr)
        commit_cs(m, cs)
        print(f"  ✓ translations updated for {ok}/{len(by_obj)} objects", file=sys.stderr)
        return ok, len(by_obj)

    print("  WARN translate: --model-id is required for data model object translations", file=sys.stderr)
    return 0, len(by_obj)


def _certify(m: MSTR, object_id: str, obj_type: int = 3) -> bool:
    """PUT /api/objects/{id}/certify?type=3&certify=true (the documented call; a Mosaic
    model is type 3). The Mosaic MCP server lists and resolves certified models only."""
    r = m.put(f"/api/objects/{object_id}/certify", params={"type": obj_type, "certify": "true"})
    if r.ok:
        print(f"  ✓ certified {object_id}", file=sys.stderr)
        return True
    print(f"  WARN certify {object_id}: {format_mstr_error(r)}", file=sys.stderr)
    return False


def classify_object_surface(m: MSTR, object_id: str) -> dict:
    """Decide whether an object is a Mosaic model, a data-import cube (both subtype 779), a
    classic cube (776) or something else.

    For 779 the Modeling read GET /api/model/dataModels/{id} decides (its changeset header is
    optional in the spec): 200 means a Mosaic model, 404 means none. Any other answer falls
    back to extType — 448 or missing means Mosaic (unverified constant; see the memory note).

    Returns {"subtype": int, "extType": int|None, "surface":
    "mosaic_data_model|data_import_cube|classic_cube|other", "name": str, "decided_by": str}.
    Pure read. Must be called before any endpoint that differs between surfaces (publish,
    refresh, execute, ACL, security filter, serve mode). See
    memory/reference_mosaic_vs_legacy_surfaces.md for the full pair cheat sheet.
    """
    r = m.get(f"/api/objects/{object_id}?type=3")
    if not r.ok:
        die(f"classify_object_surface: cannot GET /api/objects/{object_id}?type=3 "
            f"({r.status_code}); cannot route legacy-vs-Mosaic safely.")
    d = _json_or(r, {})
    subtype = int(d.get("subtype") or 0)
    ext_type = d.get("extType")
    decided_by = "subtype"
    if subtype == 779:
        probe = m.get(f"/api/model/dataModels/{object_id}")
        if probe.ok:
            surface, decided_by = "mosaic_data_model", "model read"
        elif probe.status_code == 404:
            surface, decided_by = "data_import_cube", "model read"
        else:
            # Subtype 779 (report_emma_cube) is shared by Mosaic models and data-import (MTDI)
            # cubes; extType 448 marks the Mosaic ones.
            surface = "mosaic_data_model" if ext_type in (None, 448, "448") else "data_import_cube"
            decided_by = f"extType (model read answered {probe.status_code})"
    else:
        surface = "classic_cube" if subtype == 776 else "other"
    return {"subtype": subtype, "extType": ext_type, "surface": surface, "name": d.get("name"),
            "decided_by": decided_by}


# Refresh policies a Mosaic publish accepts per table (TableRefreshSetting.refreshPolicy).
REFRESH_POLICIES = ("replace", "add", "update", "upsert")


LOADED_BUT_SILENT_S = 180     # tables said completed, the model still refuses queries: publish once more after this


def _mosaic_publish_verified(m: MSTR, model_id: str, *, poll_seconds: int = 180,
                             poll_interval: float = 5.0, refresh_policy: str = "replace") -> None:
    """Publish a Mosaic data model via the verified 3-step flow and assert completion.

    Flow (see memory/reference_mosaic_publish_path.md):
      1. POST /api/dataModels/{id}/instances           -> 204 with X-MSTR-DataModelInstanceId header
      2. POST /api/dataModels/{id}/publish             body {"tables":[{id,refreshPolicy}]}
      3. poll GET /api/dataModels/{id}/publishStatus   until every table is "completed"
                                                       (older tenants said "loaded") or one is in
                                                       an error state (error, schema_comparison_error)
      4. DELETE /api/dataModels/{id}/instances/{instanceId} — kept whenever the job may still
         be running (timeout, network error, interrupt): deleting it then could abort the job.

    Fails loud on:
      - missing instance header
      - non-2xx publish response
      - terminal error status (-2147212544 QueryEngine stall etc.) or a table in an error state
      - timeout before every table finishes
    Never falls back to /api/cubes/{id}?cubeAction=publish — internal and deprecated in the spec.
    """
    if refresh_policy not in REFRESH_POLICIES:
        die(f"refresh policy must be one of {', '.join(REFRESH_POLICIES)}, not {refresh_policy!r}")
    # discover tables (ids required in publish body); paged, so none is left unpublished
    tables, failed = _list_all(m, f"/api/model/dataModels/{model_id}/tables", "tables")
    if failed is not None:
        die(f"_mosaic_publish_verified: list tables failed {format_mstr_error(failed)}")
    tids = [(t.get("information") or {}).get("objectId") for t in tables if isinstance(t, dict)]
    tids = [tid for tid in tids if tid]
    if not tids:
        die(f"_mosaic_publish_verified: model {model_id} has 0 tables; nothing to publish.")

    # A model that does not answer yet is published the moment it does: on some tenants publishStatus keeps saying
    # status 1 with no tables long after the load finished (reference_mosaic_publish_path.md, 2026-06-11), so the
    # execute probe decides. A model that already answers (a re-publish) still holds its old data, so there the per-table
    # publishStatus remains the only proof.
    probe_decides = _executes(m, model_id) is False

    # 1. create instance
    r1 = m.post(f"/api/dataModels/{model_id}/instances")
    inst = r1.headers.get("X-MSTR-DataModelInstanceId") or r1.headers.get("X-Mstr-Datamodelinstanceid")
    if not inst:
        die(f"_mosaic_publish_verified: no X-MSTR-DataModelInstanceId header in "
            f"/instances response ({r1.status_code}); cannot proceed.")

    keep_instance = False
    try:
        # 2. publish with tables[] body
        hdr = {"X-MSTR-DataModelInstanceId": inst}
        r2 = m.post(f"/api/dataModels/{model_id}/publish",
                    headers=hdr,
                    json={"tables": [{"id": tid, "refreshPolicy": refresh_policy} for tid in tids]})
        if r2.status_code not in (200, 202, 204):
            die(f"_mosaic_publish_verified: publish POST {r2.status_code} {r2.text[:300]}")
        print(f"  ✓ mosaic publish started instanceId={inst} ({len(tids)} tables, {refresh_policy})",
              file=sys.stderr)

        # 3. poll until loaded
        deadline = time.time() + poll_seconds
        last = None
        next_probe = time.time() + 15
        tables_loaded = False
        loaded_at = 0.0
        retriggered = False
        while time.time() < deadline:
            if probe_decides and tables_loaded and not retriggered and time.time() - loaded_at >= LOADED_BUT_SILENT_S:
                # seen live (2026-10-28): every table said completed, the model never loaded (size 0) and kept refusing
                # queries; a second publish on a fresh instance loaded it in seconds. The first instance is idle by now.
                retriggered = True
                try:
                    m.delete(f"/api/dataModels/{model_id}/instances/{inst}")
                except Exception:
                    pass
                r1 = m.post(f"/api/dataModels/{model_id}/instances")
                inst = r1.headers.get("X-MSTR-DataModelInstanceId") or r1.headers.get("X-Mstr-Datamodelinstanceid") or inst
                hdr = {"X-MSTR-DataModelInstanceId": inst}
                r2 = m.post(f"/api/dataModels/{model_id}/publish", headers=hdr,
                            json={"tables": [{"id": tid, "refreshPolicy": refresh_policy} for tid in tids]})
                if r2.status_code not in (200, 202, 204):
                    die(f"_mosaic_publish_verified: second publish POST {r2.status_code} {r2.text[:300]}")
                print(f"  ↳ the tables said loaded {LOADED_BUT_SILENT_S}s ago but the model does not answer; publishing again "
                      f"(instanceId={inst})", file=sys.stderr)
                tables_loaded = False
                next_probe = time.time() + 15
            if probe_decides and time.time() >= next_probe:
                next_probe = time.time() + 15
                if _executes(m, model_id):
                    print("  ✓ mosaic publish COMPLETE: the model answers queries (publishStatus still said "
                          f"{json.dumps(last)[:120] if last else 'nothing'}).", file=sys.stderr)
                    return
            rs = m.get(f"/api/dataModels/{model_id}/publishStatus", headers=hdr)
            try: js = rs.json()
            except Exception: js = {"raw": rs.text}
            last = js
            if not isinstance(js, dict):
                js = {"raw": js}
            st = js.get("status")
            tbl = [t for t in (js.get("tables") or []) if isinstance(t, dict)]
            if isinstance(st, int) and st < 0:
                die(f"_mosaic_publish_verified: terminal error status={st} body={json.dumps(js)[:400]}")
            if js.get("code"):
                die(f"_mosaic_publish_verified: server error {js.get('code')}: {str(js.get('message',''))[:300]}")
            failed_tables = [t for t in tbl if str(t.get("status") or "").lower().endswith("error")]
            if failed_tables:
                die(f"_mosaic_publish_verified: table(s) failed to load: {json.dumps(failed_tables)[:400]}")
            if tbl and all(str(t.get("status") or "").lower() in ("loaded", "completed") for t in tbl):
                if not probe_decides:
                    print(f"  ✓ mosaic publish COMPLETE: {len(tbl)} tables loaded.", file=sys.stderr)
                    return
                # seen live (2026-10-28, a cross-store model): every table said completed within seconds while the model
                # went on loading for minutes and did not answer; a first publish is complete only when the model answers
                if not tables_loaded:
                    tables_loaded, loaded_at = True, time.time()
                    next_probe = time.time()
                    print(f"  ↳ {len(tbl)} tables loaded; waiting for the model to answer queries…", file=sys.stderr)
            time.sleep(poll_interval)
        keep_instance = True
        die(f"_mosaic_publish_verified: timeout after {poll_seconds}s; last status: "
            f"{json.dumps(last)[:400] if last else 'none'}. "
            f"This signature historically indicates tenant-side QueryEngineServer trouble or "
            f"dirty dataTypes; see memory/reference_mosaic_publish_path.md and "
            f"captures/2026-04-22-queryengine-publish-incident/README.md.")
    except (requests.RequestException, KeyboardInterrupt):
        keep_instance = True          # the job may still be running on the server
        print(f"  [publish] instance {inst} kept: the publish may still be running", file=sys.stderr)
        raise
    finally:
        # The documented flow ends by deleting the publish instance — unless the job may still
        # be running, where deleting it could abort the publish.
        if not keep_instance:
            try:
                m.delete(f"/api/dataModels/{model_id}/instances/{inst}")
            except Exception:
                pass


def _executes(m: MSTR, model_id: str) -> bool | None:
    """True when the model answers a one-row query (published), False when Strategy says it is not published
    (-2147072488), None when neither is certain (a timeout or another error)."""
    try:
        r = m.s.post(f"{m.base}/api/v2/cubes/{model_id}/instances", params={"limit": 1}, json={}, timeout=(15, 60))
    except requests.RequestException:
        return None
    if r.ok:
        return True
    text = r.text.lower()
    if "-2147072488" in r.text or "not published" in text:
        return False
    # the whole-model query of a model with several fact tables can exceed the engine's row limits: the engine ran it
    # against loaded data, so the model is published
    # (or be refused as a cartesian join of two facts): either way the engine planned it against loaded data
    if "intermediate rows" in text or "exceeds the maximum" in text or "result set" in text and "limit" in text or "cartesian join" in text:
        return True
    return None


def _classic_cube_publish(m: MSTR, cube_id: str) -> None:
    """Publish a classic Intelligent Cube (subtype 776). Separate from Mosaic publish.
    POST /api/v2/cubes/{id} is the public call (202 + job); the v1 cubeAction=publish form is
    internal and deprecated in the spec, kept only as a fallback for older servers."""
    r = m.post(f"/api/v2/cubes/{cube_id}")
    if r.status_code in (404, 405):
        r = m.post(f"/api/cubes/{cube_id}?cubeAction=publish", json={})
    if not r.ok:
        die(f"_classic_cube_publish: {format_mstr_error(r)}")
    print("  ✓ classic cube publish accepted (202 expected).", file=sys.stderr)


def _publish(m: MSTR, model_id: str, *, poll_seconds: int = 180,
             skip_classify: bool = False, refresh_policy: str = "replace") -> None:
    """Surface-routed publish: classify subType first, never mix Mosaic/legacy paths.

    skip_classify=True bypasses GET /api/objects/{id}?type=3 and assumes the caller
    already knows the target is a Mosaic data model (subType 779). Use this when
    chaining build→publish in the same session to save one project-scoped call
    against the session cap — see feedback_build_mosaic_session_leak.md.

    refresh_policy applies to Mosaic models only (per table: replace, add, update, upsert).
    """
    if skip_classify:
        print("  ↳ skip-classify: assuming Mosaic data model (subType 779)", file=sys.stderr)
        _mosaic_publish_verified(m, model_id, poll_seconds=poll_seconds, refresh_policy=refresh_policy)
        return
    info = classify_object_surface(m, model_id)
    if info["surface"] == "mosaic_data_model":
        _mosaic_publish_verified(m, model_id, poll_seconds=poll_seconds, refresh_policy=refresh_policy)
    elif info["surface"] == "classic_cube":
        if refresh_policy != "replace":
            die(f"refresh policy {refresh_policy!r} applies to Mosaic models only; {model_id} is a "
                f"classic Intelligent Cube (its publish reloads everything — use replace).")
        _classic_cube_publish(m, model_id)
    else:
        die(f"_publish: object {model_id} is subtype {info['subtype']} / extType {info.get('extType')} "
            f"({info['name']}) — {info['surface']}, not a Mosaic data model or classic cube "
            f"(decided by {info.get('decided_by')}). Refusing to guess. "
            f"See memory/reference_mosaic_vs_legacy_surfaces.md.")


def cmd_set_serve_mode(m: MSTR, args):
    m.login(identity=True)
    info = classify_object_surface(m, args.model_id)
    if info["surface"] != "mosaic_data_model":
        die(f"set-serve-mode: dataServeMode is a Mosaic-only concept; object {args.model_id} "
            f"is subtype {info['subtype']} ({info['name']}).")
    cs = open_cs(m)
    r = m.patch(f"/api/model/dataModels/{args.model_id}", json={"dataServeMode": args.mode})
    if not r.ok:
        discard_cs(m, cs)
        die(f"set-serve-mode: {format_mstr_error(r)}")
    commit_cs(m, cs)
    print(f"  ✓ dataServeMode set to {args.mode}", file=sys.stderr)


def cmd_publish(m: MSTR, args):
    m.login()
    _publish(m, args.model_id, poll_seconds=args.poll_seconds,
             skip_classify=getattr(args, "skip_classify", False))


def _refresh_policy(refresh_type: str | None) -> str:
    """--refresh-type → the publish refreshPolicy. "incremental" (the old cube refresh name)
    means upsert: insert new rows, update changed ones."""
    value = (refresh_type or "replace").lower()
    if value == "incremental":
        print("  [refresh] --refresh-type incremental = upsert (insert new rows, update changed ones)",
              file=sys.stderr)
        return "upsert"
    return value


def cmd_refresh(m: MSTR, args):
    """Re-publish (reload) a model's data. The REST API has no separate refresh call:
    a Mosaic refresh is a publish (POST /api/dataModels/{id}/publish), so this routes
    through the same verified publish path as `publish`, with --refresh-type as each
    table's refreshPolicy (replace by default)."""
    policy = _refresh_policy(getattr(args, "refresh_type", None))
    m.login()
    _publish(m, args.model_id, poll_seconds=getattr(args, "poll_seconds", 180), refresh_policy=policy)


# ── wire-relationships ─────────────────────────────────────────────────────────
# Post-build relationship wiring. Validates step-3 and step-5 prerequisites from
# feedback_mosaic_relationship_wiring.md before issuing any PUT, so we don't
# burn the session cap retrying on 8004ccdb (self-ref) or 8004ccc7 (invalid
# join table).

def _fetch_attribute(m: MSTR, model_id: str, attr_id: str, cache: dict | None = None) -> dict:
    """GET one attribute with token expressions. Pass `cache` (a dict) to reuse reads within
    one planning pass; never across writes."""
    if cache is not None and attr_id in cache:
        return cache[attr_id]
    r = m.s.get(f"{m.base}/api/model/dataModels/{model_id}/attributes/{attr_id}",
                params={"showExpressionAs": "tokens"})
    if not r.ok:
        die(f"wire-relationships: GET attribute {attr_id}: {r.status_code} {r.text[:200]}")
    body = _json_or(r, {})
    if cache is not None:
        cache[attr_id] = body
    return body


def _attr_table_ids(attr: dict) -> set:
    """Return the set of logical-table ids that this attribute's forms touch."""
    tids = set()
    for form in attr.get("forms", []) or []:
        for exp in form.get("expressions", []) or []:
            for t in exp.get("tables", []) or []:
                tid = t.get("objectId") or t.get("id")
                if tid:
                    tids.add(tid)
    return tids


def _list_model_attributes(m: MSTR, model_id: str) -> list:
    attrs, failed = _list_all(m, f"/api/model/dataModels/{model_id}/attributes", "attributes")
    if failed is not None:
        die(f"list attributes: {format_mstr_error(failed)}")
    return attrs


def _list_model_tables(m: MSTR, model_id: str) -> dict:
    tbls, failed = _list_all(m, f"/api/model/dataModels/{model_id}/tables", "tables")
    if failed is not None:
        die(f"list tables: {format_mstr_error(failed)}")
    by_name = {}
    for t in tbls:
        nm = (t.get("information") or {}).get("name") or t.get("name") or ""
        tid = (t.get("information") or {}).get("objectId") or t.get("id") or t.get("objectId")
        if nm and tid:
            by_name[nm] = tid
    return by_name


def _plan_attribute_merges(
    pairs: list[tuple[str, str]],
    by_tcol: dict[tuple[str, str], dict],
    table_ids: dict[str, str],
) -> tuple[list[tuple[dict, dict | None, str, str, str]], list[tuple[str, str]]]:
    """Pure pre-flight validation for merge-attributes: given the requested
    (child_ref, parent_ref) pairs and the model's current (table, column) ->
    attribute / table -> id lookups, decide what can be merged and what must
    be skipped. Separated out from cmd_merge_attributes (which does the
    actual REST I/O) so this decision logic is independently unit-testable.

    Returns (plan, skips):
      plan  -- list of (parent_attr, child_attr_or_None, child_table, child_col, label).
               child_attr is None when the child column never got an attribute
               created at all (e.g. entity-key detection failed for its table).
               There's nothing to merge/delete in that case, but the parent
               can still gain the expression -- see cmd_merge_attributes'
               docstring for why this matters for compact-key schemas.
      skips -- list of (label, reason) for pairs that can't proceed at all.
    """
    plan: list[tuple[dict, dict | None, str, str, str]] = []
    skips: list[tuple[str, str]] = []
    for child_ref, parent_ref in pairs:
        try:
            ct, cc = child_ref.split(".", 1)
            pt, pc = parent_ref.split(".", 1)
        except ValueError:
            skips.append((f"{child_ref} → {parent_ref}", "malformed pair (expected TABLE.COL on both sides)"))
            continue
        label = f"{pt}.{pc} += {ct}.{cc}"
        parent = by_tcol.get((pt, pc))
        child  = by_tcol.get((ct, cc))
        if not parent:
            skips.append((label, f"parent attribute for {pt}.{pc} not found"))
            continue
        if parent is child:
            skips.append((label, "already conformed (parent and child resolve to same attribute)"))
            continue
        if ct not in table_ids:
            skips.append((label, f"child table {ct!r} not in model"))
            continue
        plan.append((parent, child, ct, cc, label))
    return plan, skips


def cmd_merge_attributes(m: MSTR, args):
    """Conform differently-named FK columns by merging child expressions into
    the parent attribute.

    Mosaic's auto-conformance groups expressions by IDENTICAL column names
    across tables (e.g. `PRODUCT_ID` in both `orders` and `products`). Real
    warehouses using Kimball-style prefixed surrogate keys (`i_item_sk` on
    the item dim vs `ss_item_sk` on the store_sales fact) defeat that — each
    column becomes its own attribute and joins never resolve, even with
    --conformance-map / --fk-map applied at build time (those only rename;
    they do not actually merge).

    This command takes the same {child.col: parent.col} map shape as --fk-map
    and, for each pair, PATCHes the parent attribute to gain an expression on
    the child's table, then DELETEs the now-redundant child attribute. The
    result is a true Kimball conformed dimension: one attribute whose forms
    span the dim and every fact that FKs to it.

    The child attribute does not have to already exist. If entity-key
    detection failed for the child's table entirely (e.g. a compact-key
    naming convention the build's heuristics didn't recognize), the child
    column may never have gotten an attribute created at all -- it fails
    outright with a duplicate-name error during build and is silently
    dropped, so there is nothing to "merge from". In that case this command
    still succeeds: it adds the expression straight to the parent and skips
    only the (now moot) child-deletion step. Pre-flight output distinguishes
    "merge" (child existed) from "add" (child never existed) so you can tell
    which happened.

    Role-playing edge case: when the same fact table has two FKs to the same
    dim (e.g. `cs_sold_date_sk` AND `cs_ship_date_sk` both pointing at
    `d_date_sk`), Mosaic rejects the second merge with 8004cc77 ("table is
    used in other expressions"). Those pairs are reported as skipped — they
    need their own role-playing alias attributes built separately, which is
    out of scope for this command (see memory/feedback_mosaic_role_playing_dimensions.md).

    Hint file shape (JSON or YAML), accepts either form:
        # flat — matches the --fk-map shape that `build` already accepts
        {"<CHILD_TABLE>.<CHILD_COL>": "<PARENT_TABLE>.<PARENT_COL>", ...}

        # envelope — accepted for forward-compat with extended hint metadata
        {"merges": [
            {"child":  "<CHILD_TABLE>.<CHILD_COL>",
             "parent": "<PARENT_TABLE>.<PARENT_COL>"}, ...]}

    Honors --dry-run; otherwise opens a schema-edit changeset and commits at
    the end (with discard on exception).
    """
    m.login(identity=True)
    model_id = args.model_id

    pairs = _read_merge_hints(args.hints)
    if not pairs:
        die("merge-attributes: no merge pairs found in --hints.")

    attrs = _list_model_attributes(m, model_id)
    table_ids = _list_model_tables(m, model_id)  # name -> objectId
    # Build (table_name, column_text) -> attribute index. A given attribute can
    # appear under multiple (table, col) keys if it's already partially conformed.
    by_tcol: dict[tuple[str, str], dict] = {}
    for a in attrs:
        for form in a.get("forms") or []:
            for exp in form.get("expressions") or []:
                col = (exp.get("expression") or {}).get("text", "")
                for t in exp.get("tables") or []:
                    tn = t.get("name", "")
                    if tn and col:
                        by_tcol[(tn, col)] = a

    plan, skips = _plan_attribute_merges(pairs, by_tcol, table_ids)

    print(f"→ merge-attributes plan: {len(plan)} to merge, {len(skips)} skipped pre-flight", file=sys.stderr)
    for label, reason in skips:
        print(f"  ✗ SKIP {label}: {reason}", file=sys.stderr)
    if args.dry_run:
        for _p, _c, _t, _col, label in plan:
            verb = "would merge" if _c is not None else "would add (child never created)"
            print(f"  ✓ {verb} {label}", file=sys.stderr)
        print("→ dry-run; exiting without writes", file=sys.stderr)
        return
    if not plan:
        die("merge-attributes: nothing to write after validation.")

    cs = open_cs(m, schema_edit=True, release_self_locks=bool(args.release_locks))
    ok = 0
    deleted = 0
    write_skips = []
    write_failures = 0          # role-playing skips (8004cc77) are expected, not failures
    try:
        for parent_attr, child_attr, child_table, child_col, label in plan:
            pid = (parent_attr.get("information") or {}).get("objectId")
            # child_attr is None when the child column never got an attribute
            # created at all (see docstring); nothing to delete in that case.
            cid = (child_attr.get("information") or {}).get("objectId") if child_attr else None
            # Re-fetch parent each iteration: prior merges in this changeset
            # mutate it, and the PATCH must include the cumulative forms list.
            parent_full = _fetch_attribute(m, model_id, pid)
            normalized = ms.normalize_expressions(parent_full)
            forms = normalized.get("forms") or []
            if not forms:
                write_skips.append((label, "parent has no forms"))
                continue
            form = forms[0]
            child_tid = table_ids.get(child_table)
            new_expr = ms.make_expression(
                child_col,
                table_id=child_tid,
                table_name=child_table,
            )
            exprs = form.get("expressions") or []
            already = any(
                e.get("tables") and (e.get("tables")[0] or {}).get("name") == child_table
                and (e.get("expression") or {}).get("tokens") and
                next((tok.get("value") for tok in e["expression"]["tokens"]
                      if tok.get("type") == "column_reference"), None) == child_col
                for e in exprs
            )
            if not already:
                exprs.append(new_expr)
                form["expressions"] = exprs
            # PATCH the parent. Only forms is mutable here; everything else on
            # the parent (name, lookupTable, etc.) is preserved by Strategy.
            # The changeset travels in the X-MSTR-MS-Changeset header open_cs set.
            r = m.patch(f"/api/model/dataModels/{model_id}/attributes/{pid}", json={"forms": forms})
            if not r.ok:
                err = ms.parse_mstr_error(r)
                # 8004cc77 — "table is used in other expressions". This is
                # the role-playing case: the same fact has two FKs to the
                # same dim. Skip and continue — a future role-alias build
                # step handles these.
                if err.get("code") == "8004cc77":
                    write_skips.append((label, "role-playing secondary (8004cc77) — skipped"))
                    continue
                write_skips.append((label, f"PATCH parent: {format_mstr_error(r)}"))
                write_failures += 1
                continue
            print(f"  + {label}" + ("" if cid else " (child never existed, nothing to delete)"),
                  file=sys.stderr)
            ok += 1
            # Delete the now-redundant child attribute -- only if one existed.
            if cid and not args.keep_children:
                dr = m.delete(f"/api/model/dataModels/{model_id}/attributes/{cid}")
                if dr.status_code in (200, 204):
                    deleted += 1
                else:
                    write_skips.append((label, f"DELETE child {cid}: {format_mstr_error(dr)}"))
                    write_failures += 1
        commit_cs(m, cs)
    except Exception:
        discard_cs(m, cs)
        raise

    for label, reason in write_skips:
        print(f"  ✗ SKIP-WRITE {label}: {reason}", file=sys.stderr)
    print(
        f"→ merge-attributes committed: {ok}/{len(plan)} merges, "
        f"{deleted} child attrs deleted, {len(skips)} pre-flight skips, "
        f"{len(write_skips)} write skips",
        file=sys.stderr,
    )
    if write_failures:
        print(f"[merge-attributes] {write_failures} write(s) failed — see SKIP-WRITE above.", file=sys.stderr)
        sys.exit(1)


def _read_merge_hints(path: str) -> list[tuple[str, str]]:
    """Read a merge-hints file in either the flat fk-map shape
    ({child.col: parent.col}) or an envelope ({"merges":[{child,parent}]}).
    Returns a list of (child_ref, parent_ref) tuples preserving input order.
    """
    if not path:
        return []
    data = load_structured_file(path) or {}
    pairs: list[tuple[str, str]] = []
    if isinstance(data, dict) and isinstance(data.get("merges"), list):
        for item in data["merges"]:
            if not isinstance(item, dict):
                continue
            c, p = item.get("child"), item.get("parent")
            if c and p:
                pairs.append((str(c), str(p)))
    elif isinstance(data, dict):
        for c, p in data.items():
            if isinstance(c, str) and isinstance(p, str):
                pairs.append((c, p))
    return pairs


def infer_relationship_table(parent_full: dict, p_tids, c_tids, tables_by_name: dict) -> tuple[str | None, str]:
    """The table a relationship lives in, when the hint names none: the one table both attributes read (each has
    an expression on it). Several: the parent's lookup table if it is one of them. -> (table id, why not)."""
    names = {tid: n for n, tid in tables_by_name.items()}
    common = sorted(set(p_tids) & set(c_tids))
    if len(common) == 1:
        return common[0], ""
    if not common:
        return None, ("no table that both attributes read; name relationship_table, after giving the parent or "
                      "child an expression on it")
    lookup = (parent_full.get("attributeLookupTable") or {}).get("objectId")
    if lookup in common:
        return lookup, ""
    return None, (f"both attributes read {len(common)} tables ({sorted(names.get(t, t) for t in common)}); name "
                  f"relationship_table")


def cmd_wire_relationships(m: MSTR, args):
    """Wire attribute relationships with step-3/step-5 validation.

    Reads a JSON/YAML FK-hint file listing parent→child attribute pairs plus the
    relationship_table, validates each against the live model, and issues only
    the PUTs that will succeed.

    Hint file shape:
      {"relationships": [
         {"parent_attribute": "<name or id>",
          "child_attribute":  "<name or id>",
          "relationship_table": "<name or id>",   # optional: inferred from the tables both attributes read
          "type": "one_to_many"|"many_to_many"|"one_to_one"}]}
    """
    m.login(identity=True)
    model_id = args.model_id

    hints = _read_wire_hints(args.hints)
    if not hints:
        die("wire-relationships: no relationships found in --hints file.")

    # Role-playing dimension detection: when multiple hints share the same
    # (parent_attribute, relationship_table) pair, the second+ hint is a
    # role-playing secondary. Log them explicitly rather than silently
    # picking first-wins. See mosaic_safety.ROLE_PLAYING_DOC.
    hints, role_secondaries = ms.detect_role_playing_secondaries(hints)
    if role_secondaries:
        print(
            f"→ wire-relationships: {len(role_secondaries)} role-playing "
            "secondary hint(s) detected — wiring primary role only. "
            "Build alias attributes for secondaries before re-running:",
            file=sys.stderr,
        )
        for rel in role_secondaries:
            print(
                f"  ! ROLE-PLAY skip "
                f"{rel.get('parent_attribute')}→{rel.get('child_attribute')} "
                f"via {rel.get('relationship_table')}",
                file=sys.stderr,
            )

    attrs = _list_model_attributes(m, model_id)
    by_name: dict[str, list] = {}
    by_id = {}
    for a in attrs:
        info = a.get("information") or {}
        aid = info.get("objectId") or a.get("id")
        nm  = info.get("name") or a.get("name") or ""
        if aid:
            by_id[aid] = a
            if nm:
                by_name.setdefault(nm.lower(), []).append(a)
    tables_by_name = _list_model_tables(m, model_id)
    fetched: dict = {}          # attribute reads, reused while planning (no writes yet)

    def _resolve_attr(ref: str) -> dict:
        if not ref: return None
        if ref in by_id: return by_id[ref]
        for cand in attribute_name_candidates(ref):   # also the connector's form names
            hits = by_name.get(cand) or []
            if hits:
                return hits[0] if len(hits) == 1 else None
        return None

    def _attr_problem(ref: str, role: str) -> str:
        n = len(by_name.get((ref or "").lower()) or [])
        if n > 1:
            return f"{role} attribute {ref!r} matches {n} attributes by name; pass its objectId"
        return f"{role} attribute {ref!r} not found"

    def _resolve_table(ref: str) -> str:
        if not ref:   # inferred below from the tables both attributes read
            return None
        if ref in tables_by_name.values(): return ref
        if ref in tables_by_name: return tables_by_name[ref]
        loose = [tid for nm, tid in tables_by_name.items() if _table_key(nm) == _table_key(ref)]
        return loose[0] if len(loose) == 1 else None

    plan = []   # rows ready to PUT
    skips = []  # rows skipped with reason

    for h in hints:
        parent = _resolve_attr(h.get("parent_attribute"))
        child  = _resolve_attr(h.get("child_attribute"))
        rtbl_id = _resolve_table(h.get("relationship_table"))
        rtype  = h.get("type","one_to_many")
        table_given = bool(h.get("relationship_table"))
        label = f"{h.get('parent_attribute')}→{h.get('child_attribute')} via {h.get('relationship_table')}"

        if not parent:
            skips.append((label, _attr_problem(h.get("parent_attribute"), "parent")))
            continue
        if not child:
            skips.append((label, _attr_problem(h.get("child_attribute"), "child")))
            continue
        if table_given and not rtbl_id:
            skips.append((label, f"relationship_table {h.get('relationship_table')!r} not found in model; "
                                 f"tables: {sorted(tables_by_name)}"))
            continue

        # Step-3: forbid self-reference.
        p_id = (parent.get("information") or {}).get("objectId") or parent.get("id")
        c_id = (child.get("information")  or {}).get("objectId") or child.get("id")
        if p_id == c_id:
            skips.append((label, "parent and child resolve to same attribute (would trip 8004ccdb)"))
            continue

        # Fetch full attribute definitions (/attributes list may omit forms[].expressions details).
        p_full = _fetch_attribute(m, model_id, p_id, fetched)
        c_full = _fetch_attribute(m, model_id, c_id, fetched)
        p_tids = _attr_table_ids(p_full)
        c_tids = _attr_table_ids(c_full)
        if not table_given:
            rtbl_id, why = infer_relationship_table(p_full, p_tids, c_tids, tables_by_name)
            if not rtbl_id:
                skips.append((label, why))
                continue
            label = f"{h.get('parent_attribute')}→{h.get('child_attribute')} via " \
                    f"{next((n for n, t in tables_by_name.items() if t == rtbl_id), rtbl_id)} (inferred)"

        # Step-5: both endpoints must have an expression on the relationship table.
        missing = []
        if rtbl_id not in p_tids: missing.append("parent")
        if rtbl_id not in c_tids: missing.append("child")
        if missing:
            skips.append((label,
                f"{'+'.join(missing)} has no expression on relationship_table (would trip 8004ccc7); "
                f"PATCH to add the missing expression first — see reference_strategy_object_cloning.md"))
            continue

        plan.append((p_id, c_id, rtbl_id, rtype, label))

    # Report plan.
    print(f"→ wire-relationships plan: {len(plan)} to write, {len(skips)} skipped", file=sys.stderr)
    for label, reason in skips:
        print(f"  ✗ SKIP {label}: {reason}", file=sys.stderr)
    if args.dry_run:
        print(f"→ dry-run; exiting without PUTs", file=sys.stderr)
        for p_id, c_id, rtbl_id, rtype, label in plan:
            print(f"  ✓ would PUT {label} [{rtype}]", file=sys.stderr)
        return

    if not plan:
        die("wire-relationships: nothing to write after validation. See skip reasons above.")

    # Group plan rows by child attribute so we PUT each child once with all
    # its parents merged in — this respects the merge contract of
    # put_relationships_merged() and minimizes round-trips.
    by_child: dict[str, list[tuple] ] = {}
    for p_id, c_id, rtbl_id, rtype, label in plan:
        by_child.setdefault(c_id, []).append((p_id, c_id, rtbl_id, rtype, label))

    cs = open_cs(m, schema_edit=False)
    ok = 0
    failed = 0
    try:
        for c_id, rows in by_child.items():
            new_rels = []
            labels = []
            for p_id, _c_id, rtbl_id, rtype, label in rows:
                new_rels.append({
                    "parent": {"objectId": p_id, "subType": "attribute"},
                    "child":  {"objectId": c_id, "subType": "attribute"},
                    "relationshipType": rtype,
                    "relationshipTable": {
                        "objectId": rtbl_id, "subType": "logical_table"
                    },
                })
                labels.append(label)
            success, added, total, err = put_relationships_merged(
                m, model_id, c_id, new_rels, cs, replace=args.replace
            )
            if success:
                ok += added
                action = "REPLACED" if args.replace else f"MERGED (+{added}/{total})"
                for label in labels:
                    print(f"  ✓ {label} [{action}]", file=sys.stderr)
            else:
                failed += len(labels)
                for label in labels:
                    print(f"  ✗ {label}: {err}", file=sys.stderr)
        commit_cs(m, cs)
    except Exception:
        discard_cs(m, cs)
        raise
    print(
        f"→ wire-relationships committed: {ok}/{len(plan)} new rels written "
        f"(merge-aware), {len(skips)} skipped pre-flight, "
        f"{len(role_secondaries)} role-playing secondaries",
        file=sys.stderr,
    )
    if failed:
        print(f"[wire-relationships] {failed} relationship(s) not written — see ✗ lines above.", file=sys.stderr)
        sys.exit(1)


def _read_wire_hints(path: str) -> list:
    """Read JSON or YAML FK-hint file. Accepts either a {relationships:[...]}
    envelope or a bare list."""
    if not path: return []
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        data = json.loads(text)
    except ValueError:
        data = _load_yaml(path)     # PyYAML, else the Ruby safe_load fallback; dies on bad YAML
    if isinstance(data, dict):
        return data.get("relationships") or []
    if isinstance(data, list):
        return data
    return []


def cmd_build_from_schema_objects(m: MSTR, args):
    """Build a Mosaic data model from existing classic schema object IDs.

    Pipeline:
      1. Read classic attribute/fact/metric definitions.
      2. Resolve physical tables referenced by those objects.
      3. Discover warehouse datasource/schema/name for each.
      4. Create the Mosaic model shell.
      5. Add physical tables.
      6. Translate + batch-create attributes and fact metrics (CS1).
      7. Wire relationships from classic attribute relationship arrays (CS2).
      8. Translate + create derived metrics bottom-up (CS3).
      9. Optionally publish in-memory.
     10. Write a JSON review file with warnings + created object IDs.
    """
    m.login(identity=True)

    attr_ids   = _parse_id_list(getattr(args, "attribute_ids", "") or "")
    fact_ids   = _parse_id_list(getattr(args, "fact_ids", "") or "")
    metric_ids = _parse_id_list(getattr(args, "metric_ids", "") or "")

    if not (attr_ids or fact_ids or metric_ids):
        die("provide at least one of --attribute-ids, --fact-ids, --metric-ids")

    print(f"→ Input: {len(attr_ids)} attributes, {len(fact_ids)} facts, "
          f"{len(metric_ids)} metrics", file=sys.stderr)

    all_warnings: list[str] = []
    rate_sleep = 0.05 if (len(attr_ids) + len(fact_ids) + len(metric_ids)) > 200 else 0

    # ── Read classic object definitions ──────────────────────────────────────
    print("→ Reading classic attribute definitions…", file=sys.stderr)
    attr_defs: dict[str, dict] = {}
    for aid in attr_ids:
        r = m.get(f"/api/model/attributes/{aid}", params={"showExpressionAs": "tree"})
        if not r.ok:
            all_warnings.append(f"attribute {aid}: read failed {r.status_code} — skipped")
            continue
        attr_defs[aid] = r.json()
        if rate_sleep:
            time.sleep(rate_sleep)
        if m.verbose:
            n = (attr_defs[aid].get("information") or {}).get("name", "?")
            print(f"  attr {aid}: {n}", file=sys.stderr)

    print("→ Reading classic fact definitions…", file=sys.stderr)
    fact_defs: dict[str, dict] = {}
    for fid in fact_ids:
        r = m.get(f"/api/model/facts/{fid}", params={"showExpressionAs": "tree"})
        if not r.ok:
            all_warnings.append(f"fact {fid}: read failed {r.status_code} — skipped")
            continue
        fact_defs[fid] = r.json()
        if rate_sleep:
            time.sleep(rate_sleep)

    print("→ Reading classic metric definitions…", file=sys.stderr)
    metric_defs: dict[str, dict] = {}
    for mid in metric_ids:
        r = m.get(f"/api/model/metrics/{mid}", params={"showExpressionAs": "tree"})
        if not r.ok:
            all_warnings.append(f"metric {mid}: read failed {r.status_code} — skipped")
            continue
        metric_defs[mid] = r.json()
        if rate_sleep:
            time.sleep(rate_sleep)

    # ── Collect referenced classic table IDs ─────────────────────────────────
    classic_table_ids: set[str] = set()
    for adef in attr_defs.values():
        classic_table_ids |= sot.extract_table_ids_from_attribute(adef)
    for fdef in fact_defs.values():
        classic_table_ids |= sot.extract_table_ids_from_fact(fdef)

    if not classic_table_ids:
        die("no physical table references found in any object definition. "
            "Verify the IDs are correct and showExpressionAs=tree returns expressions.")

    print(f"→ Resolving {len(classic_table_ids)} classic table(s)…", file=sys.stderr)
    classic_table_meta: dict[str, dict] = {}
    for tid in classic_table_ids:
        r = m.get(f"/api/model/tables/{tid}")
        if not r.ok:
            all_warnings.append(
                f"table {tid}: read failed {r.status_code} — referencing "
                "objects may have unmapped expressions"
            )
            continue
        classic_table_meta[tid] = r.json()

    # Group tables by (datasource_id, schema) → list of (classic_id, table_name)
    source_groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for tid, tmeta in classic_table_meta.items():
        pt = tmeta.get("physicalTable") or {}
        ds_obj = pt.get("databaseInstance") or {}
        ds_id  = ds_obj.get("objectId") or ds_obj.get("id") or args.instance_id
        schema = pt.get("namespace") or args.schema or ""
        tname  = pt.get("tableName") or (tmeta.get("information") or {}).get("name") or ""
        if not (ds_id and tname):
            all_warnings.append(
                f"table {tid}: cannot determine datasource or table name — skipped"
            )
            continue
        source_groups.setdefault((ds_id, schema), []).append((tid, tname))

    if not source_groups:
        die("Could not resolve any source tables. Check that classic table object IDs "
            "are readable and return physicalTable.databaseInstance.")

    # ── Create model shell + add physical tables (CS1) ───────────────────────
    print(f"→ Creating model '{args.name}'…", file=sys.stderr)
    serve_mode = "in_memory" if args.publish else args.data_serve_mode
    dest_folder_id = resolve_dest_folder(m, args.dest_folder)   # may die(); before any changeset
    use_batch = bool(getattr(args, "use_batch", False))
    failures = {"tables": 0, "attributes": 0, "fact_metrics": 0, "relationships": 0, "metrics": 0,
                "publish": 0}
    logical_table_map: dict[str, str] = {}
    # POST /api/model/dataModels requires X-MSTR-MS-Changeset, so the shell is created inside
    # CS1 (as `build` does): a failure before the commit leaves no empty model behind.
    cs1 = open_cs(m)
    try:
        r = m.post("/api/model/dataModels", json={
            "information": {"name": args.name, "destinationFolderId": dest_folder_id},
            "dataServeMode": serve_mode,
        })
        model_id = (_json_or(r, {}).get("information") or {}).get("objectId") if r.ok else None
        if not model_id:
            die(f"create model: {r.status_code} {r.text[:400]}")
        print(f"  model_id={model_id}", file=sys.stderr)

        for (ds_id, schema), entries in source_groups.items():
            for classic_tid, tname in entries:
                tbl_body = _make_pipeline_table_body(m, ds_id, schema, tname)
                if tbl_body is None:
                    all_warnings.append(
                        f"table {tname}: could not fetch warehouse metadata — skipped"
                    )
                    failures["tables"] += 1
                    continue
                rr = m.post(f"/api/model/dataModels/{model_id}/tables", json=tbl_body)
                mosaic_tid = (_json_or(rr, {}).get("information") or {}).get("objectId") if rr.ok else None
                if not mosaic_tid:
                    all_warnings.append(
                        f"add table {tname}: {rr.status_code} {rr.text[:200]}"
                    )
                    failures["tables"] += 1
                    continue
                logical_table_map[classic_tid] = mosaic_tid
                # Also map any other classic tables that resolved to the same warehouse name
                for other_cid, other_meta in classic_table_meta.items():
                    if other_cid == classic_tid:
                        continue
                    other_pt = other_meta.get("physicalTable") or {}
                    if other_pt.get("tableName") == tname:
                        logical_table_map.setdefault(other_cid, mosaic_tid)
                print(f"  + table {tname} → {mosaic_tid}", file=sys.stderr)

        # ── Translate + batch-create attributes ──────────────────────────────
        print(f"→ Translating {len(attr_defs)} attribute(s)…", file=sys.stderr)
        attr_ops: list[dict] = []
        attr_id_order: list[str] = []
        attr_id_to_mosaic: dict[str, str] = {}

        for classic_id, adef in attr_defs.items():
            payload, warns = sot.translate_attribute(adef, logical_table_map)
            all_warnings.extend([f"[attr {classic_id}] {w}" for w in warns])
            attr_ops.append({"op": "create", "path": "/attributes", "value": payload})
            attr_id_order.append(classic_id)

        if attr_ops:
            passed, failed = create_objects(m, model_id, cs1, attr_ops, use_batch=use_batch)
            for f in failed:
                all_warnings.append(f"[attr] failed op: {json.dumps(f)[:200]}")
            failures["attributes"] += len(failed)
            for result in passed:
                i = result.get("_index")
                response = result.get("response") or result
                obj_id = (response.get("information") or {}).get("objectId")
                if not isinstance(i, int) or i >= len(attr_id_order) or not obj_id:
                    all_warnings.append(f"[attr] created object not matched to its classic id: "
                                        f"{json.dumps(result)[:200]}")
                    failures["attributes"] += 1
                    continue
                attr_id_to_mosaic[attr_id_order[i]] = obj_id

        # ── Translate + batch-create fact metrics ────────────────────────────
        print(f"→ Translating {len(fact_defs)} fact(s)…", file=sys.stderr)
        fact_ops: list[dict] = []
        fact_id_order: list[str] = []
        fact_id_to_mosaic: dict[str, str] = {}

        for classic_id, fdef in fact_defs.items():
            payload, warns = sot.translate_fact_to_factmetric(fdef, logical_table_map)
            all_warnings.extend([f"[fact {classic_id}] {w}" for w in warns])
            fact_ops.append({"op": "create", "path": "/factMetrics", "value": payload})
            fact_id_order.append(classic_id)

        if fact_ops:
            passed, failed = create_objects(m, model_id, cs1, fact_ops, use_batch=use_batch)
            for f in failed:
                all_warnings.append(f"[fact] failed op: {json.dumps(f)[:200]}")
            failures["fact_metrics"] += len(failed)
            for result in passed:
                i = result.get("_index")
                response = result.get("response") or result
                obj_id = (response.get("information") or {}).get("objectId")
                if not isinstance(i, int) or i >= len(fact_id_order) or not obj_id:
                    all_warnings.append(f"[fact] created object not matched to its classic id: "
                                        f"{json.dumps(result)[:200]}")
                    failures["fact_metrics"] += 1
                    continue
                fact_id_to_mosaic[fact_id_order[i]] = obj_id

        commit_cs(m, cs1)
        cs1 = None
        print(f"  CS1 committed ({len(attr_id_to_mosaic)} attrs, "
              f"{len(fact_id_to_mosaic)} factMetrics)", file=sys.stderr)
    except Exception:
        if cs1:
            discard_cs(m, cs1)
        raise

    # ── Wire relationships (CS2) ─────────────────────────────────────────────
    rel_count = 0
    cs2 = open_cs(m)
    try:
        for classic_child_id, adef in attr_defs.items():
            mosaic_child_id = attr_id_to_mosaic.get(classic_child_id)
            if not mosaic_child_id:
                continue
            child_rels: list[dict] = []   # one merged PUT per child — the PUT replaces the set
            for rel in (adef.get("relationships") or []):
                parent_classic_id = (rel.get("parent") or {}).get("objectId")
                if not parent_classic_id:
                    continue
                mosaic_parent_id = attr_id_to_mosaic.get(parent_classic_id)
                if not mosaic_parent_id:
                    all_warnings.append(
                        f"[rel] child {classic_child_id}: parent "
                        f"{parent_classic_id} not in translated set — relationship skipped"
                    )
                    continue
                rel_table_classic = (rel.get("relationshipTable") or {}).get("objectId")
                rel_table_mosaic = (
                    logical_table_map.get(rel_table_classic) if rel_table_classic else None
                )
                rel_body = {
                    "relationships": [{
                        "parent": {"objectId": mosaic_parent_id, "subType": "attribute"},
                        "child":  {"objectId": mosaic_child_id,  "subType": "attribute"},
                        "relationshipType": rel.get("relationshipType", "one_to_many"),
                    }]
                }
                if rel_table_mosaic:
                    rel_body["relationships"][0]["relationshipTable"] = {
                        "objectId": rel_table_mosaic, "subType": "logical_table",
                    }
                child_rels.extend(rel_body["relationships"])
            if child_rels:
                ok, _added, _total, err = put_relationships_merged(m, model_id, mosaic_child_id, child_rels, cs2)
                if ok:
                    rel_count += len(child_rels)
                else:
                    all_warnings.append(f"[rel] into {classic_child_id}: {err}")
                    failures["relationships"] += len(child_rels)
        commit_cs(m, cs2)
        cs2 = None
        print(f"  CS2 committed ({rel_count} relationships)", file=sys.stderr)
    except Exception:
        if cs2:
            discard_cs(m, cs2)
        raise

    # ── Translate + create derived metrics (CS3, bottom-up) ──────────────────
    metric_id_to_mosaic: dict[str, str] = {}
    if metric_defs:
        print(f"→ Translating {len(metric_defs)} metric(s) in dependency order…",
              file=sys.stderr)
        try:
            ordered_ids = sot.build_metric_translation_order(metric_defs)
        except ValueError as e:
            all_warnings.append(
                f"[metrics] dependency cycle detected: {e} — input order used"
            )
            ordered_ids = list(metric_defs.keys())

        cs3 = open_cs(m)
        try:
            for classic_id in ordered_ids:
                mdef = metric_defs.get(classic_id)
                if not mdef:
                    continue
                payload, warns = sot.translate_metric(
                    mdef, fact_id_to_mosaic, metric_id_to_mosaic,
                    attr_id_to_mosaic_id=attr_id_to_mosaic,
                )
                all_warnings.extend([f"[metric {classic_id}] {w}" for w in warns])
                mtype = sot.classify_metric(mdef)
                path_suffix = "/factMetrics" if mtype == "fact_metric" else "/metrics"
                rr = m.post(
                    f"/api/model/dataModels/{model_id}{path_suffix}", json=payload
                )
                new_id = (_json_or(rr, {}).get("information") or {}).get("objectId") if rr.ok else None
                if not new_id:
                    all_warnings.append(
                        f"[metric {classic_id}] create failed: "
                        f"{rr.status_code} {rr.text[:200]}"
                    )
                    failures["metrics"] += 1
                    continue
                metric_id_to_mosaic[classic_id] = new_id
                print(f"  metric {classic_id} → {new_id}", file=sys.stderr)
            commit_cs(m, cs3)
            cs3 = None
            print(f"  CS3 committed ({len(metric_id_to_mosaic)} metrics)",
                  file=sys.stderr)
        except Exception:
            if cs3:
                discard_cs(m, cs3)
            raise

    # ── Optional publish: the verified Mosaic flow, confirmed by publishStatus ──
    published = None
    if args.publish:
        if failures["tables"] or failures["attributes"] or failures["fact_metrics"]:
            all_warnings.append("publish skipped: tables, attributes or fact metrics failed to build")
            published = False
        else:
            print(f"→ Publishing model {model_id} in-memory…", file=sys.stderr)
            try:
                _publish(m, model_id, skip_classify=True)   # just created: known Mosaic model
                published = True
            except (SystemExit, requests.RequestException) as e:
                failures["publish"] += 1
                published = False
                all_warnings.append(f"publish failed ({type(e).__name__}) — the model exists but "
                                    "is not materialized; see the FATAL/error line above")

    # ── Review file ──────────────────────────────────────────────────────────
    review = {
        "model_id": model_id,
        "model_url": f"{m.base}/app/library#/model/{model_id}",
        "ok": not any(failures.values()),
        "failures": failures,
        "published": published,
        "translated": {
            "attributes": len(attr_id_to_mosaic),
            "factMetrics": len(fact_id_to_mosaic),
            "metrics": len(metric_id_to_mosaic),
            "relationships": rel_count,
        },
        "warnings": all_warnings,
    }
    if args.review_file:
        _write_private(args.review_file, review)
        print(f"→ Review file: {args.review_file}", file=sys.stderr)

    if all_warnings:
        print(f"\n⚠  {len(all_warnings)} warning(s) — see review file or stderr above.",
              file=sys.stderr)

    print(f"\n✓ Model: {m.base}/app/library#/model/{model_id}", file=sys.stderr)
    print(json.dumps(review, indent=2))
    if not review["ok"]:
        print(f"[build-from-schema-objects] finished with failures: {failures}", file=sys.stderr)
        sys.exit(1)


def cmd_delete_model(m: MSTR, args):
    if not args.yes:
        die("delete-model requires --yes after verifying the Mosaic data model id")
    m.login()
    # DELETE /api/objects/{id}?type=3 deletes any type-3 object (reports and cubes too):
    # confirm the target is a Mosaic model first.
    info = classify_object_surface(m, args.model_id)
    if info["surface"] != "mosaic_data_model" and not getattr(args, "any_type", False):
        die(f"delete-model: {args.model_id} ({info['name']}) is a {info['surface']} "
            f"(subtype {info['subtype']}), not a Mosaic data model; refusing. "
            f"Pass --any-type to delete it anyway.")
    r = m.delete(f"/api/objects/{args.model_id}?type=3")
    print(f"HTTP {r.status_code}: {r.text[:300]}")
    if not r.ok:
        sys.exit(1)


def cmd_set_acl(m: MSTR, args):
    m.login(identity=bool(args.model_id))
    _apply_acl(m, args.object_id, args.grant, model_id=args.model_id, sub_type=args.sub_type,
               denies=args.deny, replace=bool(getattr(args, "replace_trustee", False)))


def cmd_add_security_filter(m: MSTR, args):
    m.login(identity=True)
    if not _apply_security_filter(m, args.model_id, args.spec):
        die("security filter created, but its members are not bound (see the WARN above)")


def cmd_translate(m: MSTR, args):
    m.login(identity=True)
    ok, total = _apply_translations(m, args.model_id, args.entry, default_sub_type=args.sub_type)
    if ok < total:
        die(f"translations: {total - ok} of {total} object(s) not updated (see the WARN lines above)")


def cmd_certify(m: MSTR, args):
    m.login()
    if not _certify(m, args.object_id):
        die(f"certify {args.object_id} failed")


def _load_records_file(path: str, list_keys=("users", "items", "rows", "members")) -> list:
    ext = path.lower().rsplit(".", 1)[-1]
    if ext == "csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            return [{str(k).strip(): v for k, v in row.items()} for row in csv.DictReader(f)]
    data = load_structured_file(path)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in list_keys:
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    die(f"{path}: expected a list, object, or CSV rows")


def _first_present(row: dict, names: list[str]):
    for name in names:
        for key in (name, name.lower(), name.upper()):
            value = row.get(key)
            if value not in (None, ""):
                return value
    return None


def _split_list_value(value) -> list:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in _re.split(r"[;,]", str(value)) if v.strip()]


def _parse_bool(value, default=None):
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "t", "yes", "y", "1", "enabled"}:
        return True
    if text in {"false", "f", "no", "n", "0", "disabled"}:
        return False
    return default


def _user_identifier_from_record(row) -> str:
    if isinstance(row, str):
        return row
    if not isinstance(row, dict):
        return ""
    return str(_first_present(row, ["id", "username", "email", "name", "fullName", "full_name", "displayName"]) or "")


def cmd_resolve_users(m: MSTR, args):
    """Resolve user IDs from names, usernames, emails, or files before security/admin writes."""
    m.login()
    inputs = list(args.lookup_user or [])
    for path in args.file or []:
        inputs.extend(_user_identifier_from_record(row) for row in _load_records_file(path))
    inputs = [i.strip() for i in inputs if str(i).strip()]

    resolved, ambiguous, unresolved = [], [], []
    for value in inputs:
        candidates = _resolve_member_candidates(m, value, limit=args.limit)
        exact = _exact_member_matches(candidates, value)
        if not candidates:
            unresolved.append(value)
        elif len(exact) == 1:
            resolved.append(exact[0])
        elif args.first:
            resolved.append((exact or candidates)[0])
        else:
            # A lone substring match is a guess, not an answer (the same rule security
            # filters use): report it for a human to confirm.
            ambiguous.append({"input": value, "candidates": candidates,
                              "reason": f"{len(exact)} exact match(es)" if exact else "no exact match"})

    print(json.dumps({
        "ok": True,
        "resolved": resolved,
        "ambiguous": ambiguous,
        "unresolved": unresolved,
    }, indent=2))


def cmd_search_objects(m: MSTR, args):
    """Quick Search wrapper for finding object IDs before updates."""
    m.login()
    params = {
        "name": args.name,
        "pattern": args.pattern,
        "getAncestors": str(args.ancestors).lower(),
        "offset": args.offset,
        "limit": args.limit,
    }
    if args.type:
        params["type"] = [int(t) for t in args.type]
    if args.project_id_filter:
        params["projectId"] = args.project_id_filter
    r = m.get("/api/searches/results", params=params)
    if not r.ok:
        die(f"search-objects: {r.status_code} {r.text[:300]}")
    body = r.json() if r.text else {}
    rows = []
    for item in _items_from_response(body, "result"):
        subtype = item.get("subtype") or item.get("subType")
        if args.subtype and str(subtype) != str(args.subtype):
            continue
        rows.append({k: v for k, v in {
            "id": item.get("id") or item.get("objectId"),
            "name": item.get("name"),
            "type": item.get("type"),
            "subtype": subtype,
            "projectId": item.get("projectId"),
            "owner": (item.get("owner") or {}).get("name") if isinstance(item.get("owner"), dict) else item.get("owner"),
            "ancestors": item.get("ancestors") if args.ancestors else None,
        }.items() if v not in (None, "")})
    print(json.dumps({
        "ok": True,
        "count": len(rows),
        "objects": rows if not args.raw else body,
    }, indent=2))


# Path and write verbs per the 2026 spec. Data-model objects (needs_model) live under
# /api/model/dataModels/{id}; filters, transformations and hierarchies are project-level
# objects (the spec has no data-model sub-resource for them). `metric` is a derived metric
# (/metrics, PUT); fact metrics are `fact_metric`. schema_edit marks schema objects, whose
# edits need a schemaEdit changeset. Every write runs in a changeset.
MODEL_OBJECT_KINDS = {
    "data_model": {"path": "/api/model/dataModels/{model_id}", "needs_model": True, "verbs": ("PATCH",)},
    "attribute": {"path": "/api/model/dataModels/{model_id}/attributes/{object_id}", "needs_model": True,
                  "verbs": ("PATCH",)},
    "fact_metric": {"path": "/api/model/dataModels/{model_id}/factMetrics/{object_id}", "needs_model": True,
                    "verbs": ("PATCH",)},
    "metric": {"path": "/api/model/dataModels/{model_id}/metrics/{object_id}", "needs_model": True,
               "verbs": ("PUT",)},
    "derived_metric": {"alias": "metric"},
    "table": {"path": "/api/model/dataModels/{model_id}/tables/{object_id}", "needs_model": True, "verbs": ("PATCH",)},
    "security_filter": {"path": "/api/model/dataModels/{model_id}/securityFilters/{object_id}", "needs_model": True,
                        "verbs": ("PUT",)},
    "project_attribute": {"path": "/api/model/attributes/{object_id}", "needs_model": False, "verbs": ("PATCH",),
                          "schema_edit": True},
    "legacy_attribute": {"alias": "project_attribute"},
    "project_metric": {"path": "/api/model/metrics/{object_id}", "needs_model": False, "verbs": ("PUT",)},
    "legacy_metric": {"alias": "project_metric"},
    "project_fact": {"path": "/api/model/facts/{object_id}", "needs_model": False, "verbs": ("PUT",),
                     "schema_edit": True},
    "project_table": {"path": "/api/model/tables/{object_id}", "needs_model": False, "verbs": ("PATCH",),
                      "schema_edit": True},
    "project_filter": {"path": "/api/model/filters/{object_id}", "needs_model": False, "verbs": ("PUT",)},
    "filter": {"alias": "project_filter"},
    "project_transformation": {"path": "/api/model/transformations/{object_id}", "needs_model": False,
                               "verbs": ("PATCH",), "schema_edit": True},
    "transformation": {"alias": "project_transformation"},
    "project_hierarchy": {"path": "/api/model/hierarchies/{object_id}", "needs_model": False, "verbs": ("PATCH",),
                          "schema_edit": True},
    "hierarchy": {"alias": "project_hierarchy"},
}


def _kind_info(kind: str) -> dict:
    info = MODEL_OBJECT_KINDS.get(kind)
    if not info:
        die(f"unknown kind '{kind}'. expected one of {sorted(MODEL_OBJECT_KINDS)}")
    while "alias" in info:
        info = MODEL_OBJECT_KINDS[info["alias"]]
    return info


def _model_object_path(kind: str, model_id: str, object_id: str) -> tuple[str, dict]:
    info = _kind_info(kind)
    if info.get("needs_model") and not model_id:
        die(f"--model-id is required for kind {kind}")
    if "{object_id}" in info["path"] and not object_id:
        die(f"--object-id is required for kind {kind}")
    path = info["path"].format(model_id=model_id or object_id, object_id=object_id or model_id)
    return path, info


def _expression_params(args) -> dict:
    params = {}
    if getattr(args, "show_expression_as", None):
        params["showExpressionAs"] = args.show_expression_as
    if getattr(args, "fields", None):
        params["fields"] = args.fields
    if getattr(args, "show_advanced_properties", False):
        params["showAdvancedProperties"] = "true"
    return params


def cmd_export_model(m: MSTR, args):
    """The model's full definition (tables, columns, attributes with their lookup tables, metrics) to a private
    file: what assess_model.py lookups --model-export reads to name existing lookup tables."""
    m.login(identity=False)
    r = m.post(f"/api/model/dataModels/{args.model_id}/export")
    if not r.ok:
        die(f"export-model: {format_mstr_error(r)}")
    _write_private(args.out, r.json())
    body = r.json()
    model = (body.get("model") or [{}])[0] if isinstance(body.get("model"), list) else body.get("model") or {}
    print(json.dumps({"ok": True, "saved_to": args.out, "tables": len(model.get("tables") or []),
                      "attributes": len(model.get("attributes") or []),
                      "note": "holds IDs of your tenant: keep it out of repos"}, indent=2))


def cmd_get_model_object(m: MSTR, args):
    path, _ = _model_object_path(args.kind, args.model_id, args.object_id)
    m.login(identity=False)
    r = m.get(path, params=_expression_params(args) or None)
    out = {"ok": r.ok, "status": r.status_code, "path": path}
    try:
        out["body"] = r.json()
    except ValueError:
        out["body"] = r.text[:args.text_limit]
        out["body_truncated"] = len(r.text) > args.text_limit
    if args.out and out.get("body") is not None:
        _write_private(args.out, out["body"])
        out["saved_to"] = args.out
    print(json.dumps(out, indent=2))


def cmd_patch_model_object(m: MSTR, args):
    """Patch/put a Mosaic-contained or legacy schema object through Modeling Service.
    The verb defaults to the one the spec defines for the kind; schema objects
    (project_attribute/fact/table/transformation/hierarchy) get a schemaEdit changeset."""
    if not args.yes:
        die("patch-model-object requires --yes after you have reviewed the target ID and request body")
    body = _load_json_arg(args.json, args.json_file)
    if body is None:
        die("patch-model-object requires --json or --json-file")
    path, info = _model_object_path(args.kind, args.model_id, args.object_id)
    verbs = info.get("verbs") or ("PATCH",)
    method = (getattr(args, "method", None) or verbs[0]).upper()
    if method not in verbs:
        die(f"patch-model-object: kind {args.kind} is written with {'/'.join(verbs)} only "
            f"(the spec has no {method} for {info['path']})")
    schema_edit = bool(getattr(args, "schema_edit", False) or info.get("schema_edit"))
    m.login(identity=info.get("needs_model", False))
    before = None
    if args.before_out or args.include_before:
        r0 = m.get(path, params=_expression_params(args) or None)
        if r0.ok:
            before = r0.json() if r0.text else {}
            if args.before_out:
                _write_private(args.before_out, before)

    cs = open_cs(m, schema_edit=schema_edit)
    try:
        request = getattr(m, method.lower())
        r = request(path, params=_expression_params(args) or None, json=body)
        if not r.ok:
            discard_cs(m, cs)
            die(f"patch-model-object {method} {path}: {format_mstr_error(r)}")
        updated = r.json() if r.text else {}
        commit_cs(m, cs)
        verify = m.get(path, params=_expression_params(args) or None)
        out = {
            "ok": verify.ok,
            "method": method,
            "path": path,
            "changeset": cs,
            "schema_edit": schema_edit,
            "updated_in_changeset": updated,
            "verified": verify.json() if verify.ok and verify.text else verify.text[:args.text_limit],
        }
        if before is not None and args.include_before:
            out["before"] = before
        print(json.dumps(out, indent=2))
    finally:
        m.s.headers.pop("X-MSTR-MS-Changeset", None)


USER_CREATION_FIELDS = {
    "username": ["username", "user", "login", "loginName"],
    "fullName": ["fullName", "full_name", "name", "displayName"],
    "description": ["description"],
    "password": ["password"],
    "enabled": ["enabled", "status"],
    "passwordModifiable": ["passwordModifiable", "password_modifiable"],
    "passwordAutoExpire": ["passwordAutoExpire", "password_auto_expire"],
    "passwordExpirationDate": ["passwordExpirationDate", "password_expiration_date"],
    "passwordExpirationFrequency": ["passwordExpirationFrequency", "password_expiration_frequency"],
    "requireNewPassword": ["requireNewPassword", "require_new_password"],
    "standardAuth": ["standardAuth", "standard_auth"],
    "ldapdn": ["ldapdn", "ldap_dn"],
    "trustId": ["trustId", "trust_id"],
    "ssoScopes": ["ssoScopes", "sso_scopes"],
    "databaseAuthLogin": ["databaseAuthLogin", "database_auth_login"],
    "memberships": ["memberships", "groups", "groupIds", "group_ids"],
    "languageId": ["languageId", "language_id"],
}


def _redact_secrets(value):
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in {"password", "oldpassword", "newpassword"} else _redact_secrets(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(v) for v in value]
    return value


def _user_creation_payload(row, default_password: str = "") -> tuple[dict, str]:
    if not isinstance(row, dict):
        row = {"username": str(row)}
    body = dict(row.get("body") or {}) if isinstance(row.get("body"), dict) else {}
    for out_key, aliases in USER_CREATION_FIELDS.items():
        value = _first_present(row, aliases)
        if value in (None, ""):
            continue
        if out_key in {"enabled", "passwordModifiable", "passwordAutoExpire", "requireNewPassword", "standardAuth"}:
            parsed = _parse_bool(value)
            if parsed is not None:
                body[out_key] = parsed
        elif out_key in {"memberships", "ssoScopes"}:
            body[out_key] = _split_list_value(value)
        elif out_key == "passwordExpirationFrequency":
            body[out_key] = int(value)
        else:
            body[out_key] = str(value)

    email = str(_first_present(row, ["email", "mail", "defaultEmailAddress"]) or "").strip()
    if not body.get("username") and email:
        body["username"] = email
    if not body.get("fullName"):
        body["fullName"] = body.get("username")
    if default_password and not body.get("password"):
        body["password"] = default_password
    if not body.get("username") or not body.get("fullName"):
        die(f"user row missing username/fullName: {_redact_secrets(row)}")
    return body, email


def _has_exact_user_match(candidates: list[dict], username: str, email: str = "") -> dict:
    username = (username or "").lower()
    email = (email or "").lower()
    for cand in candidates:
        fields = [cand.get("id"), cand.get("username"), cand.get("name"), cand.get("fullName"), cand.get("email")]
        for value in fields:
            text = str(value or "").lower()
            if text and (text == username or (email and text == email)):
                return cand
    return {}


def cmd_create_users(m: MSTR, args):
    """Bulk create users from CSV/JSON/YAML. Dry-run by default; --yes performs writes."""
    rows = []
    for path in args.file:
        rows.extend(_load_records_file(path, list_keys=("users", "items", "rows")))
    default_password = os.environ.get(args.default_password_env, "")
    planned, created, skipped, errors = [], [], [], []

    if args.yes or args.check_existing:
        m.login()

    for row in rows:
        body, email = _user_creation_payload(row, default_password=default_password)
        existing = {}
        if args.yes or args.check_existing:
            candidates = _resolve_member_candidates(m, body["username"], limit=5)
            if email:
                candidates.extend(_resolve_member_candidates(m, email, limit=5))
            existing = _has_exact_user_match(_dedupe_by_id(candidates), body["username"], email)
        if existing and not args.allow_existing:
            skipped.append({"reason": "already_exists", "input": body["username"], "existing": existing})
            continue
        if not args.yes:
            planned.append({"user": _redact_secrets(body), "email_address": email or None})
            continue

        params = {}
        source_user_id = _first_present(row, ["sourceUserId", "source_user_id"]) if isinstance(row, dict) else None
        if source_user_id or args.source_user_id:
            params["sourceUserId"] = source_user_id or args.source_user_id
        r = m.post("/api/users", params=params or None, json=body)
        if not r.ok:
            errors.append({"user": body["username"], "status": r.status_code, "body": r.text[:500]})
            continue
        user = r.json() if r.text else {}
        user_id = user.get("id")
        address = None
        if email and user_id and not args.skip_email_address:
            ar = m.post(f"/api/users/{user_id}/addresses", json={
                "name": args.email_address_name,
                "deliveryMode": "EMAIL",
                "device": "GENERIC_EMAIL",
                "value": email,
                "isDefault": True,
            })
            address = {"ok": ar.ok, "status": ar.status_code}
            if ar.ok and ar.text:
                address["body"] = ar.json()
            elif not ar.ok:
                address["body"] = ar.text[:300]
        created.append({"user": _redact_secrets(user), "email_address": address})

    print(json.dumps({
        "ok": not errors,
        "dry_run": not args.yes,
        "planned": planned,
        "created": created,
        "skipped": skipped,
        "errors": errors,
    }, indent=2))


def _require_unverified_ok(args, command: str, why: str) -> None:
    """Gate for commands whose request body no live tenant (or the spec) has confirmed."""
    if not getattr(args, "unverified_ok", False):
        die(f"{command} is UNVERIFIED: {why} Re-run with --unverified-ok to send it anyway, and "
            f"check the result in the model editor.")


def cmd_create_transformation(m: MSTR, args):
    """Create a time-shift transformation.
    --member: 'attributeId=offset' (repeatable). offset is integer (-1 = prior period)."""
    _require_unverified_ok(args, "create-transformation",
                           "the spec has no data-model transformations endpoint (transformations are "
                           "project-level: POST /api/model/transformations, with a different body).")
    members = []
    for spec in args.member:
        aid, _, off = spec.partition("=")
        try:
            members.append({"attribute": {"objectId": aid.strip(), "subType": "attribute"},
                            "offset": int(off)})
        except ValueError:
            die(f"--member {spec!r}: expected attributeId=<integer offset>")
    m.login(identity=True)
    cs = open_cs(m)
    r = m.post(f"/api/model/dataModels/{args.model_id}/transformations",
               json={"information":{"name": args.name}, "members": members})
    tid = (_json_or(r, {}).get("information") or {}).get("objectId") if r.ok else None
    if not tid:
        discard_cs(m, cs)
        die(f"create transformation: {r.status_code} {r.text[:300]}")
    commit_cs(m, cs)
    print(json.dumps({"ok": True, "transformation_id": tid}, indent=2))


# ── Derived metrics: compound (tree), conditional (embedded filter), delete ───────────────────
# Verified write paths, all on /api/model/dataModels/{id}/metrics in one changeset each:
# memory/reference_mosaic_derived_metrics.md §0 (tree compounds), §0c (conditional metrics),
# §0d (level rules and counts on periodic-snapshot facts).

DESCRIPTION_MAX = 250   # longer descriptions are cut in the UI (memory/feedback_mosaic_ship_bar.md)
_TREE_OPS = {"+": "plus", "-": "minus", "*": "times", "/": "divide"}
_OBJECT_ID = _re.compile(r"[0-9A-Fa-f]{32}")
_ARITH_TOKEN = _re.compile(r"\s*(?:(?P<ref>\[[^\]]+\])|(?P<num>\d+(?:\.\d+)?)|(?P<op>[()+\-*/])"
                           r"|(?P<id>[0-9A-Fa-f]{32})(?![0-9A-Za-z]))")


def _check_description(text: str) -> str:
    text = text or ""
    if len(text) > DESCRIPTION_MAX:
        die(f"description is {len(text)} characters; the limit is {DESCRIPTION_MAX}")
    return text


def compound_metric_tree(formula: str, resolve) -> dict:
    """Arithmetic over metrics → expression tree (§0): `[A] - [B]`, `([A] + [B]) / [C]`,
    `[A] * 100`, `-[A]`. Operands are `[name]`, `[objectId]`, `[fact_metric:name]`,
    `[metric:name]`, or a bare 32-character objectId (the old space-separated syntax).
    Constants are whole numbers (the tree's int64 variant; write 0.5 as 1 / 2).
    resolve(text) returns the operand's {"objectId", "subType", "name"}."""
    s, pos, toks = formula.strip(), 0, []
    while pos < len(s):
        mt = _ARITH_TOKEN.match(s, pos)
        if not mt or mt.end() == pos:
            die(f"formula: cannot read '{s[pos:pos + 20]}' (operands are [metric name] or an objectId; "
                f"operators + - * / and parentheses)")
        pos = mt.end()
        toks.append(mt)
    if not toks:
        die("formula is empty")
    i = 0

    def peek():
        return toks[i].group("op") if i < len(toks) else None

    def factor():
        nonlocal i
        if i >= len(toks):
            die("formula ends after an operator")
        t = toks[i]
        i += 1
        if t.group("ref") or t.group("id"):
            raw = t.group("ref")[1:-1].strip() if t.group("ref") else t.group("id")
            return {"type": "object_reference", "target": resolve(raw)}
        if t.group("num"):
            if "." in t.group("num"):
                die(f"formula: constant {t.group('num')} is not a whole number (write 0.5 as 1 / 2)")
            return {"type": "constant", "variant": {"type": "int64", "value": t.group("num")}}
        if t.group("op") == "(":
            node = expr()
            if peek() != ")":
                die("formula: missing )")
            i += 1
            return node
        if t.group("op") == "-":
            return {"type": "operator", "function": "unary_minus", "children": [factor()]}
        die(f"formula: unexpected '{t.group(0).strip()}'")

    def term():
        nonlocal i
        node = factor()
        while peek() in ("*", "/"):
            op = peek()
            i += 1
            node = {"type": "operator", "function": _TREE_OPS[op], "children": [node, factor()]}
        return node

    def expr():
        nonlocal i
        node = term()
        while peek() in ("+", "-"):
            op = peek()
            i += 1
            node = {"type": "operator", "function": _TREE_OPS[op], "children": [node, term()]}
        return node

    tree = expr()
    if i != len(toks):
        die(f"formula: unexpected '{toks[i].group(0).strip()}'")
    if not any(t.group("ref") or t.group("id") for t in toks):
        die("formula references no metric")
    return tree


def compound_metric_body(name: str, tree: dict, *, description: str = "", join: str = "",
                         fmt_values: list | None = None) -> dict:
    """/metrics body for a compound metric: expression tree with dimty null (a compound
    rejects a dimty, 8004d711). join "outer" keeps a row when one operand has no data for it
    (the blank operand counts as 0); the default inner join drops that row."""
    info = {"name": name, "subType": "metric"}
    if description:
        info["description"] = description
    body: dict = {"information": info, "expression": {"tree": tree}, "dimty": None}
    if join:
        body["formulaJoinType"] = join
    if fmt_values:
        body["format"] = {"header": [], "values": list(fmt_values)}
    return body


def _metric_operand_index(fact_metrics: list, derived_metrics: list) -> dict:
    """lower(name) and upper(objectId) → candidate operand refs, fact and derived metrics."""
    index: dict = {}
    for items, sub_type in ((fact_metrics, "fact_metric"), (derived_metrics, "metric")):
        for o in items:
            info = (o.get("information") or {}) if isinstance(o, dict) else {}
            if not info.get("objectId"):
                continue
            ref = {"objectId": info["objectId"], "subType": sub_type, "name": info.get("name") or ""}
            index.setdefault(ref["name"].lower(), []).append(ref)
            index.setdefault(ref["objectId"].upper(), []).append(ref)
    return index


def _resolve_metric_operand(index: dict, raw: str) -> dict:
    want = None
    if ":" in raw and raw.split(":", 1)[0].strip().lower() in ("fact_metric", "metric"):
        want, raw = (p.strip() for p in raw.split(":", 1))
        want = want.lower()
    key = raw.upper() if _OBJECT_ID.fullmatch(raw) else raw.lower()
    hits = [h for h in index.get(key, []) if want is None or h["subType"] == want]
    if not hits:
        die(f"formula: no fact or derived metric matches [{raw}]")
    if len(hits) > 1:
        die(f"formula: [{raw}] matches {len(hits)} metrics; write [fact_metric:{raw}], [metric:{raw}] or the objectId")
    return dict(hits[0])


def _list_model_metrics(m: MSTR, model_id: str) -> tuple[list, list]:
    mp = f"/api/model/dataModels/{model_id}"
    facts, failed = _list_all(m, f"{mp}/factMetrics", "factMetrics")
    if failed is not None: die(f"list factMetrics: {format_mstr_error(failed)}")
    derived, failed = _list_all(m, f"{mp}/metrics", "metrics")
    if failed is not None: die(f"list metrics: {format_mstr_error(failed)}")
    return facts, derived


def _named(items: list, name_or_id: str) -> list[dict]:
    """information blocks whose name (case-insensitive) or objectId matches."""
    infos = [o.get("information") or {} for o in items if isinstance(o, dict)]
    return [i for i in infos if (i.get("objectId") or "").upper() == name_or_id.upper()
            or (i.get("name") or "").lower() == name_or_id.lower()]


def cmd_create_compound_metric(m: MSTR, args):
    """Create (or, with --replace, rewrite in place) a compound derived metric from
    arithmetic over existing fact or derived metrics, e.g. '[Sales] - [Returns]' or
    '([Units Sold] / [Units Available]) * 100'.

    Verified write path (memory/reference_mosaic_derived_metrics.md §0): expression tree
    with dimty null, POST /metrics (create) or a full-body PUT /metrics/{id} (replace), in
    one changeset. --join outer keeps groups where one operand has no rows (blank counts
    as 0); without it such groups drop out of the result. On --replace, the description and
    number format are kept unless given again; pass --join again (the read-back may not
    carry it).
    """
    if getattr(args, "unverified_ok", False):
        print("[note] --unverified-ok is no longer needed: create-compound-metric uses the verified "
              "tree path", file=sys.stderr)
    description = _check_description(getattr(args, "description", "") or "")
    m.login(identity=True)
    mp = f"/api/model/dataModels/{args.model_id}"
    q = {"showAdvancedProperties": "true"}
    facts, derived = _list_model_metrics(m, args.model_id)
    index = _metric_operand_index(facts, derived)
    tree = compound_metric_tree(args.formula, lambda raw: _resolve_metric_operand(index, raw))
    fmt_values = _resolve_metric_format(args.format) if getattr(args, "format", None) else None

    existing = [i for i in _named(derived, args.name) if (i.get("name") or "").lower() == args.name.lower()]
    if len(existing) > 1:
        die(f"'{args.name}' names {len(existing)} derived metrics: {[e.get('objectId') for e in existing]}")
    replace = bool(getattr(args, "replace", False))
    if existing and not replace:
        die(f"a derived metric named '{args.name}' already exists ({existing[0].get('objectId')}); "
            f"pass --replace to rewrite it in place")
    if replace and not existing:
        die(f"--replace: no derived metric named '{args.name}' on this model")
    target_id = existing[0]["objectId"] if existing else None
    if target_id:
        r = m.get(f"{mp}/metrics/{target_id}", params=q)
        if not r.ok: die(f"read '{args.name}': {format_mstr_error(r)}")
        cur = _json_or(r, {})
        description = description or (cur.get("information") or {}).get("description") or ""
        if fmt_values is None:
            fmt_values = (cur.get("format") or {}).get("values")
    join = getattr(args, "join", None) or ""
    body = compound_metric_body(args.name, tree, description=description, join=join, fmt_values=fmt_values)
    method, path = ("PUT", f"{mp}/metrics/{target_id}") if target_id else ("POST", f"{mp}/metrics")
    if getattr(args, "dry_run", False):
        print(json.dumps({"ok": True, "dry_run": True,
                          "plan": [{"method": method, "path": path, "params": q, "body": body}]}, indent=2))
        return
    cs = open_cs(m)
    try:
        r = (m.put if target_id else m.post)(path, params=q, json=body)
        if not r.ok: die(f"{'replace' if target_id else 'create'} metric: {format_mstr_error(r)}")
        metric_id = target_id or (_json_or(r, {}).get("information") or {}).get("objectId")
        if not metric_id: die(f"create metric: no objectId in response {r.text[:200]}")
        commit_cs(m, cs)
    except BaseException:  # die() raises SystemExit; discard on every failure path
        discard_cs(m, cs)
        raise
    print(json.dumps({"ok": True, "metric_id": metric_id,
                      "action": "replaced" if target_id else "created"}, indent=2))


# Aggregate-function objectIds are platform constants (same on every tenant) —
# memory/reference_mosaic_derived_metrics.md "Well-known function objectIds". Count is sent by
# name without a target, the shape verified live for both counting forms (§0d).
AGG_FUNCTION_IDS = {"Sum": "8107C31BDD9911D3B98100C04F2233EA",
                    "Avg": "8107C31DDD9911D3B98100C04F2233EA"}
CONDITIONAL_FUNCTIONS = ("Avg", "Count", "Sum")

# A metric's own level rule on a period-type attribute (snapshot date, month, week). The
# *_fact rules read the latest/first period among the rows that qualify for the metric, and do
# so per group: "latest" for a group is that group's last period with qualifying rows, not
# the latest period overall. The *_lookup rules were accepted by the service but answered the
# same way on an in-memory model (§0d); for "latest period overall", filter on a
# latest-period flag column instead.
LEVEL_RULE_AGGREGATIONS = {"last": "last_in_fact", "first": "first_in_fact",
                           "last_lookup": "last_in_relationship", "first_lookup": "first_in_relationship"}


def conditional_metric_tokens(function: str, source: dict, filter_id: str = "", *,
                              distinct: bool = False) -> list[dict]:
    """Tokens for `Fn<UseLookupForAttributes=False>([Source]){~+}`; with filter_id,
    the `<embedded filter>` binding goes just before end_of_text. distinct=True (Count only)
    adds `Distinct=True` for a distinct count of an attribute.

    source is {"objectId", "subType": "fact_metric" | "attribute", "name"}. Every token
    carries a "value", end_of_text included — omitting one fails with 8004cb04.
    """
    if function in AGG_FUNCTION_IDS:
        head = {"type": "function", "value": function,
                "target": {"objectId": AGG_FUNCTION_IDS[function], "subType": "function", "name": function}}
    else:
        head = {"type": "function", "value": function}
    tokens = [head, {"type": "character", "value": "<"}]
    if distinct:
        tokens += [{"type": "identifier", "value": "Distinct"},
                   {"type": "function", "value": "="},
                   {"type": "boolean", "value": "True"},
                   {"type": "character", "value": ","}]
    tokens += [
        {"type": "identifier", "value": "UseLookupForAttributes"},
        {"type": "function", "value": "="},
        {"type": "boolean", "value": "False"},
        {"type": "character", "value": ">"},
        {"type": "character", "value": "("},
        {"type": "object_reference", "value": f"[{source['name']}]", "target": source},
        {"type": "character", "value": ")"},
    ]
    tokens += [{"type": "character", "value": c} for c in "{~+}"]
    if filter_id:
        tokens += [
            {"type": "character", "value": "<"},
            {"type": "object_reference", "value": "",
             "target": {"objectId": filter_id, "subType": "filter", "isEmbedded": True}},
            {"type": "character", "value": ">"},
        ]
    return tokens + [{"type": "end_of_text", "value": ""}]


def level_rule_unit(attribute: dict, rule: str) -> dict:
    """dimty unit that makes the metric read the latest/first value of a period-type attribute
    (LEVEL_RULE_AGGREGATIONS). A metric with its own unit keeps the rule under a filter; a
    filtered metric over a fact metric that has the rule loses it (§0d)."""
    return {"dimtyUnitType": "attribute",
            "target": {"objectId": attribute["objectId"], "subType": "attribute", "name": attribute["name"]},
            "aggregation": LEVEL_RULE_AGGREGATIONS[rule], "filtering": "apply", "groupBy": True}


def conditional_metric_body(name: str, function: str, source: dict, fmt_values: list | None = None,
                            description: str = "", filter_id: str = "", *, distinct: bool = False,
                            level: tuple[dict, str] | None = None) -> dict:
    """/metrics body for a conditional metric. Without filter_id it is the
    unfiltered create (POST); with it, the binding update (PUT) — filter-bound
    tokens plus the conditionality block. level=(attribute ref, rule) adds a level rule."""
    info = {"name": name, "subType": "metric"}
    if description:
        info["description"] = description
    units = [{"dimtyUnitType": "report_base_level", "aggregation": "normal",
              "filtering": "apply", "groupBy": True}]
    if level:
        units.append(level_rule_unit(*level))
    body = {
        "information": info,
        "expression": {"text": f"{function}({{{source['name']}}})",
                       "tokens": conditional_metric_tokens(function, source, filter_id, distinct=distinct)},
        "dimty": {"dimtyUnits": units, "excludeAttribute": False, "allowAddingUnit": True},
        "format": {"header": [], "values": list(fmt_values or [])},
    }
    if filter_id:
        body["conditionality"] = {
            "filter": {"objectId": filter_id, "subType": "filter", "isEmbedded": True},
            # removeElements False: the report's own filter on the same attribute still applies. With True the
            # condition replaced it, so a customer-filtered report read the whole-estate total (verified live).
            "embedMethod": "report_into_metric_filter", "removeElements": False}
    return body


def _element_list_predicate(attribute: dict, elements: list[str]) -> dict:
    return {"type": "predicate_element_list",
            "predicateTree": {
                "attribute": attribute,
                "elements": [{"elementId": f"h{v};{attribute['objectId']}", "display": v} for v in elements],
                "function": "in"}}


def embedded_filter_body(conditions: list[tuple[dict, list[str]]]) -> dict:
    """POST /metrics/{id}/embeddedObjects body: one `<attribute> IN (elements)` list per
    condition, ANDed under an operator node when there are several (verified with 2 and 3).
    elements are ID-form values; each elementId is `h<value>;<attribute objectId>`."""
    if not conditions:
        die("a conditional metric needs at least one condition")
    parts = [_element_list_predicate(a, list(e)) for a, e in conditions]
    tree = parts[0] if len(parts) == 1 else {"type": "operator", "function": "and", "children": parts}
    return {"subType": "filter", "qualification": {"tree": tree}}


def embedded_element_filter_body(attribute: dict, elements: list[str]) -> dict:
    """Single-condition form of embedded_filter_body."""
    return embedded_filter_body([(attribute, elements)])


def attribute_name_candidates(name: str) -> list[str]:
    """The connector shows an attribute's forms as '<attribute>__<form>' or '<attribute> (<form>)'. Return the
    name as given, then the attribute name behind such a form name (lower case)."""
    n = (name or "").strip().lower()
    out = [n]
    if "__" in n:
        out.append(n.split("__", 1)[0].strip())
    hit = _re.match(r"^(.*\S)\s+\(([^()]+)\)$", n)
    if hit:
        out.append(hit.group(1))
    return list(dict.fromkeys(x for x in out if x))


def resolve_model_object_ref(m: MSTR, model_id: str, collection: str, sub_type: str,
                             name_or_id: str) -> dict:
    """Name (case-insensitive) or objectId → {"objectId", "subType", "name"} from a
    Modeling list endpoint (factMetrics, attributes). Dies unless exactly one matches."""
    items, failed = _list_all(m, f"/api/model/dataModels/{model_id}/{collection}", collection)
    if failed is not None: die(f"list {collection}: {format_mstr_error(failed)}")
    infos = [o.get("information") or {} for o in items if isinstance(o, dict)]
    hits = [i for i in infos if (i.get("objectId") or "").upper() == name_or_id.upper()
            or (i.get("name") or "").lower() == name_or_id.lower()]
    for alt in attribute_name_candidates(name_or_id)[1:]:   # a connector form name: its attribute
        if hits:
            break
        hits = [i for i in infos if (i.get("name") or "").lower() == alt]
    if not hits:
        die(f"no {collection} entry matches '{name_or_id}'")
    if len(hits) > 1:
        die(f"'{name_or_id}' is ambiguous in {collection}: {[h.get('objectId') for h in hits]}. Pass the objectId.")
    return {"objectId": hits[0]["objectId"], "subType": sub_type, "name": hits[0]["name"]}


def cmd_create_conditional_metric(m: MSTR, args):
    """Create a conditional (filtered) derived metric scoped by a filter embedded in the
    metric: one `<attribute> IN (<elements>)` condition, plus any --and conditions (ANDed).

    What it aggregates (exactly one):
      --source-metric F [--function Sum|Avg|Count]   Fn(<fact metric>); Count counts fact rows
      --count-attribute A                            Count<Distinct=True>(<attribute>)
    Optional level rule: --level-rule last|first|last_lookup|first_lookup with
    --level-attribute P (a period-type attribute) makes the metric read the latest/first
    period's value — needed on periodic-snapshot facts, where a filtered metric over a
    fact metric that carries the rule loses it (memory/reference_mosaic_derived_metrics.md
    §0d). A distinct count of an attribute ignores the rule unless that attribute is
    related to the period attribute; count a fact metric (--function Count) instead.

    Verified write path (§0c), ONE changeset: POST /metrics (unfiltered) → POST
    /metrics/{id}/embeddedObjects (the filter) → PUT /metrics/{id} (filter-bound tokens +
    conditionality) → commit. Any failure discards the changeset. The number format is
    --format, else the source fact metric's (Sum/Avg), else `integer` (counts).
    """
    source_name = getattr(args, "source_metric", None)
    count_attr = getattr(args, "count_attribute", None)
    function = getattr(args, "function", None)
    if bool(source_name) == bool(count_attr):
        die("pass exactly one of --source-metric or --count-attribute")
    if count_attr and function not in (None, "Count"):
        die("--count-attribute always counts distinct elements; drop --function")
    function = "Count" if count_attr else (function or "Sum")
    if function not in CONDITIONAL_FUNCTIONS:
        die(f"--function must be one of {list(CONDITIONAL_FUNCTIONS)}")
    level_rule = getattr(args, "level_rule", None)
    level_attr_name = getattr(args, "level_attribute", None)
    if bool(level_rule) != bool(level_attr_name):
        die("--level-rule and --level-attribute go together")
    if level_rule and level_rule not in LEVEL_RULE_AGGREGATIONS:
        die(f"--level-rule must be one of {sorted(LEVEL_RULE_AGGREGATIONS)}")
    extra = [list(c) for c in (getattr(args, "and_conditions", None) or [])]
    for c in extra:
        if len(c) < 2:
            die(f"--and {' '.join(c)}: give an attribute and at least one element value")
    if not args.elements:
        die("--elements needs at least one value")
    description = _check_description(getattr(args, "description", "") or "")
    fmt_preset = getattr(args, "format", None)

    m.login(identity=True)
    mp = f"/api/model/dataModels/{args.model_id}"
    if count_attr:
        source = resolve_model_object_ref(m, args.model_id, "attributes", "attribute", count_attr)
    else:
        source = resolve_model_object_ref(m, args.model_id, "factMetrics", "fact_metric", source_name)
    conditions = [(resolve_model_object_ref(m, args.model_id, "attributes", "attribute", args.attribute),
                   list(args.elements))]
    for c in extra:
        conditions.append((resolve_model_object_ref(m, args.model_id, "attributes", "attribute", c[0]), c[1:]))
    level = None
    if level_rule:
        level = (resolve_model_object_ref(m, args.model_id, "attributes", "attribute", level_attr_name),
                 level_rule)
    if fmt_preset:
        fmt_values = _resolve_metric_format(fmt_preset)
    elif function == "Count":
        fmt_values = _resolve_metric_format("integer")
    else:
        r = m.get(f"{mp}/factMetrics/{source['objectId']}")
        if not r.ok: die(f"source metric GET: {format_mstr_error(r)}")
        fmt_values = ((r.json() or {}).get("format") or {}).get("values")
    distinct = bool(count_attr)

    def body(filter_id=""):
        return conditional_metric_body(args.name, function, source, fmt_values, description,
                                       filter_id=filter_id, distinct=distinct, level=level)

    q = {"showAdvancedProperties": "true"}
    filter_body = embedded_filter_body(conditions)
    if getattr(args, "dry_run", False):
        print(json.dumps({"ok": True, "dry_run": True, "plan": [
            {"step": "create", "method": "POST", "path": f"{mp}/metrics", "params": q, "body": body()},
            {"step": "embed filter", "method": "POST", "path": f"{mp}/metrics/<new metric id>/embeddedObjects",
             "params": {"showExpressionAs": "tree"}, "body": filter_body},
            {"step": "bind filter", "method": "PUT", "path": f"{mp}/metrics/<new metric id>", "params": q,
             "body": body("<embedded filter id>")},
        ]}, indent=2))
        return
    cs = open_cs(m)
    try:
        r = m.post(f"{mp}/metrics", params=q, json=body())
        if not r.ok: die(f"create metric: {format_mstr_error(r)}")
        metric_id = r.json()["information"]["objectId"]
        r = m.post(f"{mp}/metrics/{metric_id}/embeddedObjects", params={"showExpressionAs": "tree"},
                   json=filter_body)
        if not r.ok: die(f"embed filter: {format_mstr_error(r)}")
        emb = r.json() or {}
        filter_id = emb.get("id") or (emb.get("information") or {}).get("objectId")
        if not filter_id: die(f"embed filter: no id in response {str(emb)[:200]}")
        r = m.put(f"{mp}/metrics/{metric_id}", params=q, json=body(filter_id))
        if not r.ok: die(f"bind filter: {format_mstr_error(r)}")
        commit_cs(m, cs)
    except BaseException:  # die() raises SystemExit; discard on every failure path
        discard_cs(m, cs)
        raise
    print(json.dumps({"ok": True, "metric_id": metric_id, "embedded_filter_id": filter_id}, indent=2))


def _metric_references(definition: dict, target: dict) -> bool:
    """True when a derived metric's definition (tokens read-back) refers to target: its
    objectId anywhere in the body, or `{name}` / `[name]` in the expression text."""
    blob = json.dumps(definition).upper()
    if target["objectId"].upper() in blob:
        return True
    text = ((definition.get("expression") or {}).get("text") or "").lower()
    name = (target.get("name") or "").lower()
    return bool(name) and (f"{{{name}}}" in text or f"[{name}]" in text)


def cmd_delete_derived_metric(m: MSTR, args):
    """Delete one derived metric (never a fact metric or attribute) in a changeset.

    Before deleting, reads every other derived metric of the model and lists the ones that
    refer to it; refuses when there are any, unless --force. (What the service does to a
    dependent metric when its operand is deleted has not been verified.) Requires --yes;
    --dry-run reports the target and its dependents and writes nothing.
    """
    key = getattr(args, "object_id", None) or getattr(args, "name", None)
    if not key: die("pass --name or --object-id")
    dry_run = bool(getattr(args, "dry_run", False))
    if not dry_run and not getattr(args, "yes", False):
        die("delete-derived-metric requires --yes after you have reviewed the target (try --dry-run first)")
    m.login(identity=True)
    mp = f"/api/model/dataModels/{args.model_id}"
    facts, derived = _list_model_metrics(m, args.model_id)
    hits = _named(derived, key)
    if not hits:
        if _named(facts, key):
            die(f"'{key}' is a fact metric; delete-derived-metric deletes derived metrics only")
        die(f"no derived metric matches '{key}'")
    if len(hits) > 1:
        die(f"'{key}' matches {len(hits)} derived metrics: {[h.get('objectId') for h in hits]}. Pass --object-id.")
    target = {"objectId": hits[0]["objectId"], "name": hits[0].get("name") or ""}

    dependents = []
    for o in derived:
        info = (o.get("information") or {}) if isinstance(o, dict) else {}
        oid = info.get("objectId")
        if not oid or oid.upper() == target["objectId"].upper():
            continue
        r = m.get(f"{mp}/metrics/{oid}", params={"showExpressionAs": "tokens", "showAdvancedProperties": "true"})
        if not r.ok: die(f"read '{info.get('name')}' to check dependents: {format_mstr_error(r)}")
        if _metric_references(_json_or(r, {}), target):
            dependents.append({"objectId": oid, "name": info.get("name")})
    report = {"target": target, "dependents": dependents}
    if dry_run:
        print(json.dumps({"ok": True, "dry_run": True, **report}, indent=2))
        return
    if dependents and not getattr(args, "force", False):
        die(f"'{target['name']}' is used by {len(dependents)} derived metric(s): "
            f"{[d['name'] for d in dependents]}. Delete or rewrite those first, or pass --force.")
    cs = open_cs(m)
    try:
        r = m.delete(f"{mp}/metrics/{target['objectId']}")
        if not r.ok: die(f"delete metric: {format_mstr_error(r)}")
        commit_cs(m, cs)
    except BaseException:
        discard_cs(m, cs)
        raise
    print(json.dumps({"ok": True, "deleted": target, "dependents": dependents}, indent=2))


# ── Once-per-entity (level) metrics and weighted averages on snapshot facts ────────────────────
# Verified shapes: memory/reference_mosaic_derived_metrics.md §0d rules 1 and 2. Tokens with a
# report_base_level dimty; functions go by name (as the service resolves them).

LEVEL_FUNCTIONS = ("Avg", "Max", "Min", "Sum")
_RBL_UNIT = {"dimtyUnitType": "report_base_level", "aggregation": "normal", "filtering": "apply", "groupBy": True}


def _tok(kind: str, value: str, target: dict | None = None) -> dict:
    t: dict = {"type": kind, "value": value}
    if target is not None:
        t["target"] = dict(target)
    return t


def _chars(text: str) -> list[dict]:
    return [_tok("character", c) for c in text]


def _per_entity(function: str, ref: dict, entity: dict) -> list[dict]:
    """`Fn([ref]) {~+, [entity]+}`: the value per entity, at report grain."""
    return ([_tok("function", function), _tok("character", "("), _tok("object_reference", f"[{ref['name']}]", ref),
             _tok("character", ")")] + _chars("{~+,")
            + [_tok("object_reference", f"[{entity['name']}]", entity)] + _chars("+}"))


def _outer(function: str, inner: list[dict]) -> list[dict]:
    """`Fn(<inner>) {~+}` closed with end_of_text."""
    return ([_tok("function", function), _tok("character", "(")] + inner + [_tok("character", ")")]
            + _chars("{~+}") + [_tok("end_of_text", "")])


def level_metric_tokens(source: dict, entity: dict, inner: str = "Max", outer: str = "Sum") -> list[dict]:
    """`Sum(Max([M]) {~+, [Entity]+}) {~+}`: one value per entity, then added across entities (§0d rule 1).
    Right at any grouping, unlike a first/last-value rule on the period."""
    return _outer(outer, _per_entity(inner, source, entity))


def weighted_average_tokens(value: dict, weight: dict, entity: dict) -> tuple[list[dict], list[dict]]:
    """Numerator `Sum(Max([A]){E} * Max([W]){E}){~+}` and denominator `Sum(Max([W]){E} * Max([A]){E} /
    Max([A]){E}){~+}` (§0d rule 2): `A/A` is NULL for entities with no value, so they drop out of the
    denominator as they do from the numerator."""
    num = _outer("Sum", _per_entity("Max", value, entity) + [_tok("character", "*")] + _per_entity("Max", weight, entity))
    den = _outer("Sum", _per_entity("Max", weight, entity) + [_tok("character", "*")] + _per_entity("Max", value, entity)
                 + [_tok("character", "/")] + _per_entity("Max", value, entity))
    return num, den


def level_metric_body(name: str, tokens: list[dict], *, description: str = "", fmt_values: list | None = None) -> dict:
    info = {"name": name, "subType": "metric"}
    if description:
        info["description"] = description
    body: dict = {"information": info, "expression": {"tokens": tokens},
                  "dimty": {"dimtyUnits": [dict(_RBL_UNIT)], "excludeAttribute": False, "allowAddingUnit": True}}
    if fmt_values:
        body["format"] = {"header": [], "values": list(fmt_values)}
    return body


def _source_format(m: MSTR, model_id: str, ref: dict) -> list | None:
    if ref.get("subType") != "fact_metric":
        return None
    r = m.get(f"/api/model/dataModels/{model_id}/factMetrics/{ref['objectId']}")
    return ((_json_or(r, {}) or {}).get("format") or {}).get("values") if r.ok else None


def _require_new_names(derived: list, names: list[str]) -> None:
    taken = [n for n in names if any((i.get("name") or "").lower() == n.lower() for i in _named(derived, n))]
    if taken:
        die(f"derived metric(s) already exist: {taken}. Delete them first (delete-derived-metric) or pick other names.")


def _create_metric(m: MSTR, model_id: str, body: dict, label: str) -> str:
    """POST one derived metric in its own changeset; returns its objectId."""
    mp = f"/api/model/dataModels/{model_id}"
    cs = open_cs(m)
    try:
        r = m.post(f"{mp}/metrics", params={"showAdvancedProperties": "true"}, json=body)
        if not r.ok: die(f"create {label}: {format_mstr_error(r)}")
        metric_id = (_json_or(r, {}).get("information") or {}).get("objectId")
        if not metric_id: die(f"create {label}: no objectId in response {r.text[:200]}")
        commit_cs(m, cs)
    except BaseException:
        discard_cs(m, cs)
        raise
    return metric_id


def cmd_create_level_metric(m: MSTR, args):
    """Create a once-per-entity metric: `Outer(Inner([source]) {~+, [entity]+}) {~+}` — by default
    Sum(Max(...)): the value is taken once per entity (customer, account, loan...) and added across
    entities. Use it for amounts repeated on every row of a periodic-snapshot fact (an original amount, a
    limit, a characteristic), which a plain sum would multiply by the number of periods. Verified shape:
    memory/reference_mosaic_derived_metrics.md §0d rule 1. One changeset.
    """
    description = _check_description(getattr(args, "description", "") or "")
    m.login(identity=True)
    facts, derived = _list_model_metrics(m, args.model_id)
    source = _resolve_metric_operand(_metric_operand_index(facts, derived), args.source_metric)
    entity = resolve_model_object_ref(m, args.model_id, "attributes", "attribute", args.entity_attribute)
    _require_new_names(derived, [args.name])
    fmt = _resolve_metric_format(args.format) if getattr(args, "format", None) else \
        _source_format(m, args.model_id, source)
    body = level_metric_body(args.name, level_metric_tokens(source, entity, args.inner, args.outer),
                             description=description, fmt_values=fmt)
    if getattr(args, "dry_run", False):
        print(json.dumps({"ok": True, "dry_run": True, "plan": [
            {"method": "POST", "path": f"/api/model/dataModels/{args.model_id}/metrics", "body": body}]}, indent=2))
        return
    metric_id = _create_metric(m, args.model_id, body, args.name)
    print(json.dumps({"ok": True, "metric_id": metric_id}, indent=2))


def cmd_create_weighted_average(m: MSTR, args):
    """Create a weighted average taken once per entity, as three derived metrics: a numerator helper
    (value x weight per entity, summed), a denominator helper (weight of entities that have a value,
    summed) and the average (numerator / denominator, outer join). The value can be an attribute (a
    score or ratio stored per entity) or a metric; the weight is a metric (an amount). Verified shape:
    memory/reference_mosaic_derived_metrics.md §0d rule 2. Each metric goes in its own changeset; if a
    later step fails, the helpers already created are listed for cleanup with delete-derived-metric.
    """
    description = _check_description(getattr(args, "description", "") or "")
    if bool(getattr(args, "value_attribute", None)) == bool(getattr(args, "value_metric", None)):
        die("pass exactly one of --value-attribute or --value-metric")
    m.login(identity=True)
    facts, derived = _list_model_metrics(m, args.model_id)
    index = _metric_operand_index(facts, derived)
    if args.value_attribute:
        value = resolve_model_object_ref(m, args.model_id, "attributes", "attribute", args.value_attribute)
    else:
        value = _resolve_metric_operand(index, args.value_metric)
    weight = _resolve_metric_operand(index, args.weight_metric)
    entity = resolve_model_object_ref(m, args.model_id, "attributes", "attribute", args.entity_attribute)
    num_name = args.numerator_name or f"{args.name} numerator"
    den_name = args.denominator_name or f"{args.name} denominator"
    _require_new_names(derived, [args.name, num_name, den_name])
    num_tokens, den_tokens = weighted_average_tokens(value, weight, entity)
    helper = f"Helper for {args.name}"
    num_body = level_metric_body(num_name, num_tokens, fmt_values=_resolve_metric_format("fixed2"),
                                 description=_check_description(
                                     f"{helper}: {value['name']} x {weight['name']} per {entity['name']}, summed."))
    den_body = level_metric_body(den_name, den_tokens, fmt_values=_source_format(m, args.model_id, weight),
                                 description=_check_description(
                                     f"{helper}: {weight['name']} of each {entity['name']} that has a "
                                     f"{value['name']}, summed."))
    fmt = _resolve_metric_format(args.format) if getattr(args, "format", None) else None

    def avg_body(num_id: str, den_id: str) -> dict:
        tree = {"type": "operator", "function": "divide", "children": [
            {"type": "object_reference", "target": {"objectId": num_id, "subType": "metric", "name": num_name}},
            {"type": "object_reference", "target": {"objectId": den_id, "subType": "metric", "name": den_name}}]}
        return compound_metric_body(args.name, tree, description=description, join="outer", fmt_values=fmt)

    if getattr(args, "dry_run", False):
        mp = f"/api/model/dataModels/{args.model_id}/metrics"
        print(json.dumps({"ok": True, "dry_run": True, "plan": [
            {"method": "POST", "path": mp, "body": num_body}, {"method": "POST", "path": mp, "body": den_body},
            {"method": "POST", "path": mp, "body": avg_body("<numerator id>", "<denominator id>")}]}, indent=2))
        return
    created: list[dict] = []
    try:
        num_id = _create_metric(m, args.model_id, num_body, num_name)
        created.append({"name": num_name, "objectId": num_id})
        den_id = _create_metric(m, args.model_id, den_body, den_name)
        created.append({"name": den_name, "objectId": den_id})
        avg_id = _create_metric(m, args.model_id, avg_body(num_id, den_id), args.name)
    except SystemExit:
        if created:
            print(f"[cleanup] created before the failure: {created} — remove with delete-derived-metric "
                  f"--object-id <id> --yes, or rerun after fixing", file=sys.stderr)
        raise
    print(json.dumps({"ok": True, "metric_id": avg_id, "numerator_id": num_id, "denominator_id": den_id}, indent=2))


# ── set-column-types: change column types in the model (no reload) ────────────────────────
# Ported from a run that worked live on a single-table CSV model: the table PATCH carries each type in both
# physicalTable.columns and the pipeline; the service then carried the type to the attribute forms and fact
# metrics itself (the alignment pass found nothing left to write). The alignment pass stays as a safety net.

COLUMN_TYPES: dict[str, dict] = {
    "text":      {"type": "utf8_char",  "precision": 0, "scale": 0},
    "integer":   {"type": "integer",    "precision": 4, "scale": 0},
    "bigint":    {"type": "int64",      "precision": 8, "scale": 0},
    "double":    {"type": "double",     "precision": 0, "scale": 0},
    "date":      {"type": "date",       "precision": 0, "scale": 0},
    "timestamp": {"type": "time_stamp", "precision": 0, "scale": 0},
}
_FORM_DISPLAY = {"text": "text", "date": "date", "timestamp": "date",
                 "integer": "number", "bigint": "number", "double": "number"}
_SIMPLE_COLUMN = _re.compile(r"^\s*[\[{](.+?)[\]}]\s*$")


def _simple_column(text: str | None) -> str | None:
    """'[COL]' / '{COL}' / 'COL' -> 'COL'; a formula with operators or functions -> None."""
    if not text:
        return None
    hit = _SIMPLE_COLUMN.match(text)
    if hit:
        return hit.group(1)
    t = text.strip()
    return t if _re.fullmatch(r"[A-Za-z0-9_ ]+", t) else None


def column_type_targets(sets: list[list[str]] | None, spec: str | None) -> dict[str, tuple[str, str, str]]:
    """--set COLUMN TYPE [DATE_FORMAT] (repeatable) and/or --spec (CSV: column_name, target_type, date_format,
    enabled; or JSON {"columns": [{"column", "type", "date_format"}]}) -> {column.casefold(): (column, type, fmt)}."""
    rows: list[tuple[str, str, str]] = []
    for v in sets or []:
        if len(v) not in (2, 3):
            die(f"--set takes COLUMN TYPE [DATE_FORMAT], got {v}")
        rows.append((v[0], v[1], v[2] if len(v) == 3 else ""))
    if spec:
        if spec.lower().endswith(".csv"):
            with open(spec, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    r = {(k or "").strip().lower(): (x or "").strip() for k, x in r.items()}
                    if r.get("enabled", "y").lower() in ("n", "no", "0", "false"):
                        continue
                    rows.append((r.get("column_name", ""), r.get("target_type", ""), r.get("date_format", "")))
        else:
            for c in (_load_json_arg(None, spec) or {}).get("columns") or []:
                rows.append((str(c.get("column") or ""), str(c.get("type") or ""), str(c.get("date_format") or "")))
    if not rows:
        die("give at least one --set COLUMN TYPE [DATE_FORMAT] or a --spec file")
    out: dict[str, tuple[str, str, str]] = {}
    for col, typ, fmt in rows:
        t = typ.lower()
        if not col:
            die("a column name is empty")
        if t not in COLUMN_TYPES:
            die(f"{col}: unknown type '{typ}' (use {', '.join(COLUMN_TYPES)})")
        if t in ("date", "timestamp") and not fmt:
            die(f"{col}: {t} needs a date format of the source values, e.g. yyyyMM")
        if t not in ("date", "timestamp") and fmt:
            die(f"{col}: a date format only applies to date or timestamp")
        out[col.casefold()] = (col, t, fmt)
    return out


def _table_key(name: str) -> str:
    """Compare table names loosely: case, spacing and a file extension don't matter."""
    return _re.sub(r"[^a-z0-9]", "", _re.sub(r"\.(csv|txt|tsv|xlsx?|parquet)$", "", name.strip().lower()))


def _pick_table(tables: list[dict], name: str | None) -> dict:
    if name:
        hit = [t for t in tables if (t.get("information") or {}).get("name", "").casefold() == name.casefold()] or \
              [t for t in tables if _table_key((t.get("information") or {}).get("name", "")) == _table_key(name)]
        if not hit:
            die(f"table '{name}' is not in the model: {[(t.get('information') or {}).get('name') for t in tables]}")
        return hit[0]
    if not tables:
        die("the model has no tables")
    return max(tables, key=lambda t: len((t.get("physicalTable") or {}).get("columns") or []))


def resolve_column_targets(model: dict, cols: dict[str, tuple[str, str]],
                           targets: dict[str, tuple[str, str, str]]) -> tuple[dict[str, tuple[str, str, str]], list[str]]:
    """Let --set name an attribute or a fact metric as well as a column: the connector shows attribute names,
    not the columns behind them. An attribute resolves through its ID form, a fact metric through its fact;
    either must read one column directly. -> (targets keyed by column, notes on what was resolved)."""
    via: dict[str, list[str]] = {}
    for a in model.get("attributes") or []:
        name = (a.get("information") or {}).get("name") or ""
        for f in a.get("forms") or []:
            if str(f.get("category") or f.get("name") or "").upper() != "ID":
                continue
            exprs = f.get("expressions") or []
            col = _simple_column((exprs[0].get("expression") or {}).get("text")) if len(exprs) == 1 else None
            if col and col.casefold() in cols:
                via.setdefault(name.casefold(), []).append(cols[col.casefold()][1])
    for fm in model.get("factMetrics") or []:
        name = (fm.get("information") or {}).get("name") or ""
        exprs = (fm.get("fact") or {}).get("expressions") or []
        col = _simple_column((exprs[0].get("expression") or {}).get("text")) if len(exprs) == 1 else None
        if col and col.casefold() in cols:
            via.setdefault(name.casefold(), []).append(cols[col.casefold()][1])
    out, notes, unknown = {}, [], []
    for key, (name, t, fmt) in targets.items():
        if key in cols:
            col = cols[key][1]
        else:
            # a connector form name ("postal code__id", "bucket (id)"): use the attribute behind it
            base = next((c for c in attribute_name_candidates(key) if c in via), key)
            hits = sorted(set(via.get(base) or []))
            if len(hits) != 1:
                unknown.append(name if not hits else f"{name} (reads {hits})")
                continue
            col = hits[0]
            notes.append(f"'{name}' reads column {col}")
        if col.casefold() in out and out[col.casefold()][1:] != (t, fmt):
            die(f"column {col} is given two types: {out[col.casefold()][1]} and {t}")
        out[col.casefold()] = (col, t, fmt)
    if unknown:
        die(f"not a column, attribute or fact metric of this table: {unknown}. Columns: {sorted(c[1] for c in cols.values())}")
    return out, notes


def joined_column_targets(model: dict, table_id: str,
                          targets: dict[str, tuple[str, str, str]]) -> dict[str, dict[str, tuple[str, str, str]]]:
    """Columns of OTHER tables that the same attribute form reads (a lookup or mapping table keyed on the
    retyped column). They must take the same type or the join breaks. -> {table_id: {column key: target}}."""
    out: dict[str, dict[str, tuple[str, str, str]]] = {}
    for a in model.get("attributes") or []:
        for f in a.get("forms") or []:
            reads = []
            for e in f.get("expressions") or []:
                col = _simple_column((e.get("expression") or {}).get("text"))
                for t in e.get("tables") or []:
                    if col and t.get("objectId"):
                        reads.append((t["objectId"], col))
            hit = next((targets[c.casefold()] for tid, c in reads if tid == table_id and c.casefold() in targets), None)
            if not hit:
                continue
            for tid, col in reads:
                if tid != table_id:
                    out.setdefault(tid, {})[col.casefold()] = (col, hit[1], hit[2])
    return out


def column_type_table_patch(table: dict, targets: dict[str, tuple[str, str, str]]):
    """-> (physicalTable body for PATCH /tables/{id}, [(column, old type, new type, fmt)], [unchanged columns]).
    Sets each type in physicalTable.columns AND the pipeline; both must agree."""
    keep = lambda i: {k: i[k] for k in ("objectId", "subType", "name") if k in (i or {})}  # noqa: E731
    pt = copy.deepcopy(table["physicalTable"])
    pt["information"] = keep(pt.get("information"))
    pipeline = json.loads(pt["pipeline"])
    pipe_cols = {c["name"].casefold(): c for ch in (pipeline.get("rootTable") or {}).get("children") or []
                 for c in ch.get("columns") or []}
    present = {c["columnName"].casefold() for c in pt["columns"]}
    missing = [v[0] for k, v in targets.items() if k not in present]
    if missing:
        die(f"column(s) not in table '{keep(table.get('information')).get('name')}': {missing}")
    changed, same = [], []
    for c in pt["columns"]:
        c["information"] = keep(c.get("information"))
        key = c["columnName"].casefold()
        if key not in targets:
            continue
        _, t, fmt = targets[key]
        new = COLUMN_TYPES[t]
        if c.get("dataType") == new and c.get("format", "") == fmt:
            same.append(c["columnName"])
            continue
        pc = pipe_cols.get(key)
        if pc is None:
            die(f"{c['columnName']} is not in the table's pipeline")
        changed.append((c["columnName"], (c.get("dataType") or {}).get("type"), new["type"], fmt))
        c["dataType"] = dict(new)
        if fmt:
            c["format"] = fmt
        else:
            c.pop("format", None)
        pc["dataType"] = dict(new)
        pc["format"] = fmt
    pt["pipeline"] = json.dumps(pipeline, separators=(",", ":"))
    return pt, changed, same


def _colref_token(col_id: str, name: str) -> dict:
    return {"type": "column_reference", "value": f"[{name}]",
            "target": {"objectId": col_id, "subType": "column", "name": name}}


def _tokenized_exprs(exprs: list[dict], cols: dict[str, tuple[str, str]]) -> list[dict] | None:
    """Rewrite simple column expressions as tokens (the only shape the service accepts on write); None when an
    expression is a formula this command does not rewrite."""
    out = []
    for e in exprs:
        col = _simple_column((e.get("expression") or {}).get("text"))
        if col is None or col.casefold() not in cols:
            return None
        cid, name = cols[col.casefold()]
        out.append({"expression": {"tokens": [_colref_token(cid, name), {"type": "end_of_text", "value": ""}]},
                    "tables": e.get("tables")})
    return out


def column_type_object_writes(model: dict, mid: str, cols: dict[str, tuple[str, str]],
                              targets: dict[str, tuple[str, str, str]], get) -> tuple[list[tuple], list[str]]:
    """Attribute forms and fact metrics that read a target column get the same type. -> (writes, notes);
    each write is (label, path, body). `get(path)` returns the current object."""
    writes, notes = [], []
    for a in model.get("attributes") or []:
        refs = [_simple_column((e.get("expression") or {}).get("text"))
                for f in a.get("forms") or [] for e in f.get("expressions") or []]
        if not any(r and r.casefold() in targets for r in refs):
            continue
        aid, aname = a["information"]["objectId"], a["information"]["name"]
        cur = get(f"/api/model/dataModels/{mid}/attributes/{aid}")
        forms, need = [], False
        for f in cur.get("forms") or []:
            f = {k: v for k, v in f.items() if k != "translations"}
            if f.get("expressions"):
                ex = _tokenized_exprs(f["expressions"], cols)
                if ex is None:
                    notes.append(f"attribute '{aname}': form '{f.get('name')}' has a formula; check its type by hand")
                    forms = None
                    break
                col = _simple_column(f["expressions"][0]["expression"].get("text"))
                if col and col.casefold() in targets:
                    t = targets[col.casefold()][1]
                    role = "date" if t in ("date", "timestamp") else f.get("semanticRole")
                    if f.get("dataType") != COLUMN_TYPES[t] or f.get("displayFormat") != _FORM_DISPLAY[t] \
                            or f.get("semanticRole") != role:
                        need = True
                    f["dataType"], f["displayFormat"] = dict(COLUMN_TYPES[t]), _FORM_DISPLAY[t]
                    if role:   # Studio gives a date form the "date" role (an integer yyyyMM had "month")
                        f["semanticRole"] = role
                f["expressions"] = ex
            forms.append(f)
        if forms is not None and need:
            writes.append((f"attribute '{aname}'", f"/api/model/dataModels/{mid}/attributes/{aid}", {"forms": forms}))
    for fm in model.get("factMetrics") or []:
        exprs = (fm.get("fact") or {}).get("expressions") or []
        col = _simple_column((exprs[0].get("expression") or {}).get("text")) if exprs else None
        if not col or col.casefold() not in targets:
            continue
        t = targets[col.casefold()][1]
        fid, fname = fm["information"]["objectId"], fm["information"]["name"]
        cur = get(f"/api/model/dataModels/{mid}/factMetrics/{fid}")
        if (cur.get("fact") or {}).get("dataType") == COLUMN_TYPES[t]:
            continue
        ex = _tokenized_exprs(cur["fact"]["expressions"], cols)
        if ex is None:
            notes.append(f"fact metric '{fname}' has a formula; check its type by hand")
            continue
        fact = {"dataType": dict(COLUMN_TYPES[t]), "expressions": ex,
                "extensions": cur["fact"].get("extensions", []), "entryLevel": cur["fact"].get("entryLevel", [])}
        writes.append((f"fact metric '{fname}'", f"/api/model/dataModels/{mid}/factMetrics/{fid}", {"fact": fact}))
    return writes, notes


def _patch_in_changeset(m: MSTR, writes: list[tuple], label: str) -> None:
    cs = open_cs(m)
    try:
        for what, path, body in writes:
            r = m.patch(path, json=body)
            if not r.ok:
                die(f"{label}: PATCH {what}: {format_mstr_error(r)}")
        commit_cs(m, cs)
    except BaseException:   # die() raises SystemExit; discard on every failure path
        discard_cs(m, cs)
        raise


def cmd_set_column_types(m: MSTR, args):
    """Change column types (and the source date format) inside the model, without reloading the source:
    1) one changeset: the table, each type in both the column list and the pipeline;
    2) one changeset: attribute forms and fact metrics that read a changed column get the same type
       (usually already done by the service; this pass writes only what still differs).
    Republish afterwards for the data to change."""
    targets = column_type_targets(args.set, args.spec)
    m.login(identity=True)
    mid = args.model_id
    r = m.post(f"/api/model/dataModels/{mid}/export")
    if not r.ok:
        die(f"export model: {format_mstr_error(r)}")
    model = (r.json().get("model") or [{}])[0]
    table = _pick_table(model.get("tables") or [], args.table)
    tinfo = table["information"]
    cols = {c["columnName"].casefold(): (c["information"]["objectId"], c["columnName"])
            for c in table["physicalTable"]["columns"]}

    def get(path: str) -> dict:
        g = m.get(path)
        if not g.ok:
            die(f"GET {path}: {format_mstr_error(g)}")
        return g.json() or {}

    targets, resolved = resolve_column_targets(model, cols, targets)
    for n in resolved:
        print(f"  = {n}", file=sys.stderr)
    cur_table = get(f"/api/model/dataModels/{mid}/tables/{tinfo['objectId']}")
    pt, changed, same = column_type_table_patch(cur_table, targets)
    print(f"→ table '{tinfo['name']}': {len(changed)} column type(s) to change, {len(same)} already set",
          file=sys.stderr)
    for col, old, new, fmt in changed:
        print(f"  ~ {col}: {old} -> {new}{' (' + fmt + ')' if fmt else ''}", file=sys.stderr)
    if changed and not args.dry_run:
        _patch_in_changeset(m, [(f"table '{tinfo['name']}'", f"/api/model/dataModels/{mid}/tables/{tinfo['objectId']}",
                                 {"physicalTable": pt})], "table")
    # A lookup or mapping table keyed on a retyped column takes the same type, or its join breaks.
    joined = [] if args.no_joined else [(t, tgt) for t, tgt in joined_column_targets(model, tinfo["objectId"],
                                                                                     targets).items()]
    joined_changed = []
    for tid, tgt in joined:
        other = get(f"/api/model/dataModels/{mid}/tables/{tid}")
        oname = (other.get("information") or {}).get("name", tid)
        opt, ochanged, _ = column_type_table_patch(other, tgt)
        for col, old, new, fmt in ochanged:
            print(f"  ~ {oname}.{col}: {old} -> {new} (joined)", file=sys.stderr)
            joined_changed.append({"table": oname, "column": col, "from": old, "to": new})
        if ochanged and not args.dry_run:
            _patch_in_changeset(m, [(f"table '{oname}'", f"/api/model/dataModels/{mid}/tables/{tid}",
                                     {"physicalTable": opt})], f"joined table {oname}")
    writes, notes = column_type_object_writes(model, mid, cols, targets, get)
    for n in notes:
        print(f"  ! {n}", file=sys.stderr)
    if writes and not args.dry_run:
        _patch_in_changeset(m, writes, "align attributes and fact metrics")
    print(json.dumps({"ok": True, "dry_run": bool(args.dry_run), "table": tinfo["name"],
                      "changed": [{"column": c, "from": o, "to": n, "date_format": f or None}
                                  for c, o, n, f in changed],
                      "already_set": same, "resolved": resolved, "joined": joined_changed,
                      "aligned": [w[0] for w in writes],
                      "check_by_hand": notes,
                      "next": "republish the model for the data to change" if (changed or writes or joined_changed)
                      else None},
                     indent=2))


# ── create-formula-attribute: an attribute computed from the table's columns ─────────────────
# Ported from runs that worked live (AddMonths([PERIOD], -1); Year([date]) typed integer; MonthsBetween).
# Prefer a lookup table: it is faster at run time, and it can be replaced without deleting the derived
# metrics built on it. Create a formula attribute only when the user asks for one.

_ID_FORM = "45C11FA478E745FEA08D781CEA190FE5"     # the system ID form
_FORMULA_TOKEN = _re.compile(r"""\s*(?:
    (?P<col>\[[^\]]+\])            |
    (?P<str>'(?:[^']|'')*')        |
    (?P<num>\d+(?:\.\d+)?)         |
    (?P<op><=|>=|<>|[(),+\-*/=<>]) |
    (?P<id>[A-Za-z_][A-Za-z0-9_]*)
)""", _re.X)


def formula_attribute_tokens(formula: str, cols: dict[str, tuple[str, str]]) -> tuple[list[dict], list[str]]:
    """Strategy formula over [COLUMNS] -> (tokens, unverified token kinds used). Verified live: functions,
    [columns], integers, operators. String literals and decimals have not been confirmed on a tenant."""
    toks, unverified, pos, text = [], [], 0, formula.strip()
    while pos < len(text):
        hit = _FORMULA_TOKEN.match(text, pos)
        if not hit or hit.end() == pos:
            die(f"cannot read the formula at: {text[pos:pos + 20]!r}")
        pos = hit.end()
        if hit.group("col"):
            name = hit.group("col")[1:-1]
            if name.casefold() not in cols:
                die(f"unknown column [{name}]; columns: {sorted(c[1] for c in cols.values())}")
            cid, cname = cols[name.casefold()]
            toks.append(_colref_token(cid, cname))
        elif hit.group("str"):
            toks.append({"type": "string", "value": hit.group("str")})
            unverified.append("string literal")
        elif hit.group("num"):
            v = hit.group("num")
            toks.append({"type": "real" if "." in v else "integer", "value": v})
            if "." in v:
                unverified.append("decimal")
        elif hit.group("op"):
            toks.append({"type": "character", "value": hit.group("op")})
        else:
            word = hit.group("id")
            toks.append({"type": "function" if text[pos:].lstrip()[:1] == "(" else "identifier", "value": word})
    return toks + [{"type": "end_of_text", "value": ""}], sorted(set(unverified))


def formula_attribute_body(name: str, tokens: list[dict], table_ref: dict, *, description: str = "",
                           display_format: str = "text", data_type: str | None = None) -> dict:
    form = {"id": _ID_FORM, "category": "ID", "type": "system", "displayFormat": display_format, "name": "ID",
            "expressions": [{"expression": {"tokens": tokens}, "tables": [table_ref]}], "lookupTable": table_ref}
    if data_type:
        form["dataType"] = dict(COLUMN_TYPES[data_type])
    shown = [{"id": _ID_FORM, "name": "ID"}]
    return {"information": {"name": name, "description": description}, "forms": [form],
            "keyForm": {"id": _ID_FORM}, "attributeLookupTable": table_ref,
            "displays": {"reportDisplays": shown, "browseDisplays": list(shown)}}


def cmd_create_formula_attribute(m: MSTR, args):
    """Create (or, with --update, rewrite) an attribute whose ID form is a formula over the table's columns.
    One changeset. Republish afterwards for it to hold data. Prefer a lookup table where one can do the job."""
    if args.data_type and args.data_type not in COLUMN_TYPES:
        die(f"--data-type must be one of {', '.join(COLUMN_TYPES)}")
    m.login(identity=True)
    mid = args.model_id
    r = m.post(f"/api/model/dataModels/{mid}/export")
    if not r.ok:
        die(f"export model: {format_mstr_error(r)}")
    model = (r.json().get("model") or [{}])[0]
    table = _pick_table(model.get("tables") or [], args.table)
    tinfo = table["information"]
    table_ref = {"objectId": tinfo["objectId"], "subType": "logical_table", "name": tinfo["name"]}
    cols = {c["columnName"].casefold(): (c["information"]["objectId"], c["columnName"])
            for c in table["physicalTable"]["columns"]}
    if getattr(args, "column", None):   # add-column-attribute: one column as-is, named as column, attribute or metric
        targets, notes = resolve_column_targets(model, cols, {args.column.casefold(): (args.column, "", "")})
        ((col, _t, _f),) = targets.values()
        for n in notes:
            print(f"  = {n}", file=sys.stderr)
        args.formula = f"[{col}]"
        taken = {(o.get("information") or {}).get("name", "").casefold(): kind
                 for kind in ("factMetrics", "metrics") for o in model.get(kind) or []}
        if args.name.casefold() in taken:
            die(f"'{args.name}' is already a metric's name; give the attribute another name, or the connector "
                f"shows two columns with one name")
    tokens, unverified = formula_attribute_tokens(args.formula, cols)
    if unverified:
        _require_unverified_ok(args, "create-formula-attribute",
                               f"the formula uses {', '.join(unverified)}, which no live run has confirmed.")
    existing = {(a.get("information") or {}).get("name", "").casefold(): a for a in model.get("attributes") or []}
    current = existing.get(args.name.casefold())
    if args.update:
        if not current:
            die(f"no attribute named '{args.name}' to update")
        aid = current["information"]["objectId"]
        g = m.get(f"/api/model/dataModels/{mid}/attributes/{aid}")
        if not g.ok:
            die(f"GET attribute: {format_mstr_error(g)}")
        forms = (g.json() or {}).get("forms") or []
        if len(forms) != 1:
            die(f"'{args.name}' has {len(forms)} forms; --update handles single-form attributes only")
        f = {k: v for k, v in forms[0].items() if k != "translations"}
        f["expressions"] = [{"expression": {"tokens": tokens}, "tables": [table_ref]}]
        if args.data_type:
            f["dataType"] = dict(COLUMN_TYPES[args.data_type])
        f["displayFormat"] = args.display_format or f.get("displayFormat") or "text"
        method, path, body = "PATCH", f"/api/model/dataModels/{mid}/attributes/{aid}", {"forms": [f]}
    else:
        if current:
            die(f"an attribute named '{args.name}' exists; pass --update to rewrite its formula")
        method, path = "POST", f"/api/model/dataModels/{mid}/attributes"
        body = formula_attribute_body(args.name, tokens, table_ref, description=args.description or "",
                                      display_format=args.display_format or "text", data_type=args.data_type)
    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "method": method, "path": path, "body": body}, indent=2))
        return
    cs = open_cs(m)
    try:
        w = m.patch(path, json=body) if method == "PATCH" else m.post(path, json=body)
        if not w.ok:
            die(f"{method} attribute '{args.name}': {format_mstr_error(w)}")
        commit_cs(m, cs)
    except BaseException:
        discard_cs(m, cs)
        raise
    print(json.dumps({"ok": True, "attribute": args.name, "updated": bool(args.update),
                      "next": "republish the model for the attribute to hold data"}, indent=2))


# ── rename-describe: names and descriptions of attributes and fact metrics ──────────────────
# Ported from runs that worked live: PATCH {"information": {"name", "description"}} on the attribute or fact
# metric, all in one changeset; takes effect at once, no republish. Derived metrics need a full PUT (not here).

DESCRIPTION_LIMIT = 250   # characters, Strategy's limit for a description


def rename_describe_items(spec: dict | list) -> list[dict]:
    """{"items": [{object_type, current_name, new_name?, description?}]} (or the list itself) -> checked items."""
    items = spec.get("items") if isinstance(spec, dict) else spec
    out = []
    for it in items or []:
        kind = str(it.get("object_type") or "").lower()
        if kind not in ("attribute", "fact_metric"):
            die(f"{it.get('current_name')}: object_type must be attribute or fact_metric (derived metrics take "
                f"their description when created)")
        if not it.get("current_name"):
            die("an item has no current_name")
        desc = it.get("description")
        if desc is not None and len(str(desc)) > DESCRIPTION_LIMIT:
            die(f"{it['current_name']}: description is {len(str(desc))} characters (max {DESCRIPTION_LIMIT})")
        if not (it.get("new_name") or desc is not None):
            die(f"{it['current_name']}: give new_name, description or both")
        out.append({"object_type": kind, "current_name": str(it["current_name"]),
                    "new_name": it.get("new_name") or None, "description": desc})
    if not out:
        die("no items")
    return out


def rename_describe_writes(model: dict, mid: str, items: list[dict]) -> tuple[list[tuple], list[str]]:
    """-> (writes [(label, path, body)], notes). Already-applied items are skipped, so a rerun is harmless.

    A connector form name ('<attribute>__<form>', '<attribute> (<form>)') describes its attribute: a form has
    no description of its own, and the connector shows the attribute's on every form. A form name can't be
    renamed here. Every item is checked before anything is written, and all problems are reported together."""
    pools = {"attribute": ("attributes", model.get("attributes") or []),
             "fact_metric": ("factMetrics", model.get("factMetrics") or [])}
    by_name = {k: {(o.get("information") or {}).get("name", "").casefold(): o for o in objs}
               for k, (_, objs) in pools.items()}
    writes, notes, problems = [], [], []
    wanted: dict[str, dict] = {}             # objectId -> merged request for that object
    renamed: dict[str, dict] = {k: {} for k in pools}
    for it in items:
        kind, cur = it["object_type"], it["current_name"]
        names = by_name[kind]
        o, via_form = names.get(cur.casefold()) or renamed[kind].get(cur.casefold()), False
        if o is None and kind == "attribute":
            for alt in attribute_name_candidates(cur)[1:]:
                if alt in names:
                    o, via_form = names[alt], True
                    break
        if o is None:
            if it["new_name"] and it["new_name"].casefold() in names:
                notes.append(f"{cur}: already renamed to '{it['new_name']}'")
                continue
            other = "fact_metric" if kind == "attribute" else "attribute"
            hint = (f" (it is a {other}: set object_type to {other})"
                    if any(c in by_name[other] for c in attribute_name_candidates(cur)) else "")
            problems.append(f"{kind} '{cur}' not found{hint}")
            continue
        info = o["information"]
        if via_form:
            if it["new_name"]:
                problems.append(f"'{cur}' is a form of attribute '{info['name']}': forms can't be renamed here; "
                                f"rename the attribute, or leave new_name out")
                continue
            notes.append(f"'{cur}' is a form: its description is set on attribute '{info['name']}', which the "
                         f"connector shows on every form")
        if it["new_name"]:
            renamed[kind][it["new_name"].casefold()] = o     # later items may use the new name
        w = wanted.setdefault(info["objectId"], {"o": o, "kind": kind, "items": [], "name": None, "desc": None})
        for key, val in (("name", it["new_name"]), ("desc", it["description"])):
            if val is None:
                continue
            if w[key] is not None and str(w[key]) != str(val):
                problems.append(f"{kind} '{info['name']}' gets two different {'names' if key == 'name' else 'descriptions'} "
                                f"(items '{w['items'][-1]}' and '{cur}'); give it one (a form shares its "
                                f"attribute's description)")
            w[key] = val
        w["items"].append(cur)
    for w in wanted.values():
        kind, o = w["kind"], w["o"]
        info, names = o["information"], by_name[kind]
        name = w["name"] or info["name"]
        desc = info.get("description", "") if w["desc"] is None else str(w["desc"])
        if name.casefold() != info["name"].casefold() and name.casefold() in names \
                and names[name.casefold()] is not o:
            problems.append(f"{info['name']}: another {kind} is already named '{name}'")
            continue
        if name == info["name"] and desc == info.get("description", ""):
            notes.append(f"{info['name']}: unchanged")
            continue
        names[name.casefold()] = o
        what = " + ".join(x for x in ((f"rename -> '{name}'" if name != info["name"] else ""),
                                       ("description" if desc != info.get("description", "") else "")) if x)
        writes.append((f"{kind} '{info['name']}': {what}",
                       f"/api/model/dataModels/{mid}/{pools[kind][0]}/{info['objectId']}",
                       {"information": {"name": name, "description": desc}}))
    if problems:
        die(f"rename-describe: {len(problems)} of {len(items)} item(s) can't be applied; nothing was written. "
            f"Fix them and run again:\n  - " + "\n  - ".join(problems))
    return writes, notes


def cmd_rename_describe(m: MSTR, args):
    """Rename attributes and fact metrics and/or set their descriptions, by name, in one changeset."""
    items = rename_describe_items(_load_json_arg(None, args.spec))
    m.login(identity=True)
    r = m.post(f"/api/model/dataModels/{args.model_id}/export")
    if not r.ok:
        die(f"export model: {format_mstr_error(r)}")
    model = (r.json().get("model") or [{}])[0]
    writes, notes = rename_describe_writes(model, args.model_id, items)
    for n in notes:
        print(f"  = {n}", file=sys.stderr)
    for w in writes:
        print(f"  ~ {w[0]}", file=sys.stderr)
    if writes and not args.dry_run:
        _patch_in_changeset(m, writes, "rename-describe")
    print(json.dumps({"ok": True, "dry_run": bool(args.dry_run), "changed": [w[0] for w in writes],
                      "skipped": notes}, indent=2))


TIME_INTERVALS = ("year", "quarter", "month", "week", "day")   # seen live on a date attribute (smartAttributeType "<x>_interval")


def time_attribute_items(args) -> list[dict]:
    """--attribute A --intervals year,quarter,month and/or --spec {items: [{attribute, intervals}]}."""
    items = []
    if getattr(args, "spec", None):
        items += list((_load_json_arg(None, args.spec) or {}).get("items") or [])
    if getattr(args, "attribute", None):
        items.append({"attribute": args.attribute, "intervals": [x.strip() for x in (args.intervals or "").split(",")
                                                                   if x.strip()]})
    if not items:
        die("add-time-attributes: give --attribute and --intervals, or --spec")
    for it in items:
        bad = [x for x in it.get("intervals") or [] if str(x).lower() not in TIME_INTERVALS]
        if not it.get("attribute") or not it.get("intervals") or bad:
            die(f"add-time-attributes: each item needs an attribute and intervals from {', '.join(TIME_INTERVALS)}"
                + (f" (unknown: {bad})" if bad else ""))
    return items


def _smart_type(entry: dict) -> str:
    return str(entry.get("smartAttributeType") or (entry.get("definition") or {}).get("smartAttributeType") or "")


def time_attribute_body(attr: dict, intervals: list[str]) -> tuple[dict | None, list[str], list[str]]:
    """-> (PUT body or None when nothing to add, added types, problems). The PUT replaces the whole set, so the
    existing smart attributes go in again with their objectId."""
    name = (attr.get("information") or {}).get("name") or "?"
    key_id = (attr.get("keyForm") or {}).get("id")
    key = next((f for f in attr.get("forms") or [] if f.get("id") == key_id), (attr.get("forms") or [{}])[0])
    typ = ((key.get("dataType") or {}).get("type") or "").lower()
    if typ not in ("date", "time_stamp"):
        return None, [], [f"'{name}' is {typ or 'not typed'}: time attributes need a date or timestamp; convert it "
                          f"first (set-column-types ... date <pattern>, e.g. yyyyMM)"]
    existing = [e for e in attr.get("smartAttributes") or [] if _smart_type(e)]
    have = {_smart_type(e) for e in existing}
    wanted = [f"{str(x).lower()}_interval" for x in intervals]
    added = [w for w in wanted if w not in have]
    if not added:
        return None, [], []
    def entry(t, e=None):
        label = t.replace("_interval", "").capitalize()             # Studio names them '<attribute> (Year)'
        out = {"subType": "attribute_smart",
               "name": (e or {}).get("name") or ((e or {}).get("information") or {}).get("name") or f"{name} ({label})",
               "definition": {"smartAttributeType": t, "parameters": [], "ascending": (e or {}).get("ascending", True)}}
        if e and ((e.get("information") or {}).get("objectId") or e.get("objectId")):
            out["objectId"] = (e.get("information") or {}).get("objectId") or e.get("objectId")
        return out
    return {"smartAttributes": [entry(_smart_type(e), e) for e in existing] + [entry(t) for t in added]}, added, []


def cmd_add_time_attributes(m: MSTR, args):
    """Time attributes (year, quarter, month, week, day intervals) on date attributes, as Studio adds them: each
    shows on the connector as '<attribute> (<interval>)__id / __start / __desc'. One changeset; republish after."""
    items = time_attribute_items(args)
    m.login(identity=True)
    r = m.post(f"/api/model/dataModels/{args.model_id}/export")
    if not r.ok:
        die(f"export model: {format_mstr_error(r)}")
    model = (r.json().get("model") or [{}])[0]
    by_name = {}
    for a in model.get("attributes") or []:
        by_name[(a.get("information") or {}).get("name", "").casefold()] = a
    writes, notes, problems = [], [], []
    for it in items:
        a = next((by_name[c] for c in attribute_name_candidates(it["attribute"]) if c in by_name), None)
        if a is None:
            problems.append(f"attribute '{it['attribute']}' not found")
            continue
        body, added, probs = time_attribute_body(a, it["intervals"])
        problems += probs
        aname, aid = a["information"]["name"], a["information"]["objectId"]
        if body is None and not probs:
            notes.append(f"'{aname}' already has {', '.join(it['intervals'])}")
        if body is not None:
            writes.append((f"attribute '{aname}': + {', '.join(added)}",
                           f"/api/model/dataModels/{args.model_id}/attributes/{aid}/smartAttributes", body))
    if problems:
        die("add-time-attributes: nothing was written:\n  - " + "\n  - ".join(problems))
    for n in notes:
        print(f"  = {n}", file=sys.stderr)
    for w in writes:
        print(f"  ~ {w[0]}", file=sys.stderr)
        if args.dry_run:
            print(json.dumps(w[2], indent=2), file=sys.stderr)
    if writes and not args.dry_run:
        cs = open_cs(m)
        try:
            for what, path, body in writes:
                resp = m.put(path, json=body)
                if not resp.ok:
                    die(f"add-time-attributes: PUT {what}: {format_mstr_error(resp)}")
            commit_cs(m, cs)
        except BaseException:   # die() raises SystemExit; discard on every failure path
            discard_cs(m, cs)
            raise
    print(json.dumps({"ok": True, "dry_run": bool(args.dry_run), "changed": [w[0] for w in writes],
                      "skipped": notes, "next": "republish, then the intervals show on the connector as "
                                                "'<attribute> (<interval>)__id / __start / __desc'"}, indent=2))


def _display_ids(entries) -> list[str]:
    out = []
    for d in entries or []:
        i = d.get("id") if isinstance(d, dict) else d
        if i and str(i) not in out:
            out.append(str(i))
    return out


def show_forms_body(attr: dict, forms: list[str]) -> tuple[dict | None, list[str], list[str]]:
    """The displays body that adds forms (by name or category, such as ID or DESC) to an attribute's report and
    browse displays, keeping the ones it has. A form in neither list is hidden: the connector doesn't list it
    (verified: adding a form to both lists made the connector list it as '<attribute>__<form>').
    Returns (body or None when nothing changes, forms added, problems)."""
    by_key: dict[str, str] = {}
    for f in attr.get("forms") or []:
        fid = f.get("id") or (f.get("information") or {}).get("objectId")
        for k in (f.get("name"), (f.get("information") or {}).get("name"), f.get("category")):
            if k and fid:
                by_key.setdefault(str(k).casefold(), str(fid))
    disp = attr.get("displays") or {}
    report, browse = _display_ids(disp.get("reportDisplays")), _display_ids(disp.get("browseDisplays"))
    problems, ids, added = [], [], []
    for name in forms:
        fid = by_key.get(str(name).strip().casefold())
        if not fid:
            problems.append(f"form '{name}' not found (forms: {', '.join(sorted({k for k in by_key}))})")
            continue
        ids.append(fid)
        if fid not in report or fid not in browse:
            added.append(str(name))
    new_report = report + [i for i in ids if i not in report]
    new_browse = browse + [i for i in ids if i not in browse]
    if problems or (new_report == report and new_browse == browse):
        return None, added, problems
    return ({"displays": {"reportDisplays": [{"id": i} for i in new_report],
                          "browseDisplays": [{"id": i} for i in new_browse]}}, added, problems)


def cmd_show_forms(m: MSTR, args):
    """Make hidden forms visible: add them to the attribute's report and browse displays, keeping the ones it
    has. A form already in both is left alone, so running it twice changes nothing. One changeset; republish
    and re-certify after, then check the connector lists the form."""
    forms = [x.strip() for x in (args.forms or "").split(",") if x.strip()]
    if not forms:
        die("show-forms: give --forms (names or categories, comma-separated)")
    m.login(identity=True)
    r = m.post(f"/api/model/dataModels/{args.model_id}/export")
    if not r.ok:
        die(f"export model: {format_mstr_error(r)}")
    model = (r.json().get("model") or [{}])[0]
    by_name = {(a.get("information") or {}).get("name", "").casefold(): a for a in model.get("attributes") or []}
    a = next((by_name[c] for c in attribute_name_candidates(args.attribute) if c in by_name), None)
    if a is None:
        die(f"show-forms: attribute '{args.attribute}' not found")
    body, added, problems = show_forms_body(a, forms)
    if problems:
        die("show-forms: nothing was written:\n  - " + "\n  - ".join(problems))
    aname, aid = a["information"]["name"], a["information"]["objectId"]
    if body is None:
        print(json.dumps({"ok": True, "dry_run": bool(args.dry_run), "changed": [],
                          "skipped": [f"'{aname}': {', '.join(forms)} already shown"]}, indent=2))
        return
    print(f"  ~ attribute '{aname}': show {', '.join(added)}", file=sys.stderr)
    if args.dry_run:
        print(json.dumps(body, indent=2), file=sys.stderr)
    else:
        cs = open_cs(m)
        try:
            resp = m.patch(f"/api/model/dataModels/{args.model_id}/attributes/{aid}", json=body)
            if not resp.ok:
                die(f"show-forms: PATCH '{aname}': {format_mstr_error(resp)}")
            commit_cs(m, cs)
        except BaseException:
            discard_cs(m, cs)
            raise
    print(json.dumps({"ok": True, "dry_run": bool(args.dry_run), "changed": [f"'{aname}': {', '.join(added)}"],
                      "next": "republish and re-certify; the connector then lists the form as "
                              "'<attribute>__<form>'"}, indent=2))


def cmd_patch_fact_metrics(m: MSTR, args):
    """Set aggregation function and/or number format on many fact metrics in ONE
    changeset: list → per-metric GET (diff) → PATCH only what differs → commit →
    re-read verify. One login and one changeset keep it clear of the interactive
    session cap. Spec (JSON or YAML):

      presets: {"<name>": [<format.values tokens>]}   # optional; adds to METRIC_FORMAT_PRESETS
      metrics: [{"name": "<fact metric name or id>", "function": "avg", "format": "currency"}]

    Logs in WITHOUT the identity token (memory/feedback_mosaic_identity_token_
    privilege_downgrade.md). See memory/reference_mosaic_fact_metric_aggregation.md.
    """
    spec = (_load_yaml(args.spec) if args.spec.endswith((".yaml", ".yml"))
            else _load_json_arg(None, args.spec)) or {}
    presets = spec.get("presets") or {}
    items = spec.get("metrics") or []
    if not items: die(f"{args.spec}: no metrics[] entries")
    m.login()
    mp = f"/api/model/dataModels/{args.model_id}"
    listed, failed = _list_all(m, f"{mp}/factMetrics", "factMetrics")
    if failed is not None: die(f"list factMetrics: {format_mstr_error(failed)}")
    by_key = {}
    for o in listed:
        info = o.get("information") or {}
        by_key[(info.get("name") or "").lower()] = info
        by_key[(info.get("objectId") or "").lower()] = info

    plan, missing = [], []
    for it in items:
        info = by_key.get(str(it.get("name") or "").lower())
        if not info:
            missing.append(it.get("name")); continue
        r = m.get(f"{mp}/factMetrics/{info['objectId']}")
        if not r.ok: die(f"GET factMetric {info['name']}: {format_mstr_error(r)}")
        cur = r.json() or {}
        body = {}
        if it.get("function") and it["function"].lower() != (cur.get("function") or "").lower():
            body["function"] = it["function"].lower()
        if it.get("format") is not None:
            want = _resolve_metric_format(it["format"], presets)
            if not _format_matches(cur.get("format"), want):
                body["format"] = {"header": (cur.get("format") or {}).get("header") or [], "values": want}
        plan.append({"name": info["name"], "id": info["objectId"],
                     "function": f"{cur.get('function')}→{body['function']}" if "function" in body else cur.get("function"),
                     "format": "change" if "format" in body else "ok", "body": body})
    if missing: die(f"spec names not on model: {missing}")

    todo = [p for p in plan if p["body"]]
    print(f"→ {len(plan)} metric(s) in spec, {len(todo)} need a PATCH", file=sys.stderr)
    for p in plan:
        print(f"  {'→' if p['body'] else '·'} {p['name']}: function {p['function']}, format {p['format']}",
              file=sys.stderr)
    if args.dry_run or not todo:
        return

    cs = open_cs(m, release_self_locks=bool(getattr(args, "release_locks", False)))
    try:
        for p in todo:
            r = m.patch(f"{mp}/factMetrics/{p['id']}", json=p["body"])
            if not r.ok: die(f"PATCH {p['name']}: {format_mstr_error(r)}")
        commit_cs(m, cs)
    except BaseException:  # die() raises SystemExit; discard on every failure path
        discard_cs(m, cs)
        raise

    report = []
    for p in todo:
        cur = m.get(f"{mp}/factMetrics/{p['id']}").json() or {}
        ok = (("function" not in p["body"] or cur.get("function") == p["body"]["function"])
              and ("format" not in p["body"] or _format_matches(cur.get("format"), p["body"]["format"]["values"])))
        report.append({"name": p["name"], "ok": ok, "function": cur.get("function")})
    print(json.dumps({"ok": all(x["ok"] for x in report), "patched": len(todo), "verify": report}, indent=2))
    if not all(x["ok"] for x in report):
        die("read-back does not match the spec for at least one metric")


def cmd_attach_transformation(m: MSTR, args):
    """Apply a transformation to an existing metric → creates a new time-shifted metric."""
    _require_unverified_ok(args, "attach-transformation",
                           "the spec's fact-metric body has no `transformation` field, so a server that "
                           "ignores unknown fields creates an unshifted copy of the source metric.")
    m.login(identity=True)
    r = m.get(f"/api/model/dataModels/{args.model_id}/factMetrics/{args.source_metric}")
    if not r.ok: die(f"source metric GET: {r.status_code} {r.text[:200]}")
    src = r.json()
    body = {
        "information":{"name": args.name},
        "fact": src.get("fact"), "function": src.get("function","sum"),
        "functionProperties": src.get("functionProperties",[]),
        "dimty": src.get("dimty",{}), "format": src.get("format",{}),
        "transformation": {"objectId": args.transformation, "subType":"transformation"},
    }
    cs = open_cs(m)
    r = m.post(f"/api/model/dataModels/{args.model_id}/factMetrics", json=body)
    mid = (_json_or(r, {}).get("information") or {}).get("objectId") if r.ok else None
    if not mid:
        discard_cs(m, cs)
        die(f"transformation metric: {r.status_code} {r.text[:300]}")
    commit_cs(m, cs)
    print(json.dumps({"ok": True, "metric_id": mid}, indent=2))


_FACT_HINT = _re.compile(r"(?:^|_)(FACT|LINEITEM|DETAIL|REL|F|TRANSACTIONS?|ACTIVITY|EVENTS?)(?:_|$)", _re.I)
_RATE_COL  = _re.compile(r"(?:_RATE|_PCT|_RATIO|DISCOUNT|TAX|_PRICE|_COST|BALANCE)", _re.I)

def _is_fact_like(name: str) -> bool:
    if not name: return False
    u = name.upper()
    return bool(_FACT_HINT.search(u)) or u.endswith("_DETAIL") or u.endswith("_FACT")

def cmd_validate_model(m: MSTR, args):
    """Run the post-build quality checklist against a Mosaic data model.

    Enforces the rules in memory/feedback_mosaic_build_quality.md and
    reference_mosaic_relationship_archetypes.md. Exit code non-zero if any
    FAIL check trips.
    """
    m.login()
    mid = args.model_id
    forced_facts = {t.strip().upper() for t in (args.fact_tables or "").split(",") if t.strip()}

    def load(ep, key=None):
        if key:   # list endpoints: page through, so big models are checked in full
            items, failed = _list_all(m, f"/api/model/dataModels/{mid}{ep}", key)
            if failed is not None: die(f"{ep}: {failed.status_code} {failed.text[:200]}")
            return items
        r = m.get(f"/api/model/dataModels/{mid}{ep}")
        if not r.ok: die(f"{ep}: {r.status_code} {r.text[:200]}")
        return _json_or(r, {})

    root    = load("")
    tables  = load("/tables", "tables")
    attrs   = load("/attributes", "attributes")
    metrics = load("/factMetrics", "factMetrics")

    failures, warnings = [], []
    def FAIL(check, msg, obj=None): failures.append({"check":check,"message":msg,"object":obj})
    def WARN(check, msg, obj=None): warnings.append({"check":check,"message":msg,"object":obj})

    # F3 — required top-level fields
    for f in ("dataServeMode","autoJoin","enableAutoHierarchyRelationships"):
        if root.get(f) is None:
            FAIL("F3", f"model.{f} missing")
    if not (root.get("information",{}) or {}).get("name"):
        FAIL("F3", "model.information.name missing")
    if not (root.get("information",{}) or {}).get("description"):
        WARN("W2", "model has no description — degrades AI/Library discoverability")

    # F2 read-back integrity
    def readback(ep, objs, kind):
        for o in objs:
            oid = (o.get("information") or {}).get("objectId")
            name = (o.get("information") or {}).get("name", oid)
            if not oid: continue
            r = m.get(f"/api/model/dataModels/{mid}/{ep}/{oid}")
            if not r.ok:
                FAIL("F2", f"{kind} {name}: GET /{ep}/{oid} -> {r.status_code} (partial commit)", {"name":name,"id":oid,"kind":kind})
    readback("tables",      tables,  "table")
    readback("attributes",  attrs,   "attribute")
    readback("factMetrics", metrics, "factMetric")

    # Detect fact/bridge tables (name-based + usage-based)
    rel_table_usage = {}
    for a in attrs:
        for r in (a.get("relationships") or []):
            rt = (r.get("relationshipTable") or {}).get("name","")
            if rt: rel_table_usage[rt] = rel_table_usage.get(rt,0) + 1
    fact_like = {t["information"]["name"] for t in tables
                 if _is_fact_like(t["information"]["name"])
                    or t["information"]["name"].upper() in forced_facts
                    or rel_table_usage.get(t["information"]["name"],0) >= 2}

    # F1 empty form names + W6 date-hierarchy heuristic + W1 orphan attrs + W5 dup names
    name_counts = {}
    date_like_bases = set()
    for a in attrs:
        name = (a.get("information") or {}).get("name","")
        name_counts[name] = name_counts.get(name,0) + 1
        forms = a.get("forms") or []
        for i,f in enumerate(forms):
            if not (f.get("name") or "").strip():
                FAIL("F1", f"attribute '{name}' form[{i}] has empty name — disables auto-hierarchy and blanks UI labels",
                     {"attribute": name, "form_index": i})
        lookup = (a.get("attributeLookupTable") or {}).get("name","")
        rels = a.get("relationships") or []
        # Skip W1 for date-style attrs (W6 owns those) and for attrs that themselves end in typical ID patterns (they ARE the grain)
        is_date_like = bool(_re.search(r"\b(date|timestamp|day|month|quarter|year)\b", name, _re.I))
        if lookup in fact_like and not rels and not is_date_like:
            (FAIL if args.strict_orphans else WARN)(
                "W1", f"orphan attribute '{name}' on fact/bridge table {lookup} has zero relationships",
                {"attribute": name, "lookup": lookup})
        # W6 date heuristic: treat any attribute whose name ends in "Date" (not a derived Day/Month/etc) as needing 4 derivatives
        if _re.search(r"\b(date|timestamp)\b", name, _re.I) and not _re.search(r"\b(day|month|quarter|year)\b", name, _re.I):
            date_like_bases.add(name)
    for n,c in name_counts.items():
        if c > 1:
            WARN("W5", f"duplicate attribute name '{n}' appears {c}× — likely conformed-dim mis-merge", {"attribute": n})
    for base in sorted(date_like_bases):
        have = {g: any(_re.search(rf"\b{g}\b", (a.get('information') or {}).get('name',''), _re.I)
                       and base.split()[0].lower() in (a.get('information') or {}).get('name','').lower()
                       for a in attrs) for g in ("Day","Month","Quarter","Year")}
        missing = [g for g,v in have.items() if not v]
        if missing:
            WARN("W6", f"date attribute '{base}' missing derived grains: {missing}", {"attribute": base, "missing": missing})

    # W3 — FK coverage per fact table
    for t in tables:
        tname = t["information"]["name"]
        if tname not in fact_like: continue
        parents = set()
        for a in attrs:
            for r in (a.get("relationships") or []):
                if (r.get("relationshipTable") or {}).get("name") == tname:
                    parents.add((r.get("parent") or {}).get("name"))
        if len(parents) < 2:
            WARN("W3", f"fact/bridge table {tname} has only {len(parents)} distinct parent dim(s) declared — most fact tables have ≥3 FKs",
                 {"table": tname, "parents": sorted(p for p in parents if p)})

    # W2 — blank attribute/metric descriptions
    for a in attrs:
        info = a.get("information") or {}
        if not info.get("description"):
            WARN("W2", f"attribute '{info.get('name')}' has no description", {"attribute": info.get("name")})
    for mm in metrics:
        info = mm.get("information") or {}
        if not info.get("description"):
            WARN("W2", f"metric '{info.get('name')}' has no description", {"metric": info.get("name")})

    # W4 — suspect aggregation on rate/price metrics
    for mm in metrics:
        info = mm.get("information") or {}
        name = info.get("name","")
        fn = mm.get("function","").lower()
        if fn == "sum" and _RATE_COL.search(name):
            WARN("W4", f"metric '{name}' is SUM but name suggests a rate/price/balance — review vs AVG or derived formula",
                 {"metric": name, "function": fn})

    # W7 — topology: isolated attributes on fact tables, fact tables with zero
    # relationships, and numeric-named attributes that almost certainly should
    # have been metrics. (See post_build_validate_topology() for the standalone
    # helper that any wiring script can call.)
    topology = post_build_validate_topology(m, mid, attrs=attrs, tables=tables)
    for iso in topology.get("isolated_attributes", []):
        if iso.get("table") in fact_like:
            (FAIL if args.strict_isolation else WARN)(
                "W7-iso",
                f"isolated attribute '{iso['name']}' on fact-like table "
                f"{iso['table']} has zero relationships",
                iso,
            )
    for tname in topology.get("tables_without_relationships", []):
        WARN(
            "W7-tbl",
            f"fact-like table '{tname}' has zero relationships touching it — "
            "isolated table or wiring incomplete",
            {"table": tname},
        )
    for warn in topology.get("numeric_attribute_warnings", []):
        WARN(
            "W7-num",
            f"numeric-named attribute '{warn['name']}' on fact-like table "
            f"{warn['table']} has no relationships — likely a misclassified "
            "metric. Delete or convert to factMetric.",
            warn,
        )

    counts = {"tables": len(tables), "attributes": len(attrs), "factMetrics": len(metrics),
              "relationships": sum(len(a.get("relationships") or []) for a in attrs)}
    report = {
        "modelId": mid,
        "modelName": (root.get("information") or {}).get("name"),
        "counts": counts,
        "fact_like_tables": sorted(fact_like),
        "topology": topology,
        "failures": failures,
        "warnings": warnings,
    }

    if args.diff_against:
        r = m.get(f"/api/model/dataModels/{args.diff_against}")
        if not r.ok: die(f"diff target read: {r.status_code}")
        prev_attrs = load_other(m, args.diff_against, "/attributes", "attributes")
        prev_metrics = load_other(m, args.diff_against, "/factMetrics", "factMetrics")
        prev_counts = {
            "attributes": len(prev_attrs),
            "factMetrics": len(prev_metrics),
            "relationships": sum(len(a.get("relationships") or []) for a in prev_attrs),
        }
        prev_names = {(a.get('information') or {}).get('name') for a in prev_attrs}
        cur_names  = {(a.get('information') or {}).get('name') for a in attrs}
        report["diff"] = {
            "prev_counts": prev_counts,
            "cur_counts": counts,
            "attributes_removed": sorted(prev_names - cur_names),
            "attributes_added":   sorted(cur_names  - prev_names),
        }
        for k in ("attributes","factMetrics","relationships"):
            if counts[k] < prev_counts[k]:
                FAIL("F-diff", f"count regression: {k} {prev_counts[k]} -> {counts[k]}")

    # Pretty print
    status = "PASS" if not failures else "FAIL"
    print(f"\n[validate-model:{mid}] {status}")
    print(f"  name: {report['modelName']}")
    print(f"  counts: tables={counts['tables']} attributes={counts['attributes']} factMetrics={counts['factMetrics']} relationships={counts['relationships']}")
    print(f"  fact-like tables: {', '.join(report['fact_like_tables']) or '(none detected)'}")
    if failures:
        print(f"\n  FAIL ({len(failures)}):")
        for f in failures: print(f"    [{f['check']}] {f['message']}")
    if warnings:
        print(f"\n  WARN ({len(warnings)}):")
        by = {}
        for w in warnings: by.setdefault(w["check"], []).append(w["message"])
        for k, msgs in by.items():
            print(f"    [{k}] {len(msgs)} issue(s)")
            for msg in msgs[:5]: print(f"        - {msg}")
            if len(msgs) > 5: print(f"        … +{len(msgs)-5} more")
    if "diff" in report:
        d = report["diff"]
        print(f"\n  DIFF vs {args.diff_against}:")
        print(f"    prev: {d['prev_counts']}")
        print(f"    cur:  {d['cur_counts']}")
        if d["attributes_removed"]: print(f"    removed attrs: {d['attributes_removed'][:10]}{' …' if len(d['attributes_removed'])>10 else ''}")
        if d["attributes_added"]:   print(f"    added attrs:   {d['attributes_added'][:10]}{' …'   if len(d['attributes_added'])>10   else ''}")

    if args.json:
        print("\n" + json.dumps(report, indent=2, default=str))

    sys.exit(1 if failures else 0)


def load_other(m: MSTR, mid: str, ep: str, key: str):
    items, failed = _list_all(m, f"/api/model/dataModels/{mid}{ep}", key)
    if failed is not None: die(f"diff read {ep}: {failed.status_code}")
    return items


def cmd_validate_topology(m: MSTR, args):
    """Lightweight topology check designed to be the LAST step of any
    relationship-wiring or build script. Prints a short status line plus an
    optional JSON report, and exits non-zero on any finding when --strict.

    Failure modes detected:
      - Isolated attributes on fact-like tables (no relationships).
      - Fact-like tables with zero relationships touching them.
      - Numeric-named attributes that look like measures but landed as attrs.
      - Expected tables missing from the model (when --expected-tables given).
    """
    m.login()
    expected = None
    if args.expected_tables:
        expected = [t.strip() for t in args.expected_tables.split(",") if t.strip()]
    report = post_build_validate_topology(m, args.model_id, expected_tables=expected)

    if "error" in report:
        die(report["error"])

    counts = report["counts"]
    issues = (len(report["isolated_attributes"])
              + len(report["tables_without_relationships"])
              + len(report["missing_expected_tables"])
              + len(report["numeric_attribute_warnings"]))
    status = "OK" if issues == 0 else f"FOUND {issues} ISSUE(S)"
    print(f"\n[validate-topology:{args.model_id}] {status}")
    print(f"  counts: tables={counts['tables']} attributes={counts['attributes']} "
          f"relationships={counts['relationships']}")
    if report["isolated_attributes"]:
        print(f"  isolated attributes ({len(report['isolated_attributes'])}):")
        for iso in report["isolated_attributes"][:10]:
            print(f"    - {iso['name']} (table={iso.get('table') or '?'})")
        if len(report["isolated_attributes"]) > 10:
            print(f"    … +{len(report['isolated_attributes'])-10} more")
    if report["tables_without_relationships"]:
        print(f"  fact-like tables with no relationships: "
              f"{', '.join(report['tables_without_relationships'])}")
    if report["missing_expected_tables"]:
        print(f"  missing expected tables: "
              f"{', '.join(report['missing_expected_tables'])}")
    if report["numeric_attribute_warnings"]:
        print(f"  likely-misclassified numeric attributes "
              f"({len(report['numeric_attribute_warnings'])}):")
        for warn in report["numeric_attribute_warnings"][:10]:
            print(f"    - {warn['name']} (table={warn.get('table') or '?'})")

    if args.json:
        print("\n" + json.dumps(report, indent=2, default=str))

    if args.strict and issues:
        sys.exit(1)


def cmd_build_from_config(m: MSTR, args):
    """Declarative build from a YAML/JSON spec. See memory/reference_mosaic_config_schema.md."""
    spec = load_structured_file(args.config) or {}
    if not isinstance(spec, dict) or not spec.get("name"):
        die(f"{args.config}: a build config needs at least `name` and `sources`")
    try:
        sources = [f"{s['instance']}:{s['schema']}:{','.join(s['tables'])}" for s in spec.get("sources", [])]
    except (KeyError, TypeError):
        die(f"{args.config}: each sources[] entry needs instance, schema and a tables list")
    # Rehydrate into argparse-like namespace and reuse cmd_build.
    class NS: pass
    ns = NS()
    ns.name         = spec["name"]
    ns.source       = sources
    ns.instance = ns.schema = None; ns.tables = []
    ns.dest_folder  = spec.get("destination_folder", args.dest_folder)
    ns.data_serve_mode = spec.get("data_serve_mode","connect_live")
    ns.attr_cols    = spec.get("attr_cols", [])
    ns.metric_cols  = spec.get("metric_cols", [])
    ns.skip_cols    = spec.get("skip_cols", [])
    ns.skip_relationships = spec.get("skip_relationships", False)
    ns.dictionary   = spec.get("dictionary") or spec.get("data_dictionary")
    # Conformance / FK maps — equivalent to the `build` CLI's --conformance-map
    # and --fk-map flags. Accept both `conformance_map_file` (preferred,
    # explicit about the "file path" semantics) and `conformance_map` (short
    # alias). Same for fk_map. Useful for warehouses with prefixed column
    # naming where auto-conformance via column-name identity won't fire.
    ns.conformance_map = (
        spec.get("conformance_map_file")
        or spec.get("conformance_map")
    )
    ns.fk_map = (
        spec.get("fk_map_file")
        or spec.get("fk_map")
    )
    erd = []
    for key in ("erd", "erds"):
        value = spec.get(key)
        if isinstance(value, list):
            erd.extend(value)
        elif value:
            erd.append(value)
    ns.erd          = erd
    # Security filters go through as mappings — {name, qualification (object, JSON string,
    # @file or ATTR_ID[:FORM_ID]=VALUE), members (list or comma string)} — so a qualification
    # object never has to survive a round trip through the CLI string syntax.
    ns.security_filter = [dict(sf) for sf in spec.get("security_filters", []) if isinstance(sf, dict)]
    ns.grant        = [_config_acl_entry(g, args.config) for g in spec.get("grants", [])]
    ns.deny         = [_config_acl_entry(g, args.config) for g in spec.get("denies", [])]
    ns.translate    = [_config_translation_entry(t, args.config) for t in spec.get("translations", [])]
    ns.certify      = spec.get("certify", False)
    ns.publish      = spec.get("publish", False)
    # how long to wait for an in-memory publish to confirm (cross-store models can take several minutes); bounded
    ns.publish_wait = max(60, min(1800, int(spec.get("publish_wait_seconds", 180) or 180)))
    cmd_build(m, ns)


def _config_acl_entry(g, source: str) -> str:
    """build-from-config grant/deny {trustee, rights (list or "view"/"browse,read" string),
    type?: user|user_group} → the CLI's 'trustee:rights[:type]'."""
    if not isinstance(g, dict) or not g.get("trustee") or not g.get("rights"):
        die(f"{source}: each grant/deny needs `trustee` and `rights`, got {g!r}")
    rights = g["rights"]
    rights = ",".join(str(r) for r in rights) if isinstance(rights, (list, tuple)) else str(rights)
    kind = g.get("type") or g.get("subType") or g.get("trustee_type")
    return f"{g['trustee']}:{rights}" + (f":{kind}" if kind else "")


def _config_translation_entry(t, source: str) -> str:
    """build-from-config translation {object, locale, text, sub_type?, field?} → the CLI's
    'objectId[:subType]:locale[:field]=text'."""
    if not isinstance(t, dict) or not t.get("object") or not t.get("locale") or "text" not in t:
        die(f"{source}: each translation needs `object`, `locale` and `text`, got {t!r}")
    sub_type = t.get("sub_type") or t.get("subType")
    field = t.get("field")
    if field and not sub_type:
        die(f"{source}: translation {t!r} sets `field`, so it also needs `sub_type`")
    left = ":".join(str(x) for x in (t["object"], sub_type, t["locale"], field) if x)
    return f"{left}={t['text']}"


# ── CLI ───────────────────────────────────────────────────────────────────────
def build_parser():
    # allow_abbrev=False: `--pass x` must not quietly mean --password (and slip past the
    # secret-flag warning in main()).
    p = argparse.ArgumentParser(prog="build_mosaic.py", allow_abbrev=False)
    p.add_argument("--base",        default=DEFAULT_BASE)
    p.add_argument("--project-id",  default=DEFAULT_PROJECT_ID)
    p.add_argument("--user",        default=DEFAULT_USER)
    p.add_argument("--password",    default=DEFAULT_PASSWORD)
    p.add_argument("--login-mode",  type=int, default=DEFAULT_LOGIN_MODE,
                   help="1 Standard (default), 16 LDAP, 8 Anonymous, 4096 API token (the token goes in "
                        "MSTR_API_TOKEN, not MSTR_USER). Env: MSTR_LOGIN_MODE.")
    strategy_auth.add_auth_method_arg(p)
    # Borrowed-session auth (Studio Cloud / SSO tenants). When --auth-token is
    # provided, MSTR.login() skips /auth/login and /auth/logout so the external
    # owner's session is left intact. See README.md → "Borrowed-session auth".
    p.add_argument("--auth-token",     default=DEFAULT_AUTH_TOKEN,
                   help="Pre-existing X-MSTR-AuthToken (e.g. from a browser session). "
                        "Skips /auth/login and /auth/logout. Env: MSTR_AUTH_TOKEN.")
    p.add_argument("--identity-token", default=DEFAULT_IDENTITY_TOKEN,
                   help="Pre-existing X-MSTR-IdentityToken (required for Mosaic Modeling "
                        "Service changesets). If unset, the script attempts to mint one "
                        "via /api/auth/identityToken using --auth-token + cookies. "
                        "Env: MSTR_IDENTITY_TOKEN.")
    p.add_argument("--session-cookie", default=DEFAULT_SESSION_COOKIE,
                   help="JSESSIONID value from the browser session. Required alongside "
                        "--auth-token for project-scoped APIs on Studio Cloud. "
                        "Env: MSTR_SESSION_COOKIE.")
    p.add_argument("--ingress-cookie", default=DEFAULT_INGRESS_COOKIE,
                   help="library-ingress value from the browser session (Studio Cloud "
                        "routing cookie). Env: MSTR_INGRESS_COOKIE.")
    p.add_argument("-v","--verbose", action="store_true")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("auth-probe")

    sp = sub.add_parser("list-datasources")
    sp.add_argument("--name", help="filter by substring match on name")

    sp = sub.add_parser("list-namespaces")
    sp.add_argument("--instance"); sp.add_argument("--instance-id")

    sp = sub.add_parser("list-tables")
    sp.add_argument("--instance"); sp.add_argument("--instance-id")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--match", help="substring match on table name")

    sp = sub.add_parser("describe-tables",
                        help="Describe many tables in one login to avoid the project session cap")
    sp.add_argument("--source", action="append", required=True,
                    help="Repeatable: instanceId:namespace:table")

    sp = sub.add_parser("kill-sessions",
                        help="List your open Intelligence Server connections (the per-project "
                             "session cap, 8004cb0a) and disconnect them with --yes. Needs a "
                             "monitoring privilege. See memory/feedback_build_mosaic_session_leak.md.")
    sp.add_argument("--project", default="", help="only connections to this project (name or id)")
    sp.add_argument("--idle-minutes", type=float, default=5.0,
                    help="only connections idle at least this long (default 5)")
    sp.add_argument("--yes", action="store_true", help="disconnect them (default: list only)")

    sp = sub.add_parser("describe-table")
    sp.add_argument("--instance"); sp.add_argument("--instance-id")
    sp.add_argument("--namespace", required=True)
    sp.add_argument("--table",     required=True)

    sp = sub.add_parser("discover")
    sp.add_argument("--instance-id"); sp.add_argument("--namespace"); sp.add_argument("--table")

    sp = sub.add_parser("openapi-summary")
    sp.add_argument("--out", help="optional path to save the raw OpenAPI YAML")
    sp.add_argument("--path-filter", action="append", default=[],
                    help="path prefix to include in selected_paths; repeatable")
    sp.add_argument("--limit", type=int, default=120)

    sp = sub.add_parser("openapi-search")
    sp.add_argument("pattern", help="regex/text pattern to search in the OpenAPI YAML")
    sp.add_argument("--file", help="local OpenAPI YAML path; defaults to ./openapi.yaml, then live tenant")
    sp.add_argument("--limit", type=int, default=40)
    sp.add_argument("--context", type=int, default=0)
    sp.add_argument("--case-sensitive", action="store_true")

    sp = sub.add_parser("api-call")
    sp.add_argument("--method", default="GET", choices=["GET","POST","PUT","PATCH","DELETE"])
    sp.add_argument("--path", required=True, help="REST path, e.g. /api/projects")
    sp.add_argument("--param", action="append", default=[], help="query param key=value; repeatable")
    sp.add_argument("--header", action="append", default=[], help="extra header key=value; repeatable")
    sp.add_argument("--json", help="JSON request body string")
    sp.add_argument("--json-file", help="JSON request body file")
    sp.add_argument("--file", action="append", default=[],
                    help="multipart file part FIELD=PATH; repeatable. Switches the request to multipart/form-data")
    sp.add_argument("--form", action="append", default=[],
                    help="multipart text field key=value; repeatable. Use with --file for upload endpoints")
    sp.add_argument("--out", help="save response body to file")
    sp.add_argument("--text-limit", type=int, default=8000)
    sp.add_argument("--no-auth", action="store_true", help="do not login first; useful for public OpenAPI paths")
    sp.add_argument("--with-identity-token", action="store_true",
                    help="also request X-MSTR-IdentityToken; use for Mosaic data-model Modeling Service writes, not classic/project Modeling calls")
    sp.add_argument("--yes", action="store_true", help="required for DELETE")
    sp.add_argument("--show-secrets", action="store_true",
                    help="print token/password fields of the response body (redacted by default; "
                         "--out always gets the full body, in a 0600 file)")

    sp = sub.add_parser("resolve-users")
    sp.add_argument("--user", dest="lookup_user", action="append", default=[],
                    help="user id, username, full name, or email; repeatable")
    sp.add_argument("--file", action="append", default=[],
                    help="CSV/JSON/YAML with id, username, email, name/fullName rows; repeatable")
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--first", action="store_true", help="return first candidate instead of reporting ambiguity")

    sp = sub.add_parser("create-users")
    sp.add_argument("--file", action="append", required=True,
                    help="CSV/JSON/YAML rows. Required fields: username and fullName/name; email is optional.")
    sp.add_argument("--source-user-id", help="optional source user to duplicate from")
    sp.add_argument("--default-password-env", default="MSTR_NEW_USER_PASSWORD",
                    help="env var used as default password when a row omits password")
    sp.add_argument("--email-address-name", default="Default email")
    sp.add_argument("--skip-email-address", action="store_true")
    sp.add_argument("--allow-existing", action="store_true", help="create even if an exact username/email match is found")
    sp.add_argument("--check-existing", action="store_true", help="during dry-run, login and check for exact existing users")
    sp.add_argument("--yes", action="store_true", help="actually create users; without this, print a dry-run plan")

    sp = sub.add_parser("search-objects")
    sp.add_argument("--name", required=True)
    sp.add_argument("--type", action="append", default=[],
                    help="numeric EnumDSSObjectType; repeatable")
    sp.add_argument("--subtype", help="numeric subtype filter applied client-side")
    sp.add_argument("--pattern", type=int, default=4,
                    help="Strategy search pattern enum; default 4")
    sp.add_argument("--project-id-filter", action="append", default=[],
                    help="search project ID query param; repeatable")
    sp.add_argument("--ancestors", action="store_true")
    sp.add_argument("--offset", type=int, default=0)
    sp.add_argument("--limit", type=int, default=25)
    sp.add_argument("--raw", action="store_true")

    object_kinds = sorted(MODEL_OBJECT_KINDS)
    sp = sub.add_parser("export-model", help="save the model's full definition (for lookups --model-export)")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--out", required=True)

    sp = sub.add_parser("get-model-object")
    sp.add_argument("--kind", required=True, choices=object_kinds,
                    help="Mosaic-contained kind, or project_/legacy_ kind for classic schema objects")
    sp.add_argument("--model-id", help="required for Mosaic-contained objects")
    sp.add_argument("--object-id", help="required except kind=data_model")
    sp.add_argument("--show-expression-as", action="append", choices=["tokens", "tree"])
    sp.add_argument("--show-advanced-properties", action="store_true")
    sp.add_argument("--fields")
    sp.add_argument("--out", help="save response body to JSON/text file")
    sp.add_argument("--text-limit", type=int, default=8000)

    sp = sub.add_parser("patch-model-object")
    sp.add_argument("--kind", required=True, choices=object_kinds,
                    help="Mosaic-contained kind, or project_/legacy_ kind for classic schema objects")
    sp.add_argument("--model-id", help="required for Mosaic-contained objects")
    sp.add_argument("--object-id", help="required except kind=data_model")
    sp.add_argument("--method", default=None, choices=["PATCH", "PUT"],
                    help="default: the verb the spec defines for the kind (PUT for metric, security_filter, "
                         "project_metric, project_fact, project_filter; PATCH otherwise)")
    sp.add_argument("--schema-edit", action="store_true",
                    help="open a schemaEdit changeset (automatic for project_attribute/fact/table/"
                         "transformation/hierarchy)")
    sp.add_argument("--json", help="JSON request body string")
    sp.add_argument("--json-file", help="JSON request body file")
    sp.add_argument("--show-expression-as", action="append", choices=["tokens", "tree"])
    sp.add_argument("--show-advanced-properties", action="store_true")
    sp.add_argument("--fields")
    sp.add_argument("--before-out", help="save current definition before patching")
    sp.add_argument("--include-before", action="store_true", help="include current definition in command output")
    sp.add_argument("--text-limit", type=int, default=8000)
    sp.add_argument("--yes", action="store_true", help="required after reviewing target and patch body")

    sp = sub.add_parser("build")
    sp.add_argument("--name", required=True, help="new model name")
    sp.add_argument("--source", action="append", default=[],
                    help="INSTANCE:SCHEMA:T1,T2,... (repeatable, for multi-source models)")
    sp.add_argument("--instance"); sp.add_argument("--schema"); sp.add_argument("--tables", nargs="*")
    sp.add_argument("--dest-folder", default=DEFAULT_DEST_FOLDER)
    sp.add_argument("--data-serve-mode", default="connect_live", choices=["connect_live","in_memory","off_memory"])
    sp.add_argument("--attr-cols",   nargs="*", default=[], help="column names to force as attributes")
    sp.add_argument("--metric-cols", nargs="*", default=[], help="column names to force as metrics")
    sp.add_argument("--skip-cols",   nargs="*", default=[],
                    help="TABLE.COLUMN or bare column names to exclude from the model "
                         "entirely — not modeled as attribute or metric (e.g. free-text "
                         "comment columns). They remain physical columns on the table.")
    sp.add_argument("--skip-relationships", action="store_true")
    sp.add_argument("--dictionary", help="JSON/YAML/CSV file of attribute+metric name/description overrides and relationships")
    sp.add_argument("--erd", action="append", default=[],
                    help="ERD file (JSON/YAML list of relationships, DBML, Mermaid, or SQL DDL). Repeatable.")
    sp.add_argument("--conformance-map",
                    help="JSON/YAML {logical_name: [TABLE.COLUMN, ...]} — forces those columns to collapse "
                         "into one conformed attribute. Overrides column-name inference. See "
                         "feedback_mosaic_relationship_wiring.md.")
    sp.add_argument("--fk-map",
                    help="JSON/YAML {child_table.child_col: parent_table.parent_col} — normalizes "
                         "semantically-same-but-differently-named FKs so they conform to the parent's "
                         "logical name. Useful for multi-DB builds with e.g. primary_<entity>_id vs <entity>_id.")
    sp.add_argument("--security-filter", action="append", default=[],
                    help="Mosaic data-model SF: 'NAME=ATTR_ID[:FORM_ID]=VALUE|USER,USER' or 'NAME=@qualification.json|USER,USER' (repeatable)")
    sp.add_argument("--grant", action="append", default=[],
                    help="ACL grant 'trusteeId:rights[:user|user_group]' (repeatable). Rights: view, modify, "
                         "full, or browse,use_execute,read,write,delete,control,use,execute, or a number")
    sp.add_argument("--deny", action="append", default=[],
                    help="ACL deny, same syntax as --grant (repeatable)")
    sp.add_argument("--translate", action="append", default=[],
                    help="'objectId[:subType]:locale[:name|description]=translation' (repeatable)")
    sp.add_argument("--certify", action="store_true")
    sp.add_argument("--publish", action="store_true", help="publish cube for in_memory mode")

    sp = sub.add_parser("validate-model",
        help="Run post-build quality checks on a Mosaic data model (see memory/reference_mosaic_build_validation.md).")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--fact-tables", help="comma-separated table names to force-classify as fact/bridge")
    sp.add_argument("--strict-orphans", action="store_true", help="promote W1 orphan-attributes from WARN to FAIL")
    sp.add_argument("--strict-isolation", action="store_true",
                    help="promote W7-iso (isolated attributes on fact tables) from WARN to FAIL — "
                         "use as the standard tail of every wiring script")
    sp.add_argument("--diff-against", help="another modelId; emit a count diff and fail on regressions")
    sp.add_argument("--json", action="store_true", help="also print the full JSON report")

    sp = sub.add_parser("validate-topology",
        help="Lightweight post-build topology check: isolated attributes, fact "
             "tables with no relationships, numeric-named attrs that should "
             "have been metrics. Exit 1 on any finding when --strict is set.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--expected-tables",
                    help="comma-separated table names that MUST appear in the model")
    sp.add_argument("--strict", action="store_true",
                    help="exit non-zero on any finding (recommended as the "
                         "tail of every wiring/build script)")
    sp.add_argument("--json", action="store_true", help="print the full JSON report")

    sp = sub.add_parser("build-from-config")
    sp.add_argument("--config", required=True, help="path to YAML/JSON spec file")
    sp.add_argument("--dest-folder", default=DEFAULT_DEST_FOLDER)

    sp = sub.add_parser(
        "build-from-schema-objects",
        help="Build a Mosaic data model from existing classic schema object IDs."
    )
    sp.add_argument("--name", required=True,
                    help="Name for the new Mosaic data model.")
    sp.add_argument("--attribute-ids", default="",
                    help="Comma-separated classic attribute object IDs, "
                         "or @filepath for one-per-line.")
    sp.add_argument("--fact-ids", default="",
                    help="Comma-separated classic fact object IDs, or @filepath.")
    sp.add_argument("--metric-ids", default="",
                    help="Comma-separated classic metric object IDs, or @filepath.")
    sp.add_argument("--instance-id", default="",
                    help="Fallback datasource ID if classic table metadata "
                         "lacks databaseInstance.")
    sp.add_argument("--schema", default="",
                    help="Fallback warehouse schema if classic table metadata "
                         "lacks namespace.")
    sp.add_argument("--data-serve-mode",
                    choices=["connect_live", "in_memory", "off_memory"],
                    default="connect_live")
    sp.add_argument("--publish", action="store_true",
                    help="Publish to in-memory after build (forces "
                         "data-serve-mode=in_memory).")
    sp.add_argument("--dest-folder", default=DEFAULT_DEST_FOLDER,
                    help="Destination folder ID for the new model.")
    sp.add_argument("--review-file", default="",
                    help="Write a JSON review file with warnings and "
                         "created object IDs.")
    sp.add_argument("--use-batch", action="store_true",
                    help="create attributes/fact metrics through the UI's undocumented POST "
                         "/api/model/batch instead of one documented POST per object")

    sp = sub.add_parser("set-serve-mode")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--mode", required=True, choices=["connect_live","in_memory","off_memory"])

    sp = sub.add_parser("publish"); sp.add_argument("--model-id", required=True)
    sp.add_argument("--poll-seconds", type=int, default=180,
                    help="max wait for every table to reach 'loaded' before failing")
    sp.add_argument("--skip-classify", action="store_true",
                    help="skip GET /api/objects/{id}?type=3 surface classification; "
                         "assume Mosaic data model. Saves one project-scoped call to "
                         "stay under the session cap when chaining build→publish.")

    sp = sub.add_parser("wire-relationships",
        help="Post-build relationship wiring with step-3/step-5 validation; avoids 8004ccdb/8004ccc7 retry loops. See memory/feedback_mosaic_relationship_wiring.md.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--hints", required=True,
                    help="JSON/YAML file: {relationships:[{parent_attribute, child_attribute, relationship_table, type}]}")
    sp.add_argument("--dry-run", action="store_true",
                    help="validate + print plan without issuing PUTs")
    sp.add_argument("--replace", action="store_true",
                    help="OVERWRITE existing relationships on each child attribute "
                         "(destructive — wipes incoming rels too). Default is "
                         "merge-aware: fetch existing, dedupe, PUT the union.")

    sp = sub.add_parser("merge-attributes",
        help="Conform differently-named FK columns by merging child expressions "
             "into the parent attribute. The Kimball pattern for warehouses where "
             "prefixed surrogate keys (i_item_sk vs ss_item_sk) defeat auto-conformance.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--hints",    required=True,
                    help="JSON/YAML map of {child_table.child_col: parent_table.parent_col} "
                         "(same shape as `build --fk-map`); or {'merges':[{child,parent}]} envelope.")
    sp.add_argument("--dry-run",  action="store_true",
                    help="validate + print plan without writing")
    sp.add_argument("--keep-children", action="store_true",
                    help="leave the now-redundant child attributes in place "
                         "instead of deleting them (useful for staged rollouts)")
    sp.add_argument("--release-locks", action="store_true",
                    help="if open_cs hits 8004cc41 and GET /api/model/schema/lock says you own the "
                         "lock, release it and retry once (only when no other job uses this "
                         "account). See the `release-locks` subcommand for the standalone equivalent.")

    sp = sub.add_parser("release-locks",
        help="Release stuck Modeling Service schemaEdit changesets owned by the "
             "current user. Use after a crashed build/merge/wire run leaves "
             "8004cc41 on every subsequent open.")
    sp.add_argument("--max-iters", type=int, default=5,
                    help="cap on release attempts (each provokes one lock conflict "
                         "to discover the LOCKID). Default 5.")

    sp = sub.add_parser("refresh",
                        help="Reload a Mosaic model's data (a publish with a per-table refresh policy).")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--refresh-type", default="replace",
                    choices=["replace", "add", "update", "upsert", "incremental"],
                    help="publish refreshPolicy for every table (default replace; incremental = upsert)")
    sp.add_argument("--poll-seconds", type=int, default=180,
                    help="max wait for every table to finish before failing")

    sp = sub.add_parser("delete-model")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--yes", action="store_true", help="required after verifying the Mosaic data model id")
    sp.add_argument("--any-type", action="store_true",
                    help="delete even when the id is not a Mosaic data model (a cube or report)")

    sp = sub.add_parser("set-acl")
    sp.add_argument("--model-id", help="required for data-model-contained objects")
    sp.add_argument("--object-id", required=True)
    sp.add_argument("--sub-type", default="report_emma_cube",
                    help="object subtype for data model ACL endpoint: report_emma_cube (model root, default), "
                         "attribute, metric, logical_table (contained table), md_security_filter")
    sp.add_argument("--grant", action="append", default=[],
                    help="'trusteeId:rights[:user|user_group]' (repeatable). Adds the rights and keeps the "
                         "trustee's others; also clears them from the trustee's deny set")
    sp.add_argument("--deny", action="append", default=[],
                    help="same syntax as --grant (repeatable). Adds the denies and keeps the trustee's grants "
                         "for other rights")
    sp.add_argument("--replace-trustee", action="store_true",
                    help="set each named trustee's entry to exactly the given grants/denies "
                         "(other trustees are never touched)")

    sp = sub.add_parser("add-security-filter")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--spec", required=True,
                    help="Mosaic data-model SF: 'NAME=ATTR_ID[:FORM_ID]=VALUE|USER,USER' or 'NAME=@qualification.json|USER,USER'")

    sp = sub.add_parser("translate")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--sub-type", default="report_emma_cube",
                    help="subType for entries without one (default: the model root, report_emma_cube; "
                         "data_model is accepted as an alias)")
    sp.add_argument("--entry", action="append", required=True,
                    help="'objectId[:subType]:locale[:name|description]=translation' (repeatable)")

    sp = sub.add_parser("certify"); sp.add_argument("--object-id", required=True)

    sp = sub.add_parser("create-transformation",
                        help="UNVERIFIED: posts a body the 2026 spec does not define (spec: POST "
                             "/api/model/transformations with attributes[].forms expressions). "
                             "Verify live before relying on it.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--member", action="append", required=True,
                    help="'attributeId=offset' e.g. 'ABC...=-1' for prior period (repeatable)")
    sp.add_argument("--unverified-ok", action="store_true", help="send the unverified request anyway")

    sp = sub.add_parser("create-compound-metric",
        help="Derived metric from arithmetic over existing metrics ('[Sales] - [Returns]', "
             "'([A] + [B]) / [C]', '[A] * 100'), posted as an expression tree in one changeset "
             "(see memory/reference_mosaic_derived_metrics.md §0).")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--formula", required=True,
                    help="operands [metric name], [objectId], [fact_metric:name] or [metric:name] (bare "
                         "objectIds also work); operators + - * / and parentheses; whole-number constants")
    sp.add_argument("--join", choices=["inner", "outer"],
                    help="formulaJoinType. outer keeps a group when one operand has no rows for it (the "
                         "blank operand counts as 0); the default (inner) drops that group from results")
    sp.add_argument("--format", help=f"number format preset: {', '.join(sorted(METRIC_FORMAT_PRESETS))}")
    sp.add_argument("--description", default="", help=f"business description (max {DESCRIPTION_MAX} characters)")
    sp.add_argument("--replace", action="store_true",
                    help="rewrite the existing derived metric with this name in place (full-body PUT); "
                         "keeps its description and format unless given again")
    sp.add_argument("--dry-run", action="store_true", help="print the request; open no changeset")
    sp.add_argument("--unverified-ok", action="store_true",
                    help="no longer needed (the tree path is verified); accepted so old scripts still run")

    sp = sub.add_parser("create-conditional-metric",
        help="Derived metric scoped by a filter embedded in the metric (<attribute> IN (<elements>), "
             "optionally ANDed with more), with an optional latest/first-period level rule, in one "
             "changeset (see memory/reference_mosaic_derived_metrics.md §0c, §0d).")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--name", required=True)
    what = sp.add_mutually_exclusive_group(required=True)
    what.add_argument("--source-metric",
                      help="fact metric to aggregate with --function: name (case-insensitive) or objectId")
    what.add_argument("--count-attribute",
                      help="attribute whose distinct elements to count (customers, patients, accounts...): "
                           "name or objectId")
    sp.add_argument("--attribute", required=True,
                    help="attribute to filter on: name (case-insensitive) or objectId")
    sp.add_argument("--elements", required=True, nargs="+",
                    help="attribute ID-form values to keep (IN list); quote values that contain spaces")
    sp.add_argument("--and", dest="and_conditions", action="append", nargs="+", metavar="ATTR_OR_VALUE",
                    help="another condition, ANDed: the attribute (name or objectId) then its ID-form values. "
                         "Repeatable.")
    sp.add_argument("--function", choices=list(CONDITIONAL_FUNCTIONS),
                    help="aggregation around --source-metric (default Sum). Count counts the fact rows and, "
                         "unlike a distinct count of an attribute, honors --level-rule")
    sp.add_argument("--level-rule", choices=sorted(LEVEL_RULE_AGGREGATIONS),
                    help="read only the latest (last) or earliest (first) period's rows, per group, for "
                         "balances, statuses and other snapshot values that must not be summed over time. "
                         "*_lookup variants use the attribute's lookup; see §0d before relying on them")
    sp.add_argument("--level-attribute",
                    help="the period-type attribute (snapshot date, month, week) for --level-rule")
    sp.add_argument("--format", help=f"number format preset: {', '.join(sorted(METRIC_FORMAT_PRESETS))} "
                                     f"(default: the source metric's format; integer for counts)")
    sp.add_argument("--description", default="", help=f"business description (max {DESCRIPTION_MAX} characters)")
    sp.add_argument("--dry-run", action="store_true", help="print the three requests; open no changeset")

    sp = sub.add_parser("delete-derived-metric",
        help="Delete one derived metric (never a fact metric or attribute) in a changeset, after "
             "checking which other derived metrics refer to it.")
    sp.add_argument("--model-id", required=True)
    target = sp.add_mutually_exclusive_group(required=True)
    target.add_argument("--name", help="derived metric name (case-insensitive)")
    target.add_argument("--object-id", help="derived metric objectId")
    sp.add_argument("--yes", action="store_true", help="delete it (required unless --dry-run)")
    sp.add_argument("--force", action="store_true",
                    help="delete even when other derived metrics refer to it (they may break)")
    sp.add_argument("--dry-run", action="store_true", help="show the target and its dependents; delete nothing")

    sp = sub.add_parser("create-level-metric",
        help="Once-per-entity metric Sum(Max([M]) {~+, [Entity]+}) {~+}: for amounts repeated on every row of a "
             "periodic-snapshot fact (see memory/reference_mosaic_derived_metrics.md §0d rule 1).")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--source-metric", required=True,
                    help="fact or derived metric: [name], objectId, or fact_metric:/metric: prefix when ambiguous")
    sp.add_argument("--entity-attribute", required=True,
                    help="the entity the value belongs to (customer, account, loan...): name or objectId")
    sp.add_argument("--inner", choices=list(LEVEL_FUNCTIONS), default="Max",
                    help="per-entity function (default Max: the value repeated on the entity's rows)")
    sp.add_argument("--outer", choices=list(LEVEL_FUNCTIONS), default="Sum",
                    help="function across entities (default Sum; Avg for a simple per-entity average)")
    sp.add_argument("--format", help=f"number format preset: {', '.join(sorted(METRIC_FORMAT_PRESETS))} "
                                     f"(default: the source fact metric's format)")
    sp.add_argument("--description", default="", help=f"business description (max {DESCRIPTION_MAX} characters)")
    sp.add_argument("--dry-run", action="store_true", help="print the request; open no changeset")

    sp = sub.add_parser("create-weighted-average",
        help="Weighted average taken once per entity, as numerator and denominator helpers plus the ratio "
             "(see memory/reference_mosaic_derived_metrics.md §0d rule 2).")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--name", required=True, help="name of the average metric")
    value = sp.add_mutually_exclusive_group(required=True)
    value.add_argument("--value-attribute", help="attribute holding the value per entity (score, ratio, rate)")
    value.add_argument("--value-metric", help="metric holding the value per entity")
    sp.add_argument("--weight-metric", required=True, help="metric to weight by (an amount per entity)")
    sp.add_argument("--entity-attribute", required=True, help="the entity: name or objectId")
    sp.add_argument("--numerator-name", help="default: '<name> numerator'")
    sp.add_argument("--denominator-name", help="default: '<name> denominator'")
    sp.add_argument("--format", help=f"number format preset for the average: {', '.join(sorted(METRIC_FORMAT_PRESETS))}")
    sp.add_argument("--description", default="", help=f"business description (max {DESCRIPTION_MAX} characters)")
    sp.add_argument("--dry-run", action="store_true", help="print the three requests; open no changeset")

    sp = sub.add_parser("rename-describe",
        help="Rename attributes and fact metrics and/or set their descriptions by name, in one changeset "
             "(no republish). Spec: {items: [{object_type, current_name, new_name, description}]}.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--spec", required=True, help="JSON file with the items")
    sp.add_argument("--dry-run", action="store_true", help="print the changes; open no changeset")

    sp = sub.add_parser("add-time-attributes",
        help="Time attributes (year, quarter, month, week, day intervals) on date or timestamp attributes, in one "
             "changeset. Convert a yyyyMM number first with set-column-types. Republish afterwards.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--attribute", help="date attribute (name as the connector shows it, or the model's)")
    sp.add_argument("--intervals", default="year,quarter,month", help="comma-separated: " + ", ".join(TIME_INTERVALS))
    sp.add_argument("--spec", help="JSON {items: [{attribute, intervals: [...]}]} instead of --attribute")
    sp.add_argument("--dry-run", action="store_true", help="print the request bodies; open no changeset")

    sp = sub.add_parser("show-forms",
        help="Make hidden forms visible: add them to the attribute's report and browse displays (the connector lists "
             "only displayed forms). Keeps the displays it has. Republish and re-certify afterwards.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--attribute", required=True, help="attribute (name as the connector shows it, or the model's)")
    sp.add_argument("--forms", required=True, help="comma-separated form names or categories (ID, DESC)")
    sp.add_argument("--dry-run", action="store_true", help="print the request body; open no changeset")

    sp = sub.add_parser("add-column-attribute",
        help="Attribute that reads one existing column as-is, so a numeric column read as a fact metric can also be "
             "grouped and filtered on (number of units, a score band, a code stored as a number). Not a formula: "
             "the fact metric stays. Republish afterwards.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--column", required=True, help="the column, or the fact metric or attribute that reads it")
    sp.add_argument("--description", default="")
    sp.add_argument("--display-format", choices=["text", "number", "date"], default="number")
    sp.add_argument("--data-type", help="text | integer | bigint | double | date | timestamp (default: the column's)")
    sp.add_argument("--table", help="table name (default: the table with the most columns)")
    sp.add_argument("--dry-run", action="store_true", help="resolve the column and print the request; no changeset")
    sp.set_defaults(update=False, unverified_ok=False, formula="")

    sp = sub.add_parser("create-formula-attribute",
        help="Attribute computed from the table's columns (e.g. Year([FIRST DATE])). Prefer a lookup table: "
             "faster, and replaceable without deleting the metrics built on it. Republish afterwards.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--formula", required=True, help="Strategy syntax, columns in [brackets]")
    sp.add_argument("--description", default="")
    sp.add_argument("--display-format", choices=["text", "number", "date"])
    sp.add_argument("--data-type", help="text | integer | bigint | double | date | timestamp (Year() of a date "
                                        "comes back as a date unless you set integer)")
    sp.add_argument("--table", help="table name (default: the table with the most columns)")
    sp.add_argument("--update", action="store_true", help="rewrite the formula of an existing single-form attribute")
    sp.add_argument("--unverified-ok", action="store_true",
                    help="send a formula with string literals or decimals (not yet confirmed live)")
    sp.add_argument("--dry-run", action="store_true",
                    help="check the columns against the model and print the request; open no changeset. The "
                         "tenant checks the functions only when the step is applied")

    sp = sub.add_parser("set-column-types",
        help="Change column types in the model without reloading the source (table + pipeline, then the "
             "attribute forms and fact metrics that read them); republish afterwards.")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--set", action="append", nargs="+", metavar="VALUE",
                    help="COLUMN TYPE [DATE_FORMAT]; TYPE is one of " + ", ".join(COLUMN_TYPES)
                         + "; date and timestamp need the source pattern, e.g. yyyyMM. Repeatable.")
    sp.add_argument("--spec", help="CSV (column_name, target_type, date_format, enabled) or JSON "
                                   "{columns: [{column, type, date_format}]}")
    sp.add_argument("--table", help="table name (default: the table with the most columns)")
    sp.add_argument("--no-joined", action="store_true",
                    help="don't retype the matching key column of lookup/mapping tables joined through the same "
                         "attribute (by default they take the same type, or the join breaks)")
    sp.add_argument("--dry-run", action="store_true", help="print the changes; open no changeset")

    sp = sub.add_parser("patch-fact-metrics",
        help="Set aggregation function and/or number format on many fact metrics in one changeset, "
             "then verify by read-back (see memory/reference_mosaic_fact_metric_aggregation.md).")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--spec", required=True,
                    help="JSON/YAML: {presets?: {name: [format tokens]}, metrics: [{name, function?, format?}]}")
    sp.add_argument("--dry-run", action="store_true", help="print the diff plan; open no changeset")
    sp.add_argument("--release-locks", action="store_true",
                    help="if the changeset open hits 8004cc41 and GET /api/model/schema/lock says you own "
                         "the lock, release it and retry once (only when no other job uses this account)")

    sp = sub.add_parser("attach-transformation",
                        help="UNVERIFIED against the 2026 spec; see create-transformation")
    sp.add_argument("--model-id", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--source-metric", required=True)
    sp.add_argument("--transformation", required=True)
    sp.add_argument("--unverified-ok", action="store_true", help="send the unverified request anyway")

    return p


_SECRET_FLAGS = ("--password", "--auth-token", "--identity-token", "--session-cookie", "--ingress-cookie")


def main():
    args = build_parser().parse_args()
    if any(a.split("=", 1)[0] in _SECRET_FLAGS for a in sys.argv[1:]):
        print("[auth] WARN: secrets passed as command-line flags are visible to other processes (ps) and "
              "shell history; use the MSTR_* environment variables or --auth-method sso instead.",
              file=sys.stderr)
    m = MSTR(args)
    t0 = time.monotonic()
    try:
        {
            "auth-probe":      cmd_auth_probe,
            "list-datasources":cmd_list_datasources,
            "list-namespaces": cmd_list_namespaces,
            "list-tables":     cmd_list_tables,
            "describe-table":  cmd_describe_table,
            "describe-tables": cmd_describe_tables,
            "kill-sessions":   cmd_kill_sessions,
            "release-locks":   cmd_release_locks,
            "discover":        cmd_discover,
            "openapi-summary": cmd_openapi_summary,
            "openapi-search":  cmd_openapi_search,
            "api-call":        cmd_api_call,
            "resolve-users":   cmd_resolve_users,
            "create-users":    cmd_create_users,
            "search-objects":  cmd_search_objects,
            "get-model-object":cmd_get_model_object,
            "patch-model-object":cmd_patch_model_object,
            "build":           cmd_build,
            "build-from-config":cmd_build_from_config,
            "build-from-schema-objects": cmd_build_from_schema_objects,
            "validate-model":  cmd_validate_model,
            "validate-topology": cmd_validate_topology,
            "set-serve-mode":  cmd_set_serve_mode,
            "publish":         cmd_publish,
            "wire-relationships": cmd_wire_relationships,
            "merge-attributes":   cmd_merge_attributes,
            "refresh":         cmd_refresh,
            "delete-model":    cmd_delete_model,
            "set-acl":         cmd_set_acl,
            "add-security-filter": cmd_add_security_filter,
            "translate":       cmd_translate,
            "certify":         cmd_certify,
            "create-transformation": cmd_create_transformation,
            "create-compound-metric": cmd_create_compound_metric,
            "create-conditional-metric": cmd_create_conditional_metric,
            "delete-derived-metric": cmd_delete_derived_metric,
            "create-level-metric": cmd_create_level_metric,
            "create-weighted-average": cmd_create_weighted_average,
            "rename-describe": cmd_rename_describe,
            "create-formula-attribute": cmd_create_formula_attribute,
            "add-column-attribute": cmd_create_formula_attribute,
            "add-time-attributes": cmd_add_time_attributes,
            "show-forms": cmd_show_forms,
            "export-model": cmd_export_model,
            "set-column-types": cmd_set_column_types,
            "patch-fact-metrics": cmd_patch_fact_metrics,
            "attach-transformation": cmd_attach_transformation,
        }[args.cmd](m, args)
    except requests.HTTPError as e:
        die(f"{e.response.status_code} {e.response.text[:500]}")
    finally:
        # die() raises SystemExit, which `except Exception` cleanup blocks don't catch:
        # discard whatever changeset is still open so no schema lock outlives the run.
        for cs in list(m.open_changesets):
            print(f"[cleanup] discarding open changeset {cs}", file=sys.stderr)
            discard_cs(m, cs)
        m.logout()
        if args.verbose:
            print(f"[wall] {int((time.monotonic()-t0)*1000)}ms", file=sys.stderr)


if __name__ == "__main__":
    main()
