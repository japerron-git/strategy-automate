"""mosaic_safety.py — defensive helpers shared across Strategy automation scripts.

Stateless utilities only. No requests.Session, no MSTR class, no I/O — these
functions accept already-fetched response bodies (or plain dicts) and return
parsed data or freshly-constructed payloads. Callers own the network session.

Why a separate module: the issues these helpers address surfaced from
hands-on TPC-DS Galaxy builds (see strategy_automate_improvements report). The
fixes are defensive in nature — pre-flight checks, error-code parsing,
expression-format normalization, role-playing-dimension detection — that
any wiring or build script can call without buying into the build_mosaic.py
MSTR class.

Coverage:
  parse_mstr_error / format_mstr_error / is_session_cap_error  — item 4, 7
  make_expression / normalize_expressions                       — item 5
  attribute_lookup_table_map / attribute_table_name_map         — item 8
  detect_role_playing_secondaries                               — item 10

The corresponding network-bound helpers (relationship merge-PUT, join-table
preflight, topology validation) live in build_mosaic.py because they need a
live session; this module is what they call to do the actual computation.

Import pattern:
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mosaic_safety as ms
"""
from __future__ import annotations

import json
from typing import Any, Iterable


# ── Error parsing ────────────────────────────────────────────────────────────

# Strategy iServer session cap. The only REST logout is POST /api/auth/logout (DELETE
# /api/auth/login is a 404, which is how older scripts leaked sessions); orphans age out after
# ~30 minutes idle. See feedback_build_mosaic_session_leak.md.
SESSION_CAP_CODE = "8004cb0a"
SESSION_CAP_ISERVER = -2147072486
SESSION_CAP_MESSAGE = (
    "Strategy iServer interactive-session cap reached "
    f"(code={SESSION_CAP_CODE}, iServerCode={SESSION_CAP_ISERVER}). "
    "Orphaned sessions age out after ~30 min idle; end them now with "
    "`build_mosaic.py kill-sessions --idle-minutes 10 --yes`, or reuse one session "
    "(MSTR_REUSE_SESSION=1), then retry."
)

# Join-table membership rule (8004ccc7): the relationship's join table must
# carry an expression for BOTH the parent and the child attribute. Kimball:
# the fact row physically connects both conformed dims. See
# feedback_mosaic_relationship_wiring.md.
JOIN_TABLE_RULE_DOC = (
    "Strategy 8004ccc7: relationship_table must contain an expression for "
    "BOTH the parent and child attributes. For a galaxy/star schema, that "
    "means the FACT table (not a dim table). If only one endpoint has an "
    "expression on the join table, add the missing expression via PATCH "
    "BEFORE issuing the relationship PUT."
)


def parse_mstr_error(value: Any) -> dict[str, Any]:
    """Parse a Strategy REST error body into a structured dict.

    Accepts a `requests.Response`-like object (anything with `.text`/`.status_code`),
    a JSON-encoded string, a pre-parsed dict, or None. Always returns a dict with
    keys: code, iServerCode, message, status, raw.

    Strategy error bodies look like:
        {"code": "8004ccc7",
         "iServerCode": -2147072486,
         "message": "Table cannot be used as the join table…",
         "ticketId": "..."}

    Missing fields come back as None or "". Never raises.
    """
    result: dict[str, Any] = {
        "code": None, "iServerCode": None, "message": "",
        "status": None, "raw": None,
    }
    if value is None:
        return result

    # Response-like object — pull status + text.
    text = None
    if hasattr(value, "text") and hasattr(value, "status_code"):
        result["status"] = getattr(value, "status_code", None)
        text = getattr(value, "text", "") or ""
        result["raw"] = text[:600]
    elif isinstance(value, dict):
        body = value
        result["raw"] = value
        return _fill_error_fields(result, body)
    elif isinstance(value, (bytes, bytearray)):
        try:
            text = value.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        result["raw"] = text[:600]
    elif isinstance(value, str):
        text = value
        result["raw"] = value[:600]
    else:
        result["raw"] = repr(value)[:600]

    if text:
        try:
            body = json.loads(text)
        except Exception:
            result["message"] = text[:400]
            return result
        if isinstance(body, dict):
            _fill_error_fields(result, body)
    headers = getattr(value, "headers", None) or {}
    if not (result["code"] or result["message"]) and hasattr(headers, "get"):   # errors sent as headers only
        result["code"] = headers.get("X-MSTR-ErrorCode") or None
        result["message"] = (headers.get("X-MSTR-ErrorMessage") or "")[:400]
    return result


def _fill_error_fields(result: dict, body: dict) -> dict:
    result["code"] = body.get("code") or result.get("code")
    iserver = body.get("iServerCode")
    if iserver is None:
        iserver = body.get("iserverCode")
    if isinstance(iserver, str):
        try:
            iserver = int(iserver)
        except ValueError:
            pass
    result["iServerCode"] = iserver if iserver not in (None, "") else result.get("iServerCode")
    msg = body.get("message") or body.get("error") or body.get("detail") or body.get("title")
    if not isinstance(msg, str):
        msg = None
    nested = body.get("errors") or body.get("details")   # some services nest their errors
    if isinstance(nested, list) and nested and isinstance(nested[0], dict):
        first = nested[0]
        result["code"] = result["code"] or first.get("code")
        inner = "; ".join(str(e.get("message") or e.get("detail") or "") for e in nested[:3] if isinstance(e, dict))
        msg = msg or inner or None
    if not msg and not result["code"] and body:
        msg = json.dumps(body, separators=(",", ":"))[:400]   # unknown shape: show it rather than lose it
    result["message"] = msg or result.get("message") or ""
    return result


def format_mstr_error(value: Any, prefix: str = "") -> str:
    """One-line, grep-friendly error string. Surfaces the Strategy `code` so
    operators can match the error-code index in
    memory/reference_strategy_error_codes.md without manually parsing JSON."""
    info = parse_mstr_error(value)
    pieces = []
    if prefix:
        pieces.append(prefix.rstrip(": ") + ":")
    if info["status"] is not None:
        pieces.append(f"HTTP {info['status']}")
    if info["code"]:
        pieces.append(f"[{info['code']}]")
    if info["iServerCode"] is not None:
        pieces.append(f"(iServerCode={info['iServerCode']})")
    msg = (info["message"] or "").strip()
    if msg:
        pieces.append(msg if len(msg) <= 300 else msg[:297] + "...")
    return " ".join(pieces) if pieces else "<no error info>"


def is_session_cap_error(value: Any) -> bool:
    """Detect Strategy iServer session-cap (8004cb0a / -2147072486). Accepts
    a response object, a parsed dict, an already-parsed parse_mstr_error()
    output, or a raw body string."""
    info = parse_mstr_error(value)
    if info["code"] and str(info["code"]).lower() == SESSION_CAP_CODE.lower():
        return True
    if info["iServerCode"] == SESSION_CAP_ISERVER:
        return True
    return False


# ── Expression helpers ───────────────────────────────────────────────────────

def make_expression(
    column_name: str,
    table_id: str | None = None,
    *,
    table_name: str = "",
    dtype: dict | str | None = None,
) -> dict:
    """Return a canonical Modeling-Service `expression` in the tokens format.

    Use this any time a script needs to PATCH an attribute form expression or
    POST a fact expression. The Strategy API will accept a `tree` shape on read
    (via `?showExpressionAs=tree`) and produces a read-only `text` field on
    plain GET, but writes require either `tree` or `tokens`. This helper
    produces the simpler tokens form so round-trips through GET → write don't
    trip 8004ccde ("The tree or token is required for expression").

    Args:
        column_name: warehouse column name (case-sensitive — Mosaic is strict).
        table_id: optional logical-table objectId to bind the expression to.
        table_name: optional warehouse table name for the tables[] hint.
        dtype: optional column datatype dict {type, precision, scale} or string.

    Returns:
        {"expression": {"tokens": [...], "text": "..."},
         "tables": [{...}]?}  — the outer container expected by
        forms[*].expressions[*].
    """
    if not column_name:
        raise ValueError("make_expression: column_name is required")

    tokens = [{"type": "column_reference", "value": column_name}]
    container: dict[str, Any] = {
        "expression": {"tokens": tokens, "text": column_name},
    }
    if table_id or table_name:
        tbl: dict[str, Any] = {"subType": "logical_table"}
        if table_id:
            tbl["objectId"] = table_id
        if table_name:
            tbl["name"] = table_name
        container["tables"] = [tbl]
    if dtype is not None:
        if isinstance(dtype, str):
            dtype = {"type": dtype}
        container["columns"] = [{"columnName": column_name, "dataType": dtype}]
    return container


def normalize_expressions(attr_json: dict) -> dict:
    """Deep-copy an attribute GET response and convert read-only `text`-only
    expressions back into a writable `tokens` form.

    Strategy returns `expression: {"text": "[CUST_ID]"}` on plain GET. PATCHing
    that body unchanged returns 8004ccde — writes need `tokens` (or `tree`).
    This helper walks every form expression and, when only `text` is present,
    fabricates a single `column_reference` token. Anything that already has
    `tokens` or `tree` is left alone.

    Returns a new dict; the input is not mutated.
    """
    if not isinstance(attr_json, dict):
        return attr_json
    copy = json.loads(json.dumps(attr_json))
    for form in (copy.get("forms") or []):
        for expr_block in (form.get("expressions") or []):
            inner = expr_block.get("expression")
            if not isinstance(inner, dict):
                continue
            if inner.get("tokens") or inner.get("tree"):
                continue
            text = inner.get("text") or ""
            stripped = text.strip().strip("[]")
            if stripped:
                inner["tokens"] = [{"type": "column_reference", "value": stripped}]
                inner["text"] = inner.get("text") or stripped
    return copy


# ── attributeLookupTable bulk-response utilities ─────────────────────────────

def attribute_lookup_table_map(attrs: Iterable[dict]) -> dict[str, str]:
    """Map attribute-objectId → its `attributeLookupTable.objectId`.

    The bulk `GET /api/model/dataModels/{id}/attributes?limit=1000` response
    includes `attributeLookupTable` for every attribute — a direct pointer to
    the dim table that owns it. Use this instead of N individual attribute
    fetches when grouping by owning table for Level-Dim relationships.

    Attributes without a lookup table (e.g., conformed cross-table attrs) are
    omitted from the result map rather than mapped to None.
    """
    out: dict[str, str] = {}
    for a in attrs or []:
        if not isinstance(a, dict):
            continue
        info = a.get("information") or {}
        aid = info.get("objectId") or a.get("id") or a.get("objectId")
        lookup = a.get("attributeLookupTable") or {}
        lid = lookup.get("objectId")
        if aid and lid:
            out[aid] = lid
    return out


def attribute_table_name_map(attrs: Iterable[dict]) -> dict[str, str]:
    """Like attribute_lookup_table_map but emits the lookup table NAME instead
    of its objectId — handy for printing audit reports without a second join."""
    out: dict[str, str] = {}
    for a in attrs or []:
        if not isinstance(a, dict):
            continue
        info = a.get("information") or {}
        aid = info.get("objectId") or a.get("id") or a.get("objectId")
        lookup = a.get("attributeLookupTable") or {}
        lname = lookup.get("name")
        if aid and lname:
            out[aid] = lname
    return out


# ── Role-playing dimensions ──────────────────────────────────────────────────

ROLE_PLAYING_DOC = """\
Role-playing dimension pattern (TPC-DS-style schemas):

A single fact table often has multiple FK columns that point to the same
dimension — e.g. WEB_SALES has WS_SOLD_DATE_SK and WS_SHIP_DATE_SK, both
referencing DATE_DIM. Each FK column plays a different role.

In Strategy/Mosaic, the canonical handling is:
  - Create ONE base attribute for the dim (e.g. "Date").
  - Add a per-role qualifier as either:
    a) a separate alias attribute that shares the same lookup table but
       binds to the role-specific FK column (e.g. "Sold Date", "Ship Date"),
       OR
    b) a single attribute with form expressions on each FK column, with the
       role distinguished by the join table.

Most relationship-wiring scripts trip on this pattern by silently picking the
"first occurrence per (parent, table) pair wins" rule. The result is that
only the primary role gets a Level-A relationship, and the rest become
isolated Level-B attributes.

The detect_role_playing_secondaries() helper makes the pattern explicit so
scripts can log which roles they're skipping (or, better, build alias
attributes for them) rather than silently dropping data.
"""


def detect_role_playing_secondaries(
    relationships: Iterable[dict],
    *,
    parent_key: str = "parent_attribute",
    table_key: str = "relationship_table",
    child_key: str = "child_attribute",
) -> tuple[list[dict], list[dict]]:
    """Split a list of relationship hints into (primaries, secondaries).

    "Role-playing secondary" = a hint whose (parent_attribute, relationship_table)
    pair has already been claimed by an earlier hint. Returned in the order
    they appeared in the input; ties are broken by first-seen-wins.

    Hint shape (one row per relationship):
        {"parent_attribute": "...",
         "child_attribute":  "...",
         "relationship_table": "...",
         "type": "one_to_many"}

    Returns:
        (primaries, secondaries) — both are plain lists of the original dicts,
        unmodified. The caller decides what to do with secondaries:
        warn-and-skip, alias-attribute, or full role split.
    """
    seen: set[tuple[str, str]] = set()
    primaries: list[dict] = []
    secondaries: list[dict] = []
    for rel in relationships or []:
        if not isinstance(rel, dict):
            continue
        parent = str(rel.get(parent_key) or "").strip()
        table = str(rel.get(table_key) or "").strip()
        if not parent or not table:
            primaries.append(rel)
            continue
        key = (parent.lower(), table.lower())
        if key in seen:
            secondaries.append(rel)
        else:
            seen.add(key)
            primaries.append(rel)
    return primaries, secondaries
