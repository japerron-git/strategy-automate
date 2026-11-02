#!/usr/bin/env python3
"""Assess a Mosaic model with business questions: scope it, run a question bank through the
Mosaic MCP connector, grade the answers, and turn approved findings into an ordered change plan.

  assess_model.py scope --project P --model M --out scope.json        # inventory + bounded profiling
  assess_model.py relationships --project P --scope scope.json --out relationships.json
  assess_model.py lookups --project P --scope scope.json --csv-dir lookups/ --out lookups.json
  assess_model.py run   --project P --bank question_bank.json --out results.json
  assess_model.py grade --bank question_bank.json --results results.json --out graded.json
  assess_model.py plan  --findings findings.json --model-id <id> --out change_plan.json
  assess_model.py check --bank question_bank.json --findings findings.json --domain domain_notes.json

scope and run sign in to the tenant's Mosaic MCP server the way the MCP connector does
(skills/strategy-platform/scripts/strategy_mcp.py: browser single sign-on, tokens in the OS secret
store). Where the script cannot sign in (a cloud session with the Mosaic connector attached), add
--replies <folder>: the agent calls the connector tools itself, saves each reply in the folder, and
the script reads them (missing replies are listed in <folder>/pending.json). grade, plan and check
work offline on the JSON files. File formats, levels and phases:
memory/reference_model_assessment.md. Workflow: skills/strategy-model-assessment/SKILL.md.

Output files hold tenant data (rows, names); they are written owner-only and belong outside the
repo. Nothing here writes to the model: plan only prints build_mosaic.py commands for review.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shlex
import sys
import time
from typing import Any, Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILLS = os.path.normpath(os.path.join(_HERE, os.pardir, os.pardir))
sys.path.insert(0, os.path.join(_SKILLS, "build-mosaic-model", "scripts"))
sys.path.insert(0, os.path.join(_SKILLS, "strategy-platform", "scripts"))
import strategy_validate_models as svm  # noqa: E402  (comparison engine, model-name injection)

BUILD_SCRIPT = "skills/build-mosaic-model/scripts/build_mosaic.py"
ASSESS_SCRIPT = "skills/strategy-model-assessment/scripts/assess_model.py"

LEVELS = (1, 2, 3, 4, 5)
DEFAULT_MIX = {"1": 20, "2": 25, "3": 25, "4": 20, "5": 10}
PATHS = ("governed_metric", "simple_sql", "expert_sql", "not_answerable")
EXPECTED_SOURCES = ("inline", "reference_file", "same_model_sql", "none")
STRENGTHS = ("reference", "recomputed", "same_model")
IMPACTS = ("high", "medium", "low")
EFFORTS = ("small", "medium", "large")
STATUSES = ("proposed", "approved", "modified", "declined", "applied", "verified", "failed")
PLANNED_STATUSES = ("approved", "modified")
VERIFIED_VALUES = ("yes", "no", "contradicted", "not_applicable")
# Source kinds, strongest first for "how this organization defines it": the user's own documents.
SOURCE_TYPES = ("user_document", "provider", "regulator", "standard", "industry", "other")
PRIORITIES = ("high", "medium", "low")

# Change phases (memory/reference_model_assessment.md "Change phases") and the default phase per category.
PHASES = {1: "Source data and tables", 2: "Types and lookups", 3: "Attributes and relationships",
          4: "Fixes to existing objects", 5: "New objects", 6: "Names, descriptions, formats",
          7: "Publish, certify, permissions"}
CATEGORY_PHASE = {"source_data": 1, "table": 1, "data_type": 2, "lookup": 2, "attribute": 3,
                  "relationship": 3, "metric_defect": 4, "missing_metric": 5, "description": 6,
                  "format": 6, "discoverability": 6, "governance": 7, "performance": 7}
REPUBLISH_PHASES = (1, 2, 3)
ACTION_KINDS = ("create_conditional_metric", "create_compound_metric", "delete_derived_metric",
                "patch_model_object", "patch_fact_metrics", "add_lookup_table", "source_change", "publish", "certify",
                "manual")


# ── small helpers ─────────────────────────────────────────────────────────────────────────────

def _load_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as e:
        raise SystemExit(f"{path}: {e}") from e
    except ValueError as e:
        raise SystemExit(f"{path}: not valid JSON ({e})") from e


def _write_json(path: str, obj: Any) -> None:
    """Owner-only file (the content is tenant data)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_model(name: str) -> str:
    return svm.inject_model("{{MODEL}}", name)


# ── parsing what the Mosaic MCP tools return ─────────────────────────────────────────────────

_NULLS = {"", "NULL", "null"}


def _split_md_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    cells, cur, i = [], [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body) and body[i + 1] == "|":
            cur.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    cells.append("".join(cur).strip())
    return cells


def parse_markdown_table(text: str) -> tuple[list[str], list[dict[str, Any]]]:
    """The first markdown table in text → (columns, rows). Empty cells and NULL become None."""
    lines = [ln for ln in text.splitlines()]
    start = next((i for i, ln in enumerate(lines) if ln.strip().startswith("|")), None)
    if start is None:
        return [], []
    columns = _split_md_row(lines[start])
    rows: list[dict[str, Any]] = []
    rest = lines[start + 1:]
    for k, ln in enumerate(rest):
        if not ln.strip():
            nxt = next((x for x in rest[k + 1:] if x.strip()), "")
            if nxt.strip().startswith("|"):
                continue   # the connector can split one table across several text blocks
            break
        if not ln.strip().startswith("|"):
            break
        cells = _split_md_row(ln)
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c):
            continue
        cells += [""] * (len(columns) - len(cells))
        rows.append({c: (None if v in _NULLS else v) for c, v in zip(columns, cells)})
    return columns, rows


def _rows_from_json(payload: Any) -> tuple[list[str], list[dict[str, Any]]] | None:
    if isinstance(payload, dict):
        for key in ("rows", "data", "items", "results", "result"):
            if key in payload:
                inner = payload[key]
                cols = payload.get("columns")
                if isinstance(inner, list) and inner and isinstance(inner[0], list) and isinstance(cols, list):
                    names = [c.get("name") if isinstance(c, dict) else str(c) for c in cols]
                    return names, [dict(zip(names, r)) for r in inner]
                return _rows_from_json(inner)
        return None
    if isinstance(payload, list) and all(isinstance(r, dict) for r in payload):
        cols: list[str] = []
        for r in payload:
            cols += [k for k in r if k not in cols]
        return cols, list(payload)
    return None


def tool_text(result: dict) -> str:
    if result.get("structuredContent") is not None:
        return json.dumps(result["structuredContent"])
    return "\n".join(c.get("text", "") for c in result.get("content", []) or [] if c.get("type") == "text")


def parse_query_result(result: dict) -> dict[str, Any]:
    """MCP `query` tool result → {"ok", "columns", "rows", "error", "note"}. The tool answers with a
    markdown table plus a row-count line; structured JSON is accepted too."""
    text = tool_text(result)
    if result.get("isError"):
        return {"ok": False, "columns": [], "rows": [], "error": text.strip()[:2000]}
    if result.get("structuredContent") is not None:
        parsed = _rows_from_json(result["structuredContent"])
        if parsed is not None:
            return {"ok": True, "columns": parsed[0], "rows": parsed[1], "error": None}
    stripped = text.strip()
    if stripped[:1] in "[{":
        try:
            parsed = _rows_from_json(json.loads(stripped))
        except ValueError:
            parsed = None
        if parsed is not None:
            return {"ok": True, "columns": parsed[0], "rows": parsed[1], "error": None}
    columns, rows = parse_markdown_table(text)
    if columns:
        return {"ok": True, "columns": columns, "rows": rows, "error": None}
    if re.search(r"returned 0 rows|no rows", text, re.I):
        return {"ok": True, "columns": [], "rows": [], "error": None}
    return {"ok": False, "columns": [], "rows": [], "error": f"no result table in reply: {stripped[:300]}"}


_SEM_LINE = re.compile(r"^- \*\*(?P<name>.+?)\*\* \((?P<type>[^)]*)\)(?::\s*(?P<desc>.*))?$")


def parse_semantics(text: str) -> dict[str, list[dict[str, Any]]]:
    """MCP `get_semantics` markdown → {"attributes": [...], "metrics": [...]}; a metric's formula is
    the line that follows its entry."""
    out: dict[str, list[dict[str, Any]]] = {"attributes": [], "metrics": []}
    section = None
    last: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        low = line.lower()
        if low.startswith("### "):
            section = "attributes" if "attribute" in low or "dimension" in low else (
                "metrics" if "metric" in low or "measure" in low else None)
            last = None
            continue
        m = _SEM_LINE.match(line.strip())
        if m and section:
            last = {"name": m.group("name"), "type": m.group("type"), "description": (m.group("desc") or "").strip()}
            out[section].append(last)
            continue
        if section == "metrics" and last is not None and line.strip() and "formula" not in last \
                and not line.lstrip().startswith(("#", "- **")):
            last["formula"] = line.strip()
    return out


# ── MCP access ───────────────────────────────────────────────────────────────────────────────

class McpRunner:
    """Calls the tenant's Mosaic MCP server through strategy_mcp.py (same sign-on as the connector)."""

    def __init__(self, base: str = "", mcp_url: str = "", server: str = "mosaic"):
        import strategy_auth as sa
        import strategy_mcp as sm
        self._sm = sm
        http = sa.SafeSession()
        cfg = sa.AuthConfig.from_env(base=base)
        meta = sm.discover(http, mcp_url, cfg.base, server)
        self.client = sm.McpClient(http, meta, cfg)
        self.client.start()

    def call(self, tool: str, arguments: dict) -> dict:
        return self.client.call(tool, arguments)


Runner = Any   # anything with call(tool, arguments) -> MCP tools/call result


class PendingReply(Exception):
    """Replay mode: the reply to this call has not been saved yet."""


def query_id(sql: str) -> str:
    """Stable file name stem for a query's saved reply."""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:12]


class ReplayRunner:
    """Answers MCP calls from replies an agent saved after calling the connector tools itself — for
    sessions where this script cannot sign in (a cloud workspace with the Mosaic connector attached).

    Folder layout: `semantics.txt` holds the get_semantics reply; `<query id>.txt` holds each query
    reply, as the tool returned it (start the file with `ERROR:` when the tool reported an error).
    Calls with no saved reply are collected in `pending` and written to `pending.json`; save those
    replies and run the same command again until nothing is pending."""

    def __init__(self, folder: str):
        self.folder = folder
        self.pending: dict[str, dict[str, Any]] = {}

    def _read(self, name: str) -> dict | None:
        path = os.path.join(self.folder, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            text = f.read()
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except ValueError:
                payload = None
            if isinstance(payload, dict) and ({"content", "structuredContent", "isError"} & set(payload)):
                return payload
        if stripped.upper().startswith("ERROR:"):
            return {"isError": True, "content": [{"type": "text", "text": stripped[6:].strip()}]}
        return {"content": [{"type": "text", "text": text}]}

    def call(self, tool: str, arguments: dict) -> dict:
        key = "semantics" if tool == "get_semantics" else query_id(str(arguments.get("query", "")))
        name = f"{key}.txt"
        reply = self._read(name)
        if reply is None:
            self.pending[key] = {"id": key, "file": name, "tool": tool, "arguments": arguments}
            raise PendingReply(key)
        return reply

    def write_pending(self) -> str:
        path = os.path.join(self.folder, "pending.json")
        _write_json(path, {"folder": self.folder, "count": len(self.pending),
                           "instructions": "Call each tool with its arguments; save the reply text to "
                                           "<folder>/<file> (start it with ERROR: if the tool failed); "
                                           "then run the same command again.",
                           "calls": list(self.pending.values())})
        return path


# ── scope ────────────────────────────────────────────────────────────────────────────────────

def profile_sql(model: str, attribute: str, where: str = "") -> str:
    col = quote_ident(attribute)
    clause = f" WHERE {where}" if where else ""
    return (f"SELECT COUNT(DISTINCT {col}) AS distinct_values, MIN({col}) AS min_value, "  # nosec B608  # identifiers are quoted; the connector is read-only
            f"MAX({col}) AS max_value FROM {quote_model(model)}{clause}")


def sample_sql(model: str, attribute: str, limit: int, where: str = "") -> str:
    """Distinct values as text: the CAST keeps the connector from formatting numbers (202,003)."""
    col = quote_ident(attribute)
    clause = f" WHERE {where}" if where else ""
    return (f"SELECT DISTINCT CAST({col} AS VARCHAR) AS value FROM {quote_model(model)}{clause} "  # nosec B608  # identifiers are quoted; the connector is read-only
            f"ORDER BY 1 LIMIT {int(limit)}")


def sample_batch_sql(model: str, attributes: list[str], limit: int, where: str = "") -> str:
    clause = f" WHERE {where}" if where else ""
    parts = [f"SELECT DISTINCT '{a.replace(chr(39), chr(39) * 2)}' AS attribute, "  # nosec B608  # literal escaped, identifiers quoted
             f"CAST({quote_ident(a)} AS VARCHAR) AS value FROM {quote_model(model)}{clause}" for a in attributes]
    return " UNION ALL ".join(parts) + f" ORDER BY 1, 2 LIMIT {int(limit)}"  # nosec B608  # identifiers are quoted; the connector is read-only


def _sample_values(runner: Runner, project: str, model: str, wanted: dict[str, float], where: str,
                   batch: int) -> dict[str, Any]:
    """name → list of values | "pending" | None (failed). Batches attributes with UNION ALL; a failed
    batch falls back to one query per attribute."""
    out: dict[str, Any] = {}
    names = list(wanted)
    for start in range(0, len(names), max(batch, 1)):
        chunk = names[start:start + max(batch, 1)]
        if len(chunk) > 1:
            limit = int(sum(wanted[n] for n in chunk)) + len(chunk) + 10
            try:
                parsed = parse_query_result(runner.call("query", {"project": project,
                                                                  "query": sample_batch_sql(model, chunk, limit, where)}))
            except PendingReply:
                out.update({n: "pending" for n in chunk})
                continue
            if parsed["ok"]:
                for n in chunk:
                    out[n] = [r.get("value") for r in parsed["rows"] if r.get("attribute") == n]
                continue
        for n in chunk:
            try:
                parsed = parse_query_result(runner.call("query", {"project": project,
                                                                  "query": sample_sql(model, n, int(wanted[n]) + 1,
                                                                                      where)}))
            except PendingReply:
                out[n] = "pending"
                continue
            out[n] = [r.get("value") for r in parsed["rows"]] if parsed["ok"] else None
    return out


def profile_batch_sql(model: str, attributes: list[str], where: str = "") -> str:
    cols = []
    for i, a in enumerate(attributes):
        c = quote_ident(a)
        cols += [f"COUNT(DISTINCT {c}) AS d{i}", f"MIN({c}) AS n{i}", f"MAX({c}) AS x{i}"]
    clause = f" WHERE {where}" if where else ""
    return f"SELECT {', '.join(cols)} FROM {quote_model(model)}{clause}"  # nosec B608  # identifiers are quoted; the connector is read-only


def _profile_rows(runner: Runner, project: str, model: str, names: list[str], where: str,
                  batch: int) -> dict[str, dict[str, Any]]:
    """name → {"distinct_values", "min", "max"} | {"error"} | {"pending"}. Batches several attributes
    per query; a failed batch falls back to one query per attribute."""
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(names), max(batch, 1)):
        chunk = names[start:start + max(batch, 1)]
        if len(chunk) > 1:
            try:
                parsed = parse_query_result(runner.call("query", {"project": project,
                                                                  "query": profile_batch_sql(model, chunk, where)}))
            except PendingReply:
                out.update({n: {"pending": True} for n in chunk})
                continue
            if parsed["ok"] and parsed["rows"]:
                row = parsed["rows"][0]
                for i, n in enumerate(chunk):
                    out[n] = {"distinct_values": svm.number(row.get(f"d{i}")), "min": row.get(f"n{i}"),
                              "max": row.get(f"x{i}")}
                continue
        for n in chunk:
            try:
                parsed = parse_query_result(runner.call("query", {"project": project,
                                                                  "query": profile_sql(model, n, where)}))
            except PendingReply:
                out[n] = {"pending": True}
                continue
            if not parsed["ok"] or not parsed["rows"]:
                out[n] = {"error": parsed["error"] or "no rows"}
                continue
            row = parsed["rows"][0]
            out[n] = {"distinct_values": svm.number(row.get("distinct_values")), "min": row.get("min_value"),
                      "max": row.get("max_value")}
    return out


def scope_model(runner: Runner, project: str, model: str, *, profile: bool = True, where: str = "",
                sample_max: int = 30, max_attributes: int = 200, profile_batch: int = 8,
                log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    sem_result = runner.call("get_semantics", {"project": project, "model": model})
    if sem_result.get("isError"):
        raise SystemExit(f"get_semantics: {tool_text(sem_result)[:300]}")
    semantics = parse_semantics(tool_text(sem_result))
    if not semantics["attributes"] and not semantics["metrics"]:
        raise SystemExit("get_semantics returned no attributes or metrics (is the model certified?)")
    out: dict[str, Any] = {"version": 1, "project": project, "model": model,
                           "generated": time.strftime("%Y-%m-%d"), "profile_where": where or None,
                           "attributes": semantics["attributes"], "metrics": semantics["metrics"],
                           "notes": {"undescribed_attributes": [a["name"] for a in semantics["attributes"]
                                                                if not a["description"]],
                                     "undescribed_metrics": [m["name"] for m in semantics["metrics"]
                                                             if not m["description"]]}}
    if not profile:
        out["maturity"] = maturity_signals(out)
        return out
    todo = semantics["attributes"][:max_attributes]
    log(f"profile {len(todo)} attributes, {profile_batch} per query")
    profiles = _profile_rows(runner, project, model, [a["name"] for a in todo], where, profile_batch)
    wanted = {a["name"]: (profiles.get(a["name"]) or {}).get("distinct_values") for a in todo}
    wanted = {n: d for n, d in wanted.items() if d is not None and 0 < d <= sample_max}
    samples = _sample_values(runner, project, model, wanted, where, profile_batch)
    for attr in todo:
        prof = profiles.get(attr["name"]) or {"error": "not profiled"}
        if attr["name"] in samples:
            if samples[attr["name"]] == "pending":
                prof["values_pending"] = True
            else:
                prof["values"] = samples[attr["name"]]
        attr["profile"] = prof
    skipped = len(semantics["attributes"]) - max_attributes
    if skipped > 0:
        out["notes"]["unprofiled_attributes"] = skipped
    out["maturity"] = maturity_signals(out)
    return out


_RAW_METRIC = re.compile(r"^(sum|avg|min|max|count)\(\s*[^(){}]*\{[^{}]+\}\s*\)$", re.I)
_DEFAULT_NAME = re.compile(r"^(total|sum of|average|avg|count of|count)\b.*\(.*\)$|^[a-z0-9]+(_[a-z0-9]+)+$", re.I)
_ATTRIBUTE_LIKE = re.compile(r"\b(id|code|key|number|zip|postal|year|month|flag|indicator|status|type)\b", re.I)
_NUMERIC = ("bigint", "integer", "int", "double", "decimal", "real", "float", "smallint", "tinyint")
_SUMMED_NONADDITIVE = re.compile(r"\b(rate|ratio|score|percent|percentage|pct|age|months|term|price|index|"
                                 r"average|avg|mean|latitude|longitude)\b", re.I)


def _plain_number(value: Any) -> str:
    return str(value or "").replace(",", "").strip()


def looks_like_integer_date(attr: dict) -> bool:
    """A numeric attribute holding YYYYMM or YYYYMMDD values (from profile min / max or description)."""
    if attr.get("type", "").lower() not in _NUMERIC:
        return False
    if re.search(r"\bYYYY-?MM", attr.get("description") or ""):
        return True
    prof = attr.get("profile") or {}
    lo, hi = _plain_number(prof.get("min")), _plain_number(prof.get("max"))
    def ok(v):
        return (len(v) == 6 and v.isdigit() and 1900 <= int(v[:4]) <= 2199 and 1 <= int(v[4:]) <= 12) or \
               (len(v) == 8 and v.isdigit() and 1900 <= int(v[:4]) <= 2199 and 1 <= int(v[4:6]) <= 12)
    return bool(lo and hi and ok(lo) and ok(hi))


def maturity_signals(scope: dict) -> dict[str, Any]:
    """Signals from the scope and an estimated maturity level (memory/reference_model_assessment.md
    "Maturity levels"). An estimate to confirm during the assessment, not a grade."""
    attrs = scope.get("attributes") or []
    metrics = scope.get("metrics") or []
    names = [a["name"].lower() for a in attrs]
    described = sum(1 for o in attrs + metrics if (o.get("description") or "").strip())
    total = len(attrs) + len(metrics) or 1
    derived = [m["name"] for m in metrics if m.get("formula") and not _RAW_METRIC.match(m["formula"].strip())]
    default_named = [o["name"] for o in attrs + metrics if _DEFAULT_NAME.match(o["name"].strip())]
    attribute_like_metrics = [m["name"] for m in metrics if _ATTRIBUTE_LIKE.search(_words(m["name"]))
                              and m.get("formula") and _RAW_METRIC.match(m["formula"].strip())]
    numeric_attributes = [a["name"] for a in attrs if a.get("type", "").lower() in _NUMERIC]
    decoded = any(re.search(r"(__|\b)(description|desc|label)\b", n) for n in names)
    latest_flag = any(("latest" in n or "current period" in n or "is current" in n) for n in names)
    constants = [a["name"] for a in attrs if (a.get("profile") or {}).get("distinct_values") == 1]
    integer_dates = [a["name"] for a in attrs if looks_like_integer_date(a)]
    summed_nonadditive = [m["name"] for m in metrics if m.get("formula") and
                          re.match(r"^sum\(", m["formula"].strip(), re.I) and _SUMMED_NONADDITIVE.search(_words(m["name"]))]
    text_metrics = [m["name"] for m in metrics if m.get("type", "").lower() in ("varchar", "char", "string", "text")]
    share = round(described / total, 3)
    structural = len(summed_nonadditive) >= 3 or len(integer_dates) >= 2 or bool(text_metrics)
    if not derived:
        level = "M0" if share < 0.5 or structural else "M1"
    elif len(derived) < 5:
        level = "M2" if decoded else "M1"
    else:
        level = "M4" if (share >= 0.9 and latest_flag and decoded) else "M3"
    return {"estimated_level": level, "described_share": share, "derived_metrics": len(derived),
            "raw_metrics": len(metrics) - len(derived), "default_named_objects": default_named,
            "metrics_that_look_like_attributes": attribute_like_metrics,
            "numeric_attributes": numeric_attributes, "decoded_codes": decoded,
            "latest_period_flag": latest_flag, "single_value_attributes": constants,
            "dates_stored_as_numbers": integer_dates, "summed_rates_scores_or_ages": summed_nonadditive,
            "metrics_stored_as_text": text_metrics,
            "note": "estimate from names, descriptions and formulas; confirm during the assessment"}


# ── run ──────────────────────────────────────────────────────────────────────────────────────

def _select(questions: list[dict], only: str = "", levels: str = "") -> list[dict]:
    ids = {s.strip() for s in only.split(",") if s.strip()}
    lv = {int(s) for s in levels.split(",") if s.strip()}
    return [q for q in questions if (not ids or q.get("id") in ids) and (not lv or q.get("level") in lv)]


def _execute(runner: Runner, project: str, sql: str, max_rows: int) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        parsed = parse_query_result(runner.call("query", {"project": project, "query": sql}))
    except PendingReply:
        return {"ok": False, "pending": True, "error": "reply not saved yet", "columns": [], "rows": [],
                "row_count": 0, "truncated": False, "seconds": None}
    except Exception as e:  # network or MCP errors: record and keep going
        parsed = {"ok": False, "columns": [], "rows": [], "error": f"{type(e).__name__}: {e}"}
    rows = parsed["rows"]
    return {"ok": parsed["ok"], "error": parsed["error"], "columns": parsed["columns"],
            "rows": rows[:max_rows], "row_count": len(rows), "truncated": len(rows) > max_rows,
            "seconds": round(time.monotonic() - t0, 3)}


def run_bank(runner: Runner, project: str, bank: dict, *, only: str = "", levels: str = "",
             max_rows: int = 1000, log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    model = bank["model"]
    results = []
    questions = _select(bank.get("questions") or [], only, levels)
    for i, q in enumerate(questions):
        log(f"run {i + 1}/{len(questions)}: {q.get('id')}")
        entry: dict[str, Any] = {"id": q.get("id"), "level": q.get("level")}
        if not (q.get("sql") or "").strip():
            entry.update({"ok": False, "error": "no sql (expected_path not_answerable?)", "skipped": True})
            results.append(entry)
            continue
        entry["sql"] = svm.inject_model(q["sql"], model)
        entry.update(_execute(runner, project, entry["sql"], max_rows))
        exp = q.get("expected") or {}
        if exp.get("source") == "same_model_sql" and (exp.get("sql") or "").strip():
            ref_sql = svm.inject_model(exp["sql"], model)
            ref = _execute(runner, project, ref_sql, max_rows)
            entry["expected_run"] = {"sql": ref_sql, **ref}
        results.append(entry)
    ok = sum(1 for r in results if r.get("ok"))
    return {"version": 1, "project": project, "model": model, "generated": time.strftime("%Y-%m-%d %H:%M"),
            "summary": {"questions": len(results), "ok": ok, "errors": len(results) - ok},
            "results": results}


# ── relationships ────────────────────────────────────────────────────────────────────────────
# A child attribute determines a parent when every child value occurs with exactly one parent value:
# COUNT(DISTINCT child) = COUNT(DISTINCT child || parent). NULL is kept as its own value. Several
# pairs share one query (one COUNT(DISTINCT …) per attribute and per pair).

_STOP = {"of", "the", "a", "an", "at", "to", "in", "on", "for", "and", "or", "by", "id", "code"}
_PARENT_HINT = re.compile(r"\b(group|type|category|class|bucket|segment|region|state|country|year|quarter|"
                          r"description|name|channel|program|flag|tier|level|family|division|brand)\b", re.I)
_CHILD_HINT = re.compile(r"\b(code|id|zip|postal|msa|city|county|date|month|week|day|number|key|sku|item)\b", re.I)
_ENTITY_HINT = re.compile(r"\b(id|identifier|number|key|account|customer|loan|patient|member|policy|order|item|"
                          r"product|store|device|entity)\b", re.I)
_SKIP_TYPES = ("double", "real", "float")
# Hierarchy families: a child and parent from the same family are worth testing first.
_FAMILIES = {
    "geography": (re.compile(r"\b(postal|zip|msa|metro\w*|city|county|tract|census|store|branch|site|address)\b", re.I),
                  re.compile(r"\b(state|region|country|territory|district|province|market|area|zone)\b", re.I)),
    "calendar": (re.compile(r"\b(date|day|week|month|period|timestamp)\b", re.I),
                 re.compile(r"\b(year|quarter|month|season|fiscal|half|vintage)\b", re.I)),
    "organization": (re.compile(r"\b(loan|account|customer|patient|member|policy|order|item|sku|product|employee|"
                                r"device|seller|servicer|provider|vendor|supplier)\b", re.I),
                     re.compile(r"\b(pool|deal|portfolio|group|segment|category|family|brand|division|department|"
                                r"plan|program|network|tier|class)\b", re.I)),
}


def _key_expr(attribute: str) -> str:
    return f"COALESCE(CAST({quote_ident(attribute)} AS VARCHAR), '<null>')"


def _words(name: str) -> str:
    """Name with underscores and punctuation as spaces, so word patterns match 'state__id'."""
    return re.sub(r"[_\W]+", " ", name).strip()


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t and t not in _STOP}


def _form_base(name: str) -> str:
    return name.split("__", 1)[0] if "__" in name else name


def _distinct(attr: dict) -> float | None:
    return (attr.get("profile") or {}).get("distinct_values")


def guess_entity(scope: dict) -> str:
    """The attribute most likely to be the entity key: the most distinct values, preferring names
    or descriptions that look like an identifier."""
    attrs = [a for a in scope.get("attributes") or [] if _distinct(a)]
    if not attrs:
        return ""
    def rank(a):
        hint = bool(_ENTITY_HINT.search(_words(a["name"]))) or "identifier" in (a.get("description") or "").lower()
        return (hint and a.get("type", "").lower() not in _SKIP_TYPES, _distinct(a) or 0)
    return max(attrs, key=rank)["name"]


def entity_pairs(scope: dict, entity: str) -> list[dict[str, Any]]:
    attrs = [a["name"] for a in scope.get("attributes") or [] if (_distinct(a) or 0) >= 2]
    return [{"child": entity, "parent": n, "kind": "entity"} for n in attrs if n != entity] if entity in attrs else []


def candidate_pairs(scope: dict, *, entity: str = "", levels: dict[str, str] | None = None, max_pairs: int = 120,
                    max_parent_values: int = 1000) -> list[dict[str, Any]]:
    """child → parent candidates, ranked by name affinity. With `levels` (attribute → "entity" or
    "varying", from the entity tests), only attributes on the same side are paired: a dependency
    between a per-entity attribute and a per-period one is almost always coincidence."""
    attrs = {a["name"]: a for a in scope.get("attributes") or [] if (_distinct(a) or 0) >= 2}
    names = [n for n in attrs if n != entity and attrs[n].get("type", "").lower() not in _SKIP_TYPES]
    scored = []
    for child in names:
        for parent in names:
            if child == parent or _form_base(child) == _form_base(parent):
                continue
            if levels and levels.get(child) and levels.get(parent) and levels[child] != levels[parent]:
                continue
            dc, dp = _distinct(attrs[child]) or 0, _distinct(attrs[parent]) or 0
            if dp > dc or dp > max_parent_values or (dp == dc and child > parent):
                continue
            overlap = len(_tokens(child) & _tokens(parent)) / max(len(_tokens(child) | _tokens(parent)), 1)
            if dp < 3 and not overlap:
                continue   # two-value flags as parents are coincidence unless the names relate
            cw, pw = _words(child), _words(parent)
            family = sum(2.0 for c_re, p_re in _FAMILIES.values() if c_re.search(cw) and p_re.search(pw))
            time_child = bool(_FAMILIES["calendar"][0].search(cw))
            time_parent = bool(_FAMILIES["calendar"][1].search(pw) or _FAMILIES["calendar"][0].search(pw))
            score = 3.0 * overlap + family + (0.5 if _PARENT_HINT.search(pw) else 0.0) + \
                (0.25 if _CHILD_HINT.search(cw) else 0.0) - (1.5 if time_child and not time_parent else 0.0)
            scored.append((score, child, parent))
    scored.sort(key=lambda t: (-t[0], t[1], t[2]))
    return [{"child": c, "parent": p, "kind": "pair"} for _, c, p in scored[:max(max_pairs, 0)]]


def relationship_pairs(scope: dict, *, entity: str = "", levels: dict[str, str] | None = None,
                       max_pairs: int = 120, max_parent_values: int = 1000) -> list[dict[str, Any]]:
    """Entity tests (the entity key against every attribute), then ranked child → parent candidates."""
    entity = entity or guess_entity(scope)
    return entity_pairs(scope, entity) + candidate_pairs(scope, entity=entity, levels=levels, max_pairs=max_pairs,
                                                         max_parent_values=max_parent_values)


def relationship_queries(model: str, pairs: list[dict], *, where: str = "", batch: int = 10) -> list[dict[str, Any]]:
    clause = f" WHERE {where}" if where else ""
    queries = []
    for start in range(0, len(pairs), max(batch, 1)):
        chunk = pairs[start:start + max(batch, 1)]
        attrs: list[str] = []
        for pr in chunk:
            for n in (pr["child"], pr["parent"]):
                if n not in attrs:
                    attrs.append(n)
        cols = [f"COUNT(DISTINCT {_key_expr(n)}) AS a{i}" for i, n in enumerate(attrs)]
        cols += [f"COUNT(DISTINCT {_key_expr(pr['child'])} || '|' || {_key_expr(pr['parent'])}) AS p{j}"
                 for j, pr in enumerate(chunk)]
        sql = f"SELECT {', '.join(cols)} FROM {quote_model(model)}{clause}"  # nosec B608  # identifiers are quoted; the connector is read-only
        queries.append({"sql": sql, "attributes": attrs, "pairs": chunk})
    return queries


def classify_pair(child_values: float, parent_values: float, pair_values: float, kind: str) -> dict[str, Any]:
    exceptions = int(pair_values - child_values)
    holds = exceptions == 0
    near = not holds and child_values >= 50 and exceptions <= 0.01 * child_values
    if kind == "entity":
        verdict = "entity_level" if holds else ("entity_level_with_exceptions" if near else "varies_within_entity")
    elif holds:
        verdict = "one_to_one" if child_values == parent_values else "many_to_one"
    else:
        verdict = "many_to_one_with_exceptions" if near else "no_dependency"
    return {"verdict": verdict, "child_values": child_values, "parent_values": parent_values,
            "pairs": pair_values, "exceptions": max(exceptions, 0),
            "evidence": "strong" if child_values >= 100 else ("moderate" if child_values >= 30 else "weak")}


def _reduce(edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop a→c when a→b and b→c are also present (keep direct parents only)."""
    s = set(edges)
    return [(a, c) for a, c in edges if not any((a, b) in s and (b, c) in s for b in {x for _, x in edges} if b not in (a, c))]


def review_relationships(runner: Runner, project: str, scope: dict, *, entity: str = "", where: str = "",
                         max_pairs: int = 120, batch: int = 10, declared: list[dict] | None = None,
                         log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    model = scope["model"]
    entity = entity or guess_entity(scope)
    tests: list[dict[str, Any]] = []
    pending = 0

    def run(pairs: list[dict]) -> None:
        nonlocal pending
        queries = relationship_queries(model, pairs, where=where, batch=batch)
        for i, q in enumerate(queries):
            log(f"relationships {i + 1}/{len(queries)}")
            try:
                parsed = parse_query_result(runner.call("query", {"project": project, "query": q["sql"]}))
            except PendingReply:
                pending += 1
                continue
            if not parsed["ok"] or not parsed["rows"]:
                tests.extend({**pr, "verdict": "error", "error": parsed["error"] or "no rows"} for pr in q["pairs"])
                continue
            row = parsed["rows"][0]
            counts = {n: svm.number(row.get(f"a{k}")) for k, n in enumerate(q["attributes"])}
            for j, pr in enumerate(q["pairs"]):
                pv = svm.number(row.get(f"p{j}"))
                if counts.get(pr["child"]) is None or pv is None:
                    tests.append({**pr, "verdict": "error", "error": "missing counts in reply"})
                    continue
                tests.append({**pr, **classify_pair(counts[pr["child"]], counts[pr["parent"]] or 0, pv, pr["kind"])})

    # Phase 1: the entity key against every attribute; phase 2: same-level candidate pairs.
    run(entity_pairs(scope, entity))
    pairs_tested = len(tests)
    if not pending:
        levels = {t["parent"]: ("entity" if t["verdict"].startswith("entity_level") else "varying")
                  for t in tests if t["kind"] == "entity" and t["verdict"] != "error"}
        candidates = candidate_pairs(scope, entity=entity, levels=levels, max_pairs=max_pairs)
        run(candidates)
        pairs_tested += len(candidates)

    entity_level = [t["parent"] for t in tests if t["kind"] == "entity" and t["verdict"].startswith("entity_level")]
    varying = [t["parent"] for t in tests if t["kind"] == "entity" and t["verdict"] == "varies_within_entity"]
    one_to_one = [{"a": t["child"], "b": t["parent"], "evidence": t["evidence"]} for t in tests
                  if t["kind"] == "pair" and t["verdict"] == "one_to_one"]
    m2o = [(t["child"], t["parent"]) for t in tests if t["kind"] == "pair" and t["verdict"] == "many_to_one"]
    evidence = {(t["child"], t["parent"]): t["evidence"] for t in tests if t["kind"] == "pair"}
    direct = [{"child": c, "parent": p, "evidence": evidence[(c, p)]} for c, p in _reduce(m2o)]
    out: dict[str, Any] = {"version": 1, "model": model, "entity": entity, "generated": time.strftime("%Y-%m-%d"),
                           "tested_pairs": pairs_tested, "pending_queries": pending, "tests": tests,
                           "suggestions": {"parent_child": direct, "same_entity_one_to_one": one_to_one,
                                           "entity_level_attributes": entity_level,
                                           "attributes_varying_within_entity": varying},
                           "notes": ["A dependency in the data is evidence, not proof: recommend a relationship "
                                     "only when it also makes business sense (domain notes). Evidence: 'weak' "
                                     "below 30 child values (often coincidence in small samples), 'moderate' "
                                     "30-99, 'strong' from 100.",
                                     "One-to-one pairs are usually forms of one attribute (code + description) or "
                                     "a redundant attribute."]}
    if declared is not None:
        found = {(t["child"].lower(), t["parent"].lower()): t["verdict"] for t in tests}
        checks = []
        for d in declared:
            key = (str(d.get("child", "")).lower(), str(d.get("parent", "")).lower())
            checks.append({"child": d.get("child"), "parent": d.get("parent"),
                           "data": found.get(key, "not_tested")})
        declared_keys = {(str(d.get("child", "")).lower(), str(d.get("parent", "")).lower()) for d in declared}
        out["declared"] = {"checks": checks,
                           "contradicted": [c for c in checks if c["data"] == "no_dependency"],
                           "supported_but_undeclared": [d for d in direct
                                                        if (d["child"].lower(), d["parent"].lower()) not in declared_keys]}
    return out


# ── lookup tables ────────────────────────────────────────────────────────────────────────────

_CODE_NAME = re.compile(r"\b(code|status|type|indicator|flag|channel|method|purpose|program|class|category|"
                        r"reason|plan|source|grade|tier|segment)\b", re.I)
_PERIOD_NAME = re.compile(r"\b(month|period|date|week|quarter|snapshot|as of)\b", re.I)
_YES_NO = {"y", "n", "yes", "no", "true", "false", "0", "1", "u", "unknown", ""}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "lookup"


def _has_description_sibling(name: str, names: list[str]) -> bool:
    base = _form_base(name).lower()
    stem = re.sub(r"\b(code|id|indicator|flag)\b", "", base).strip()
    for other in names:
        o = other.lower()
        if o == name.lower():
            continue
        if _form_base(o) == base and any(w in o for w in ("description", "desc", "name", "label")):
            return True
        if stem and stem in o and any(w in o for w in ("description", "desc", " name", "label")):
            return True
    return False


def lookup_suggestions(scope: dict, *, period: str = "", max_code_values: int = 200,
                       relationships: dict | None = None) -> list[dict[str, Any]]:
    attrs = scope.get("attributes") or []
    names = [a["name"] for a in attrs]
    out: list[dict[str, Any]] = []
    for a in attrs:
        prof = a.get("profile") or {}
        d = prof.get("distinct_values")
        is_text = a.get("type", "").lower() in ("varchar", "char", "string", "text")
        is_numeric_code = a.get("type", "").lower() in _NUMERIC and bool(_CODE_NAME.search(_words(a["name"]))) \
            and not looks_like_integer_date(a)
        if not (is_text or is_numeric_code) or not d or d < 2:
            continue
        if d > max_code_values or "__" in a["name"] or _has_description_sibling(a["name"], names):
            continue
        if re.search(r"\b(description|desc|name|label|text)\b", a["name"], re.I):
            continue   # already the readable side
        values = [str(v) for v in prof.get("values") or [] if v is not None]
        if values and {v.lower() for v in values} <= _YES_NO:
            continue   # readable as it is
        short_codes = bool(values) and all(len(v) <= 4 for v in values)
        if not (_CODE_NAME.search(_words(a["name"])) or short_codes):
            continue
        desc = (a.get("description") or "").lower()
        out.append({"id": f"L-{_slug(a['name'])}", "kind": "code_lookup", "attribute": a["name"],
                    "distinct_values": d,
                    "reason": "coded values with no description form or attribute"
                              + ("; the codes are explained only in the description text" if "=" in desc or ":" in desc
                                 else ""),
                    "columns": [a["name"], f"{a['name']} description", "source"]})
    period_attr = period
    if not period_attr:
        dated = [a for a in attrs if (a.get("type", "").lower() in ("date", "timestamp") or looks_like_integer_date(a))
                 and _PERIOD_NAME.search(_words(a["name"])) and (_distinct(a) or 0) >= 2]
        if dated:
            preferred = re.compile(r"\b(period|reporting|snapshot|as of|activity|month)\b", re.I)
            period_attr = max(dated, key=lambda a: (bool(preferred.search(_words(a["name"]))),
                                                    _distinct(a) or 0))["name"]
    latest = any("latest" in n.lower() or "current period" in n.lower() for n in names)
    if period_attr and not latest:
        out.append({"id": f"L-{_slug(period_attr)}", "kind": "period_lookup", "attribute": period_attr,
                    "reason": "no latest-period flag: point-in-time metrics resolve 'latest' per group without one, "
                              "and calendar parents (year, quarter) need a period table",
                    "columns": [period_attr, "year", "quarter", "month", "latest period flag"]})
    if relationships:
        static = (relationships.get("suggestions") or {}).get("entity_level_attributes") or []
        if len(static) >= 5:
            out.append({"id": "L-entity", "kind": "entity_table", "attribute": relationships.get("entity"),
                        "reason": f"{len(static)} attributes are fixed per entity but repeat on every fact row; "
                                  "an entity table is optional — recommend it only if it fixes a question "
                                  "(counts, once-per-entity amounts) or the fact is large",
                        "columns": [relationships.get("entity")] + static[:50]})
    return out


def _period_parts(value: str) -> tuple[str, str, str]:
    m = re.match(r"^(\d{4})-?(\d{2})", _plain_number(value))
    if not m:
        return "", "", ""
    year, month = m.group(1), int(m.group(2))
    return year, f"{year}-Q{(month - 1) // 3 + 1}", f"{month:02d}"


def _write_csv(path: str, header: list[str], rows: list[list[Any]]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def build_lookups(runner: Runner, project: str, scope: dict, suggestions: list[dict], csv_dir: str, *,
                  where: str = "", max_values: int = 1000, batch: int = 8) -> list[dict[str, Any]]:
    """Write a CSV template per suggestion: key values from the data (reusing the scope's samples,
    querying the rest in batches); description columns left blank for the agent to fill from cited
    sources (or the user to fill)."""
    os.makedirs(csv_dir, exist_ok=True)
    model = scope["model"]
    profiles = {a["name"]: a.get("profile") or {} for a in scope.get("attributes") or []}
    known: dict[str, Any] = {}
    wanted: dict[str, float] = {}
    for sug in suggestions:
        if sug["kind"] == "entity_table":
            continue
        prof = profiles.get(sug["attribute"], {})
        if prof.get("values") is not None and prof.get("distinct_values") is not None \
                and len([v for v in prof["values"] if v is not None]) >= prof["distinct_values"]:
            known[sug["attribute"]] = prof["values"]
        else:
            wanted[sug["attribute"]] = min(prof.get("distinct_values") or max_values, max_values)
    known.update(_sample_values(runner, project, model, wanted, where, batch))
    done = []
    for sug in suggestions:
        if sug["kind"] == "entity_table":
            done.append({**sug, "csv": None, "note": "not generated: build it from the source extract if approved"})
            continue
        got = known.get(sug["attribute"])
        if got == "pending":
            done.append({**sug, "csv": None, "pending": True})
            continue
        if got is None:
            done.append({**sug, "csv": None, "error": "values could not be read"})
            continue
        values = [_plain_number(v) if sug["kind"] == "period_lookup" else v for v in got if v not in (None, "")]
        dropped = _null_artifacts(values, profiles.get(sug["attribute"], {}), scope, sug["attribute"])
        values = [v for v in values if v not in dropped]
        path = os.path.join(csv_dir, f"{_slug(sug['attribute'])}_lookup.csv")
        if sug["kind"] == "code_lookup":
            _write_csv(path, sug["columns"], [[v, "", ""] for v in values])
        else:
            latest = max(values) if values else None
            _write_csv(path, sug["columns"], [[v, *_period_parts(v), "Y" if v == latest else "N"] for v in values])
        entry = {**sug, "csv": path, "rows": len(values)}
        if dropped:
            entry["note"] = (f"dropped {sorted(dropped)}: outside the attribute's min/max, so a NULL rendered as a "
                             f"number by CAST — represent 'no value' explicitly if users need it")
        done.append(entry)
    return done


def _null_artifacts(values: list, prof: dict, scope: dict, attribute: str) -> set:
    """Values of a numeric attribute below its profiled MIN: seen live, a NULL in a numeric column comes
    back as '0' from CAST(... AS VARCHAR) while MIN / COUNT(DISTINCT) ignore it."""
    attr = next((a for a in scope.get("attributes") or [] if a["name"] == attribute), {})
    lo = _plain_number(prof.get("min"))
    if attr.get("type", "").lower() not in _NUMERIC or not re.fullmatch(r"-?\d+(\.\d+)?", lo or ""):
        return set()
    out = set()
    for v in values:
        pv = _plain_number(v)
        if re.fullmatch(r"-?\d+(\.\d+)?", pv) and float(pv) < float(lo):
            out.add(v)
    return out


def lookup_steps(args: dict) -> str:
    """Manual steps for adding a lookup table. `new_attributes` become attributes of their own (related
    to the key attribute): anything a metric filters on, such as a flag, must be an attribute, because
    an embedded filter targets attributes, not forms. `new_forms` become forms of the key attribute
    (descriptions, labels)."""
    key = args.get("key_attribute") or "<key attribute>"
    attrs = list(args.get("new_attributes") or [])
    forms = list(args.get("new_forms") or [])
    steps = [f"Add {args.get('csv') or '<csv file>'} to the model as a new table.",
             f"Map its key column to the EXISTING attribute '{key}' — keep the attribute; deleting or replacing it "
             f"deletes the derived metrics that filter on it."]
    if attrs:
        steps.append(f"Create {', '.join(repr(a) for a in attrs)} as attributes of their own (not forms), each "
                     f"related to '{key}', with business names and descriptions: metrics filter on them, and an "
                     f"embedded filter can target an attribute but not a form.")
    if forms:
        steps.append(f"Add {', '.join(repr(f) for f in forms)} as forms of '{key}' (display only, never filtered on).")
    if not attrs and not forms:
        steps.append(f"Add the new columns: as attributes of their own if any metric will filter on them, "
                     f"otherwise as forms of '{key}'.")
    steps += [f"Check that '{key}' uses the new table as its lookup (auto-detection may keep the fact table).",
              "Republish and re-certify; then re-run the questions that depend on it."]
    return " ".join(f"{i}. {t}" for i, t in enumerate(steps, 1))


def source_change_text(changes: list[dict]) -> str:
    """One consolidated rebuild of the source data for every source_change action in the plan."""
    by_table: dict[str, list[dict]] = {}
    for c in changes:
        by_table.setdefault(c.get("table") or "the source table", []).append(c)
    lines = ["Rebuild the source data ONCE with every change below, reload it, then re-map the EXISTING "
             "attributes and fact metrics to the reloaded columns (never delete and recreate them: derived "
             "metrics that reference them would be deleted)."]
    for table, items in by_table.items():
        lines.append(f"In {table}:")
        for c in items:
            what = c.get("change") or ""
            col = c.get("column")
            lines.append(f"  - [{c.get('finding')}] {col + ': ' if col else ''}{what}")
    lines.append("Then republish and re-certify (once, at the end of this phase).")
    return "\n".join(lines)


# ── grade ────────────────────────────────────────────────────────────────────────────────────

def _reference_rows(q: dict, result: dict, bank_dir: str) -> tuple[list[dict] | None, str]:
    exp = q.get("expected") or {}
    src = exp.get("source") or "none"
    if src == "inline":
        return list(exp.get("rows") or []), ""
    if src == "reference_file":
        path = exp.get("file") or ""
        path = path if os.path.isabs(path) else os.path.join(bank_dir, path)
        try:
            return svm.load_rows(path), ""
        except SystemExit as e:
            return None, str(e)
    if src == "same_model_sql":
        run = result.get("expected_run") or {}
        if not run:
            return None, "expected SQL was not run"
        if not run.get("ok"):
            return None, f"expected SQL failed: {run.get('error')}"
        return list(run.get("rows") or []), ""
    return None, "no expected value"


def grade_bank(bank: dict, results: dict, *, bank_dir: str = ".", tolerance: float | None = None,
               previous: dict | None = None) -> dict[str, Any]:
    by_id = {r.get("id"): r for r in results.get("results") or []}
    graded = []
    for q in bank.get("questions") or []:
        exp = q.get("expected") or {}
        item: dict[str, Any] = {"id": q.get("id"), "level": q.get("level"), "area": q.get("area"),
                                "use_case": q.get("use_case"), "expected_path": q.get("expected_path"),
                                "strength": exp.get("strength") or ("same_model" if exp.get("source") == "same_model_sql"
                                                                    else None)}
        res = by_id.get(q.get("id"))
        if res is None:
            item["verdict"] = "not_run"
        elif res.get("skipped"):
            item["verdict"] = "ungraded" if q.get("expected_path") == "not_answerable" else "error"
            item["note"] = res.get("error")
        elif not res.get("ok"):
            item.update({"verdict": "error", "note": res.get("error")})
        else:
            ref, why = _reference_rows(q, res, bank_dir)
            if ref is None:
                item.update({"verdict": "ungraded" if why == "no expected value" else "error", "note": why})
            else:
                tol = exp.get("tolerance", tolerance if tolerance is not None else 1e-6)
                try:
                    cmp = svm.compare_rows(res.get("rows") or [], ref, list(q.get("key") or []),
                                           list(q.get("measures") or []), float(tol), str(q.get("id")))
                    item["verdict"] = {"ok": "pass", "mismatch": "fail", "empty": "empty"}.get(cmp["status"], "fail")
                    item["comparison"] = cmp
                except SystemExit as e:
                    item.update({"verdict": "error", "note": f"comparison: {e}"})
            item["seconds"] = res.get("seconds")
        graded.append(item)

    summary: dict[str, Any] = {"total": len(graded), "by_verdict": {}, "by_level": {}, "by_expected_path": {},
                               "by_strength": {}, "by_use_case": {}}
    for g in graded:
        summary["by_verdict"][g["verdict"]] = summary["by_verdict"].get(g["verdict"], 0) + 1
        for key, val in (("by_level", str(g["level"])), ("by_expected_path", str(g["expected_path"])),
                         ("by_strength", str(g["strength"])), ("by_use_case", str(g["use_case"]))):
            bucket = summary[key].setdefault(val, {})
            bucket[g["verdict"]] = bucket.get(g["verdict"], 0) + 1
    out: dict[str, Any] = {"version": 1, "model": bank.get("model"), "summary": summary, "questions": graded}
    if previous:
        before = {g.get("id"): g.get("verdict") for g in previous.get("questions") or []}
        changes = [{"id": g["id"], "before": before[g["id"]], "after": g["verdict"]}
                   for g in graded if g["id"] in before and before[g["id"]] != g["verdict"]]
        out["changes"] = changes
        out["regressions"] = [c["id"] for c in changes if c["before"] == "pass"]
    return out


# ── plan ─────────────────────────────────────────────────────────────────────────────────────

def _phase_of(f: dict) -> int | None:
    if f.get("phase") in PHASES:
        return int(f["phase"])
    return CATEGORY_PHASE.get(str(f.get("category") or ""))


def _arg_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(v) for v in (value if isinstance(value, list) else [value])]


def action_commands(action: dict, model_id: str, python: str = "python3") -> dict[str, Any]:
    """One finding action → {"dry_run": argv | None, "apply": argv | None, "manual": text | None,
    "problems": [...]}."""
    kind = action.get("kind")
    a = action.get("args") or {}
    base = [python, BUILD_SCRIPT]
    problems: list[str] = []

    def need(*keys: str) -> None:
        for k in keys:
            if a.get(k) in (None, "", []):
                problems.append(f"{kind}: missing args.{k}")

    def opt(argv: list[str], flag: str, key: str) -> None:
        if a.get(key) not in (None, "", []):
            argv += [flag, str(a[key])]

    if kind == "create_conditional_metric":
        need("name", "attribute", "elements")
        if bool(a.get("source_metric")) == bool(a.get("count_attribute")):
            problems.append(f"{kind}: give exactly one of args.source_metric or args.count_attribute")
        argv = base + ["create-conditional-metric", "--model-id", model_id, "--name", str(a.get("name", ""))]
        opt(argv, "--source-metric", "source_metric")
        opt(argv, "--count-attribute", "count_attribute")
        argv += ["--attribute", str(a.get("attribute", "")), "--elements"] + _arg_list(a.get("elements"))
        for cond in a.get("and") or []:
            if not isinstance(cond, list) or len(cond) < 2:
                problems.append(f"{kind}: each args.and entry is [attribute, value, ...]")
                continue
            argv += ["--and"] + [str(c) for c in cond]
        for flag, key in (("--function", "function"), ("--level-rule", "level_rule"),
                          ("--level-attribute", "level_attribute"), ("--format", "format"),
                          ("--description", "description")):
            opt(argv, flag, key)
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "create_compound_metric":
        need("name", "formula")
        argv = base + ["create-compound-metric", "--model-id", model_id, "--name", str(a.get("name", "")),
                       "--formula", str(a.get("formula", ""))]
        for flag, key in (("--join", "join"), ("--format", "format"), ("--description", "description")):
            opt(argv, flag, key)
        if a.get("replace"):
            argv.append("--replace")
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "delete_derived_metric":
        if not a.get("name") and not a.get("object_id"):
            problems.append(f"{kind}: missing args.name or args.object_id")
        argv = base + ["delete-derived-metric", "--model-id", model_id]
        argv += ["--object-id", str(a["object_id"])] if a.get("object_id") else ["--name", str(a.get("name", ""))]
        apply = argv + ["--yes"] + (["--force"] if a.get("force") else [])
        return {"dry_run": argv + ["--dry-run"], "apply": apply, "manual": None, "problems": problems}
    if kind == "patch_model_object":
        need("kind", "json_file")
        target = ["--kind", str(a.get("kind", "")), "--model-id", model_id]
        if a.get("object_id"):
            target += ["--object-id", str(a["object_id"])]
        before = str(a.get("before_out") or f"before_{a.get('object_id') or 'model'}.json")
        return {"dry_run": base + ["get-model-object"] + target + ["--out", before],
                "apply": base + ["patch-model-object"] + target + ["--json-file", str(a.get("json_file", "")),
                                                                    "--before-out", before, "--yes"],
                "manual": None, "problems": problems}
    if kind == "patch_fact_metrics":
        need("spec")
        argv = base + ["patch-fact-metrics", "--model-id", model_id, "--spec", str(a.get("spec", ""))]
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "source_change":
        if not (a.get("change") or "").strip():
            problems.append(f"{kind}: missing args.change")
        return {"dry_run": None, "apply": None, "manual": None, "problems": problems}
    if kind == "add_lookup_table":
        need("csv", "key_attribute")
        return {"dry_run": None, "apply": None, "manual": lookup_steps(a), "problems": problems}
    if kind == "publish":
        return {"dry_run": None, "apply": base + ["publish", "--model-id", model_id], "manual": None,
                "problems": problems}
    if kind == "certify":
        return {"dry_run": None, "apply": base + ["certify", "--object-id", model_id], "manual": None,
                "problems": problems}
    steps = action.get("steps") or a.get("steps")
    if kind != "manual":
        problems.append(f"unknown action kind '{kind}' (use one of {', '.join(ACTION_KINDS)})")
    elif not steps:
        problems.append("manual: missing steps")
    return {"dry_run": None, "apply": None, "manual": steps, "problems": problems}


def _ps_quote(arg: str) -> str:
    return arg if re.fullmatch(r"[A-Za-z0-9_\-./:=+,@]+", arg) else "'" + arg.replace("'", "''") + "'"


def render(argv: list[str] | None, quoting: str = "posix") -> str | None:
    if argv is None:
        return None
    if quoting == "powershell":
        return " ".join(_ps_quote(a) for a in argv)
    return shlex.join(argv)


def _order(findings: list[dict], problems: list[str]) -> list[dict]:
    """Phase order, then depends_on (topological, stable by id) inside each phase."""
    ids = {f["id"]: f for f in findings}
    done: set[str] = set()
    ordered: list[dict] = []
    for phase in sorted({_phase_of(f) for f in findings if _phase_of(f) is not None}):
        pending = sorted((f for f in findings if _phase_of(f) == phase), key=lambda f: f["id"])
        while pending:
            ready = [f for f in pending if all(d in done or d not in ids for d in f.get("depends_on") or [])]
            if not ready:
                problems.append(f"phase {phase}: dependency cycle or later-phase dependency among "
                                f"{[f['id'] for f in pending]}")
                ready = pending[:]
            for f in ready:
                ordered.append(f)
                done.add(f["id"])
                pending.remove(f)
    return ordered


def build_plan(findings_doc: dict, *, model_id: str = "", statuses: tuple[str, ...] = PLANNED_STATUSES,
               python: str = "python3", quoting: str = "posix", project: str = "",
               bank: str = "question_bank.json") -> dict[str, Any]:
    problems: list[str] = list(check_findings(findings_doc)["problems"])
    warnings: list[str] = []
    every = findings_doc.get("findings") or []
    by_id = {f.get("id"): f for f in every}
    chosen = [f for f in every if f.get("status") in statuses]
    mid = model_id or "<MODEL_ID>"
    if not model_id:
        warnings.append("no --model-id: commands carry the <MODEL_ID> placeholder")
    for f in chosen:
        for dep in f.get("depends_on") or []:
            d = by_id.get(dep)
            if d is None:
                continue
            if d.get("status") not in statuses + ("applied", "verified"):
                warnings.append(f"{f['id']} depends on {dep}, which is {d.get('status')}")
            elif d.get("status") in statuses and (_phase_of(d) or 0) > (_phase_of(f) or 0):
                problems.append(f"{f['id']} (phase {_phase_of(f)}) depends on {dep} in a later phase "
                                f"({_phase_of(d)}); move one of them")
    ordered = _order(chosen, problems)

    def cmd(argv: list[str] | None) -> dict[str, Any] | None:
        return None if argv is None else {"argv": argv, "display": render(argv, quoting)}

    # Every source_change in the plan becomes one rebuild step at the start of phase 1.
    source_changes = []
    for f in ordered:
        for a in f.get("actions") or []:
            if a.get("kind") == "source_change":
                source_changes.append({**(a.get("args") or {}), "finding": f["id"]})
    rebuild_step = None
    if source_changes:
        rebuild_step = {"finding": ", ".join(dict.fromkeys(c["finding"] for c in source_changes)),
                        "title": "Rebuild the source data once", "kind": "source_change", "dry_run": None,
                        "apply": None, "manual": source_change_text(source_changes),
                        "risks": ["Re-map existing attributes and fact metrics to the reloaded columns; deleting "
                                  "them deletes the derived metrics that reference them."]}

    # A metric can filter on an attribute, not on a form: flag filters on columns the plan adds as forms.
    forms_added = {str(x).lower(): f["id"] for f in ordered for a in f.get("actions") or []
                   if a.get("kind") == "add_lookup_table" for x in (a.get("args") or {}).get("new_forms") or []}
    for f in ordered:
        for a in f.get("actions") or []:
            if a.get("kind") != "create_conditional_metric":
                continue
            args = a.get("args") or {}
            used = [args.get("attribute")] + [c[0] for c in args.get("and") or [] if isinstance(c, list) and c]
            for u in used:
                if u and str(u).lower() in forms_added:
                    problems.append(f"{f['id']}: filters on '{u}', which {forms_added[str(u).lower()]} adds as a form; "
                                    f"list it under new_attributes instead (filters need an attribute)")

    run_cmd = [python, ASSESS_SCRIPT, "run", "--project", project or "<PROJECT>", "--bank", bank,
               "--out", "results_after_phase_{n}.json"]
    grade_cmd = [python, ASSESS_SCRIPT, "grade", "--bank", bank, "--results", "results_after_phase_{n}.json",
                 "--previous", "graded_before.json", "--out", "graded_after_phase_{n}.json"]
    phases = []
    phase_numbers = sorted({_phase_of(f) for f in ordered} | ({1} if rebuild_step else set()))
    for n in phase_numbers:
        items = [f for f in ordered if _phase_of(f) == n]
        steps = [rebuild_step] if (n == 1 and rebuild_step) else []
        for f in items:
            for action in f.get("actions") or [{"kind": "manual", "steps": f.get("recommendation")}]:
                if action.get("kind") == "source_change":
                    c = action_commands(action, mid, python)
                    problems += [f"{f['id']}: {p}" for p in c["problems"]]
                    if n != 1:
                        steps.append({"finding": f["id"], "title": f.get("title"), "kind": "note", "dry_run": None,
                                      "apply": None, "manual": None, "risks": [],
                                      "note": "Source change done in the phase 1 rebuild; nothing to do here."})
                    continue
                c = action_commands(action, mid, python)
                problems += [f"{f['id']}: {p}" for p in c["problems"]]
                steps.append({"finding": f["id"], "title": f.get("title"), "kind": action.get("kind"),
                              "dry_run": cmd(c["dry_run"]), "apply": cmd(c["apply"]), "manual": c["manual"],
                              "risks": f.get("risks") or []})
        before = ["Save a backup copy of the model before applying this phase."]
        if n == 3:
            before.append("For every attribute removed or replaced: list the derived metrics that filter on "
                          "it (they are deleted with it) and plan to recreate them.")
        republish = n in REPUBLISH_PHASES or any(f.get("republish") for f in items)
        if n != 1 and any(a.get("kind") == "source_change" for f in items for a in f.get("actions") or []) and \
                not any(a.get("kind") != "source_change" for f in items for a in f.get("actions") or []) and \
                not any(f.get("republish") for f in items):
            republish = False   # its only work moved into the phase 1 rebuild
        after = []
        if republish:
            after.append({"what": "Republish the model (tables, attributes or fact metrics changed).",
                          "command": cmd([python, BUILD_SCRIPT, "publish", "--model-id", mid])})
        after.append({"what": "Re-certify the model (edits drop certification; MCP cannot see an uncertified model).",
                      "command": cmd([python, BUILD_SCRIPT, "certify", "--object-id", mid])})
        tests = sorted({t for f in items for t in f.get("tests") or []})
        if n == 1 and rebuild_step:
            tests = sorted(set(tests) | {t for f in ordered for t in f.get("tests") or []
                                         if any(a.get("kind") == "source_change" for a in f.get("actions") or [])})
        if not tests:
            warnings.append(f"phase {n}: no unit tests named in its findings")
        names = [f["id"] for f in items]
        if n == 1 and rebuild_step:
            names = list(dict.fromkeys(c["finding"] for c in source_changes)) + [x for x in names if x not in
                                                                                 {c["finding"] for c in source_changes}]
        phases.append({"phase": n, "name": PHASES[n], "findings": names,
                       "before": before, "steps": steps, "unit_tests": tests,
                       "regression": [cmd([a.replace("{n}", str(n)) for a in run_cmd]),
                                      cmd([a.replace("{n}", str(n)) for a in grade_cmd])],
                       "after": after})
    skipped = [{"id": f.get("id"), "status": f.get("status")} for f in every if f not in chosen]
    return {"version": 1, "model": findings_doc.get("model"), "model_id": model_id or None,
            "generated": time.strftime("%Y-%m-%d"), "statuses_planned": list(statuses),
            "phases": phases, "not_planned": skipped, "problems": problems, "warnings": warnings}


# ── check ────────────────────────────────────────────────────────────────────────────────────

def _dupes(ids: list[Any]) -> list[Any]:
    seen, dup = set(), []
    for i in ids:
        if i in seen and i not in dup:
            dup.append(i)
        seen.add(i)
    return dup


def check_bank(bank: dict) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []
    if not bank.get("model"):
        problems.append("bank: missing model")
    qs = bank.get("questions")
    if not isinstance(qs, list) or not qs:
        return {"problems": problems + ["bank: no questions"], "warnings": warnings}
    if _dupes([q.get("id") for q in qs]):
        problems.append(f"bank: duplicate ids {_dupes([q.get('id') for q in qs])}")
    for q in qs:
        qid = q.get("id") or "<no id>"
        if not q.get("id"):
            problems.append("bank: a question has no id")
        if q.get("level") not in LEVELS:
            problems.append(f"{qid}: level must be 1-5")
        if not (q.get("question") or "").strip():
            problems.append(f"{qid}: missing question text")
        if q.get("expected_path") not in PATHS:
            problems.append(f"{qid}: expected_path must be one of {', '.join(PATHS)}")
        if not (q.get("sql") or "").strip() and q.get("expected_path") != "not_answerable":
            problems.append(f"{qid}: missing sql")
        if (q.get("sql") or "").strip() and "{{MODEL}}" not in q["sql"]:
            warnings.append(f"{qid}: sql has no {{{{MODEL}}}} placeholder")
        for key in ("key", "measures", "metrics", "domain_claims"):
            if key in q and not isinstance(q[key], list):
                problems.append(f"{qid}: {key} must be a list")
        if q.get("use_case") is not None and not isinstance(q["use_case"], str):
            problems.append(f"{qid}: use_case must be a use-case id (text)")
        exp = q.get("expected") or {"source": "none"}
        src = exp.get("source", "none")
        if src not in EXPECTED_SOURCES:
            problems.append(f"{qid}: expected.source must be one of {', '.join(EXPECTED_SOURCES)}")
        if src == "inline" and not isinstance(exp.get("rows"), list):
            problems.append(f"{qid}: inline expected needs rows")
        if src == "reference_file" and not exp.get("file"):
            problems.append(f"{qid}: reference_file expected needs file")
        if src == "same_model_sql" and not (exp.get("sql") or "").strip():
            problems.append(f"{qid}: same_model_sql expected needs sql")
        if exp.get("strength") not in (None,) + STRENGTHS:
            problems.append(f"{qid}: expected.strength must be one of {', '.join(STRENGTHS)}")
        if src == "same_model_sql" and exp.get("strength") not in (None, "same_model"):
            problems.append(f"{qid}: same-model SQL can only be strength same_model")
    target = {str(k): float(v) for k, v in (bank.get("target_mix") or DEFAULT_MIX).items()}
    total_target = sum(target.values()) or 1.0
    counts = {str(lv): sum(1 for q in qs if q.get("level") == lv) for lv in LEVELS}
    mix = {lv: {"count": counts[lv], "share": round(counts[lv] / len(qs), 3),
                "target_share": round(target.get(lv, 0) / total_target, 3)} for lv in counts}
    for lv, m in mix.items():
        if abs(m["share"] - m["target_share"]) > 0.05:
            warnings.append(f"level {lv}: {m['share']:.0%} of questions vs target {m['target_share']:.0%}")
    graded = sum(1 for q in qs if (q.get("expected") or {}).get("source", "none") != "none")
    return {"problems": problems, "warnings": warnings, "mix": mix,
            "with_expected_values": graded, "questions": len(qs)}


def _cycles(findings: list[dict]) -> list[str]:
    graph = {f.get("id"): [d for d in f.get("depends_on") or []] for f in findings}
    state: dict[Any, int] = {}
    found: list[str] = []

    def visit(node: Any, path: list[Any]) -> None:
        state[node] = 1
        for nxt in graph.get(node, []):
            if nxt not in graph:
                continue
            if state.get(nxt) == 1:
                found.append(" -> ".join(str(p) for p in path[path.index(nxt):] + [nxt]))
            elif not state.get(nxt):
                visit(nxt, path + [nxt])
        state[node] = 2

    for node in graph:
        if not state.get(node):
            visit(node, [node])
    return found


def check_findings(doc: dict) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []
    fs = doc.get("findings")
    if not isinstance(fs, list):
        return {"problems": ["findings: missing findings list"], "warnings": warnings}
    ids = [f.get("id") for f in fs]
    if _dupes(ids):
        problems.append(f"findings: duplicate ids {_dupes(ids)}")
    known = set(ids)
    for f in fs:
        fid = f.get("id") or "<no id>"
        if not f.get("id"):
            problems.append("findings: a finding has no id")
        if f.get("category") not in CATEGORY_PHASE:
            if f.get("phase") in PHASES:
                warnings.append(f"{fid}: category '{f.get('category')}' is not a standard one")
            else:
                problems.append(f"{fid}: unknown category '{f.get('category')}' and no phase 1-7")
        if f.get("phase") is not None and f.get("phase") not in PHASES:
            problems.append(f"{fid}: phase must be 1-7")
        for key, allowed in (("impact", IMPACTS), ("effort", EFFORTS), ("status", STATUSES)):
            if f.get(key) not in allowed:
                problems.append(f"{fid}: {key} must be one of {', '.join(allowed)}")
        for dep in f.get("depends_on") or []:
            if dep not in known:
                problems.append(f"{fid}: depends_on unknown finding {dep}")
        for action in f.get("actions") or []:
            if action.get("kind") not in ACTION_KINDS:
                problems.append(f"{fid}: unknown action kind '{action.get('kind')}'")
        if f.get("status") in ("modified", "declined") and not (f.get("decision_note") or "").strip():
            warnings.append(f"{fid}: {f.get('status')} without a decision_note")
        if not f.get("tests"):
            warnings.append(f"{fid}: no tests named")
    problems += [f"findings: dependency cycle {c}" for c in _cycles(fs)]
    return {"problems": problems, "warnings": warnings}


def check_domain(doc: dict) -> dict[str, Any]:
    problems: list[str] = []
    warnings: list[str] = []
    claims = doc.get("claims")
    if not isinstance(claims, list):
        return {"problems": ["domain: missing claims list"], "warnings": warnings}
    if _dupes([c.get("id") for c in claims]):
        problems.append(f"domain: duplicate ids {_dupes([c.get('id') for c in claims])}")
    for c in claims:
        cid = c.get("id") or "<no id>"
        if not (c.get("claim") or "").strip():
            problems.append(f"{cid}: missing claim text")
        sources = c.get("sources")
        if not isinstance(sources, list) or not any(isinstance(s, dict) and (s.get("url") or s.get("title"))
                                                    for s in sources):
            problems.append(f"{cid}: no source — move it to assumptions or cite where it comes from")
        if c.get("verified_against_data") not in VERIFIED_VALUES:
            problems.append(f"{cid}: verified_against_data must be one of {', '.join(VERIFIED_VALUES)}")
        if c.get("verified_against_data") == "contradicted":
            warnings.append(f"{cid}: contradicted by the data — record a finding")
        for s in sources if isinstance(sources, list) else []:
            if not isinstance(s, dict):
                continue
            if s.get("type") not in (None,) + SOURCE_TYPES:
                warnings.append(f"{cid}: source type '{s.get('type')}' is not one of {', '.join(SOURCE_TYPES)}")
            if s.get("type") == "user_document" and not s.get("location"):
                warnings.append(f"{cid}: user document '{s.get('title')}' cited without a section or page")
    use_cases = doc.get("use_cases") or []
    if not isinstance(use_cases, list):
        problems.append("domain: use_cases must be a list")
        use_cases = []
    if _dupes([u.get("id") for u in use_cases]):
        problems.append(f"domain: duplicate use-case ids {_dupes([u.get('id') for u in use_cases])}")
    for u in use_cases:
        uid = u.get("id") or "<no id>"
        if not u.get("id"):
            problems.append("domain: a use case has no id")
        if not (u.get("description") or "").strip():
            problems.append(f"{uid}: missing use-case description")
        if u.get("priority") not in PRIORITIES:
            problems.append(f"{uid}: priority must be one of {', '.join(PRIORITIES)}")
        if not (u.get("source") or "").strip():
            warnings.append(f"{uid}: no source for this use case (user document, user statement or claim id)")
    if not use_cases:
        warnings.append("domain: no use_cases recorded; questions cannot be checked for coverage")
    user_docs = sorted({s.get("title") for c in claims for s in (c.get("sources") or [])
                        if isinstance(s, dict) and s.get("type") == "user_document" and s.get("title")})
    return {"problems": problems, "warnings": warnings, "claims": len(claims), "use_cases": len(use_cases),
            "user_documents": user_docs, "assumptions": len(doc.get("assumptions") or [])}


def check_coverage(bank: dict, domain: dict) -> dict[str, Any]:
    """Cross-check a bank against domain notes: every use case has questions, and every claim or
    use case a question cites exists."""
    problems: list[str] = []
    warnings: list[str] = []
    claims = {c.get("id") for c in domain.get("claims") or [] if isinstance(c, dict)}
    use_cases = {u.get("id"): u for u in domain.get("use_cases") or [] if isinstance(u, dict)}
    per_use_case = {uid: 0 for uid in use_cases}
    for q in bank.get("questions") or []:
        qid = q.get("id") or "<no id>"
        for cid in q.get("domain_claims") or []:
            if cid not in claims:
                problems.append(f"{qid}: cites unknown domain claim {cid}")
        uc = q.get("use_case")
        if uc is None:
            if use_cases:
                warnings.append(f"{qid}: no use_case")
        elif uc not in use_cases:
            problems.append(f"{qid}: unknown use case {uc}")
        else:
            per_use_case[uc] += 1
    total = sum(per_use_case.values()) or 1
    for uid, n in per_use_case.items():
        if n == 0:
            problems.append(f"use case {uid} has no questions")
        elif use_cases[uid].get("priority") == "high" and n / total < 1 / max(len(use_cases), 1):
            warnings.append(f"use case {uid} is high priority but has a below-average share of questions ({n})")
    return {"problems": problems, "warnings": warnings, "questions_per_use_case": per_use_case}


# ── CLI ──────────────────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="", help="Library URL used to find the MCP server (env MSTR_BASE)")
    ap.add_argument("--mcp-url", default=os.environ.get("MSTR_MCP_URL", ""), help="MCP endpoint (env MSTR_MCP_URL)")
    ap.add_argument("--server", default="mosaic", help="which tenant MCP server (default mosaic)")
    ap.add_argument("--replies", default="",
                    help="replay mode: read MCP replies saved in this folder instead of signing in; missing ones "
                         "are listed in <folder>/pending.json (for sessions that call the connector tools directly)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scope", help="list attributes and metrics and profile each attribute")
    sp.add_argument("--project", required=True)
    sp.add_argument("--model", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--no-profile", action="store_true", help="skip the per-attribute profiling queries")
    sp.add_argument("--where", default="", help="SQL condition that scopes profiling on large models")
    sp.add_argument("--sample-max", type=int, default=30,
                    help="list the values of attributes with at most this many distinct values (default 30)")
    sp.add_argument("--max-attributes", type=int, default=200, help="profile at most this many attributes")
    sp.add_argument("--profile-batch", type=int, default=8, help="attributes profiled per query (default 8)")

    sp = sub.add_parser("run", help="run a question bank through the MCP query tool")
    sp.add_argument("--project", required=True)
    sp.add_argument("--bank", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--only", default="", help="comma-separated question ids")
    sp.add_argument("--levels", default="", help="comma-separated levels, e.g. 1,2")
    sp.add_argument("--max-rows", type=int, default=1000, help="rows kept per question (default 1000)")

    sp = sub.add_parser("relationships", help="test parent-child dependencies between attributes in the data")
    sp.add_argument("--project", required=True)
    sp.add_argument("--scope", required=True, help="scope.json from the scope command (with profiles)")
    sp.add_argument("--out", required=True)
    sp.add_argument("--entity", default="", help="entity key attribute (default: guessed from the profiles)")
    sp.add_argument("--where", default="", help="SQL condition that scopes the tests on large models")
    sp.add_argument("--max-pairs", type=int, default=120, help="child-parent pairs to test besides the entity tests")
    sp.add_argument("--batch", type=int, default=10, help="pairs per query (default 10)")
    sp.add_argument("--declared", help="JSON list of the model's declared relationships: [{child, parent}]")

    sp = sub.add_parser("lookups", help="recommend lookup tables and write CSV templates for them")
    sp.add_argument("--project", required=True)
    sp.add_argument("--scope", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--csv-dir", required=True, help="folder for the CSV templates")
    sp.add_argument("--relationships", help="relationships.json, to consider an entity table")
    sp.add_argument("--period", default="", help="period attribute (default: guessed)")
    sp.add_argument("--where", default="")
    sp.add_argument("--max-code-values", type=int, default=200,
                    help="treat text attributes with at most this many values as codes (default 200)")

    sp = sub.add_parser("grade", help="compare results with expected values; summarize by level and path")
    sp.add_argument("--bank", required=True)
    sp.add_argument("--results", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--tolerance", type=float, default=None,
                    help="relative tolerance when a question sets none (default 1e-6)")
    sp.add_argument("--previous", help="an earlier graded.json: report changed verdicts and regressions")

    sp = sub.add_parser("plan", help="order approved findings into phases with commands and test plans")
    sp.add_argument("--findings", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--model-id", default="", help="model objectId for the generated commands")
    sp.add_argument("--project", default="", help="project for the regression commands")
    sp.add_argument("--bank", default="question_bank.json", help="question bank path for the regression commands")
    sp.add_argument("--status", default=",".join(PLANNED_STATUSES),
                    help="finding statuses to plan (default approved,modified)")
    sp.add_argument("--python", default="python3", help="interpreter in the generated commands")
    sp.add_argument("--quoting", choices=["posix", "powershell"], default="posix",
                    help="quoting for the display form of each command")

    sp = sub.add_parser("check", help="validate question bank, findings and domain notes files; with --bank "
                        "and --domain, also check that every use case has questions")
    sp.add_argument("--bank")
    sp.add_argument("--findings")
    sp.add_argument("--domain")
    return ap


def main(argv: list[str] | None = None, runner_factory: Callable[[argparse.Namespace], Runner] | None = None) -> int:
    args = build_parser().parse_args(argv)

    def log(msg: str) -> None:
        print(f"[assess] {msg}", file=sys.stderr)

    made: list[Runner] = []

    def runner() -> Runner:
        if runner_factory is not None:
            r = runner_factory(args)
        elif args.replies:
            r = ReplayRunner(args.replies)
        else:
            r = McpRunner(args.base, args.mcp_url, args.server)
        made.append(r)
        return r
    runner.made = made  # type: ignore[attr-defined]

    if args.cmd in ("scope", "run", "relationships", "lookups"):
        try:
            code = _live(args, runner, log)
            pending = [r for r in made if getattr(r, "pending", None)]
            if pending and hasattr(pending[0], "write_pending"):
                path = pending[0].write_pending()
                print(json.dumps({"ok": False, "pending": len(pending[0].pending), "pending_file": path,
                                  "next": "save the replies listed there, then run the same command again"},
                                 indent=2))
                return 3
            return code
        except PendingReply:
            r = made[-1]
            print(json.dumps({"ok": False, "pending": len(r.pending), "pending_file": r.write_pending(),
                              "next": "save the replies listed there, then run the same command again"}, indent=2))
            return 3
        except Exception as e:  # sign-in, MCP and network failures: one line, no traceback
            if type(e).__name__ in ("McpError", "AuthError") or isinstance(e, OSError) or \
                    type(e).__module__.startswith(("requests", "urllib3")):
                print(f"FATAL: {type(e).__name__}: {e}", file=sys.stderr)
                return 2
            raise
    return _offline(args)


def _live(args: argparse.Namespace, runner: Callable[[], Runner], log: Callable[[str], None]) -> int:
    if args.cmd == "scope":
        out = scope_model(runner(), args.project, args.model, profile=not args.no_profile, where=args.where,
                          sample_max=args.sample_max, max_attributes=args.max_attributes,
                          profile_batch=args.profile_batch, log=log)
        _write_json(args.out, out)
        if _pending(runner):
            return 3
        print(json.dumps({"ok": True, "out": args.out, "attributes": len(out["attributes"]),
                          "metrics": len(out["metrics"]), "maturity": out["maturity"]["estimated_level"],
                          **out["notes"]}, indent=2))
        return 0
    if args.cmd == "relationships":
        declared = _load_json(args.declared) if args.declared else None
        out = review_relationships(runner(), args.project, _load_json(args.scope), entity=args.entity,
                                   where=args.where, max_pairs=args.max_pairs, batch=args.batch,
                                   declared=declared, log=log)
        _write_json(args.out, out)
        if _pending(runner):
            return 3
        print(json.dumps({"ok": True, "out": args.out, "entity": out["entity"], "tested_pairs": out["tested_pairs"],
                          "parent_child": len(out["suggestions"]["parent_child"]),
                          "one_to_one": len(out["suggestions"]["same_entity_one_to_one"])}, indent=2))
        return 0
    if args.cmd == "lookups":
        scope = _load_json(args.scope)
        rel = _load_json(args.relationships) if args.relationships else None
        sugg = lookup_suggestions(scope, period=args.period, max_code_values=args.max_code_values, relationships=rel)
        built = build_lookups(runner(), args.project, scope, sugg, args.csv_dir, where=args.where)
        _write_json(args.out, {"version": 1, "model": scope.get("model"), "lookups": built})
        if _pending(runner):
            return 3
        print(json.dumps({"ok": True, "out": args.out, "suggestions": len(built),
                          "csv_files": [b["csv"] for b in built if b.get("csv")]}, indent=2))
        return 0
    if args.cmd == "run":
        bank = _load_json(args.bank)
        problems = check_bank(bank)["problems"]
        if problems:
            print(json.dumps({"ok": False, "problems": problems}, indent=2))
            return 2
        out = run_bank(runner(), args.project, bank, only=args.only, levels=args.levels,
                       max_rows=args.max_rows, log=log)
        _write_json(args.out, out)
        if _pending(runner):
            return 3
        print(json.dumps({"ok": True, "out": args.out, **out["summary"]}, indent=2))
        return 0
    return 2


def _pending(runner: Callable[[], Runner]) -> bool:
    """True when the runner made for this command is a replay runner with replies still missing."""
    made = getattr(runner, "made", [])
    return any(getattr(r, "pending", None) for r in made)


def _offline(args: argparse.Namespace) -> int:
    if args.cmd == "grade":
        bank = _load_json(args.bank)
        out = grade_bank(bank, _load_json(args.results), bank_dir=os.path.dirname(os.path.abspath(args.bank)),
                         tolerance=args.tolerance, previous=_load_json(args.previous) if args.previous else None)
        _write_json(args.out, out)
        report = {"ok": True, "out": args.out, "by_verdict": out["summary"]["by_verdict"]}
        if "regressions" in out:
            report["regressions"] = out["regressions"]
        print(json.dumps(report, indent=2))
        return 0
    if args.cmd == "plan":
        statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
        bad = [s for s in statuses if s not in STATUSES]
        if bad:
            print(json.dumps({"ok": False, "problems": [f"unknown status {bad}"]}, indent=2))
            return 2
        out = build_plan(_load_json(args.findings), model_id=args.model_id, statuses=statuses,
                         python=args.python, quoting=args.quoting, project=args.project, bank=args.bank)
        _write_json(args.out, out)
        print(json.dumps({"ok": not out["problems"], "out": args.out, "phases": [p["phase"] for p in out["phases"]],
                          "steps": sum(len(p["steps"]) for p in out["phases"]),
                          "problems": out["problems"], "warnings": out["warnings"]}, indent=2))
        return 1 if out["problems"] else 0
    # check
    if not (args.bank or args.findings or args.domain):
        print(json.dumps({"ok": False, "problems": ["pass --bank, --findings and/or --domain"]}, indent=2))
        return 2
    report: dict[str, Any] = {}
    if args.bank:
        report["bank"] = check_bank(_load_json(args.bank))
    if args.findings:
        report["findings"] = check_findings(_load_json(args.findings))
    if args.domain:
        report["domain"] = check_domain(_load_json(args.domain))
    if args.bank and args.domain:
        report["coverage"] = check_coverage(_load_json(args.bank), _load_json(args.domain))
    ok = not any(r["problems"] for r in report.values())
    print(json.dumps({"ok": ok, **report}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
