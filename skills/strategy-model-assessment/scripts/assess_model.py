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
import codecs
import copy
import csv
import hashlib
import datetime
import io
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
                "create_level_metric", "create_weighted_average", "patch_model_object", "patch_fact_metrics",
                "add_lookup_table", "change_column_type", "source_change", "wire_relationships",
                "create_formula_attribute", "add_column_attribute", "add_time_attributes", "rename_describe",
                "show_forms", "publish", "certify", "manual")
COLUMN_TYPES = ("text", "integer", "bigint", "double", "date", "timestamp")   # build_mosaic.py set-column-types
# Changes made once for the whole plan, at the start of one phase; other phases get a note.
MERGED_KINDS = {"source_change": 1, "change_column_type": 2, "wire_relationships": 3, "add_time_attributes": 3,
                "rename_describe": 6}
MERGED_NOTES = {"source_change": "Source change done in the phase {phase} rebuild",
                "change_column_type": "Column type change done in phase {phase} (set-column-types)",
                "wire_relationships": "Relationships declared in phase {phase} (wire-relationships)",
                "add_time_attributes": "Time attributes added in phase {phase} (add-time-attributes)",
                "rename_describe": "Names and descriptions set in phase {phase} (rename-describe)"}
TIME_ATTRIBUTES_SPEC = "specs/time_attributes.json"
RENAME_DESCRIBE_SPEC = "specs/rename_describe.json"
# A republish of a large model runs well past publish's 180 s default; the wait ends as soon as every table is done.
PUBLISH_WAIT = ["--poll-seconds", "1800"]
DESCRIPTION_LIMIT = 250
_TEST_STEP = re.compile(r"^\s*(after (the )?(republish|reload|type change)\w*,?\s*)?(re-?)?(test|check|verify|confirm)\b", re.I)
RELATIONSHIP_TYPES = ("one_to_many", "many_to_many", "one_to_one")
RELATIONSHIP_HINTS = "specs/relationships.json"   # written into the bundle by plan; scripts run from the repo root
VERIFIED_FORMULA_FUNCTIONS = {"addmonths", "year", "monthsbetween", "case"}   # create-formula-attribute, accepted live
MISSING_FORMULA_FUNCTIONS = {"sign"}      # refused on a tenant ("Searching for '<name>' resulted in no matches")
_DISPLAY_COLUMN = re.compile(r"\b(description|desc|name|label|title|text)$", re.I)
# A source_change whose text describes a type change: that is change_column_type, done in the model.
_TYPE_CHANGE_TEXT = re.compile(r"\b(data ?type|column type|varchar|utf8|retype)\b|\b(load|reload|re-load|read|cast|"
                               r"convert|change|store)\w*\b[^.;]*\b(as|to|into)\s+(a\s+)?(text|string|varchar|double|"
                               r"integer|int|bigint|number|numeric|date|timestamp)\b", re.I)


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
        os.makedirs(self.folder, exist_ok=True)   # a first run into a new replies folder
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
    if not profile:   # no attribute profiles, but still the anchor (one cheap COUNT query per eight metrics)
        out["anchor"] = choose_anchor(runner, project, model, semantics["metrics"], where, profile_batch)
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
    out["anchor"] = choose_anchor(runner, project, model, semantics["metrics"], where, profile_batch)
    out["maturity"] = maturity_signals(out)
    return out


def raw_fact_metrics(metrics: list[dict]) -> list[str]:
    """Raw numeric fact metrics (one column, one aggregation): the ones that read fact rows directly."""
    return [m["name"] for m in metrics if m.get("formula") and _RAW_METRIC.match(m["formula"].strip())
            and (m.get("type") or "").lower() in _NUMERIC]


def anchor_sql(model: str, metrics: list[str], where: str = "") -> str:
    cols = ", ".join(f"CAST(COUNT({quote_ident(n)}) AS VARCHAR) AS c{i}" for i, n in enumerate(metrics))
    clause = f" WHERE {where}" if where else ""
    return f"SELECT {cols} FROM {quote_model(model)}{clause}"  # nosec B608  # identifiers are quoted; the connector is read-only


def choose_anchor(runner: Runner, project: str, model: str, metrics: list[dict], where: str = "",
                  batch: int = 8) -> dict[str, Any] | None:
    """The fullest raw fact metric, to count fact rows with. An aggregate over attributes only runs on the
    attributes' lookups (real or virtual), not on the fact, so row counts and conditions in generated SQL
    use COUNT(<anchor>) / COUNT(CASE WHEN ... THEN <anchor> END). COUNT skips blanks, hence the fullest."""
    names = raw_fact_metrics(metrics)
    counts: dict[str, float] = {}
    for start in range(0, len(names), max(batch, 1)):
        chunk = names[start:start + max(batch, 1)]
        try:
            parsed = parse_query_result(runner.call("query", {"project": project,
                                                              "query": anchor_sql(model, chunk, where)}))
        except PendingReply:
            continue
        if parsed["ok"] and parsed["rows"]:
            row = parsed["rows"][0]
            for i, n in enumerate(chunk):
                v = _num(row.get(f"c{i}"))
                if v is not None:
                    counts[n] = v
    if not counts:
        return None
    best = max(counts.values())
    name = next(n for n in names if counts.get(n) == best)    # first of the fullest, in model order
    return {"metric": name, "non_blank": best,
            "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "note": "Count fact rows with COUNT(<anchor>), and conditions with COUNT(CASE WHEN ... THEN <anchor> "
                    "END). Aggregates over attributes only read the attributes' lookups, not the fact. A model "
                    "with several fact tables needs one anchor per fact."}


# one aggregation of one column: "sum(table.csv.{Net Sales})", or with no braces when the column has no space
# ("sum(retail_orders.xlsx.Quantity)")
# a table must come before the column: "Sum({Other Metric})" is a metric built on a metric, not a fact
_RAW_METRIC = re.compile(r"^(sum|avg|min|max|count)\(\s*[^(){}]+\.(\{[^{}]+\}|[A-Za-z_]\w*)\s*\)$", re.I)
_DEFAULT_NAME = re.compile(r"^(total|sum of|average|avg|count of|count)\b.*\(.*\)$|^[a-z0-9]+(_[a-z0-9]+)+$", re.I)
_ATTRIBUTE_LIKE = re.compile(r"\b(id|code|key|number|zip|postal|year|month|flag|indicator|status|type)\b", re.I)
_NUMERIC = ("bigint", "integer", "int", "double", "decimal", "real", "float", "smallint", "tinyint")
def _weighted_part(m: dict) -> bool:
    """A weighted average's numerator (Sum(rate * weight)) or denominator is meant to be summed."""
    return bool(re.search(r"\b(numerator|denominator|weight|weighted)\b", _words(m.get("name") or ""))
                or "*" in (m.get("formula") or ""))


_SUMMED_NONADDITIVE = re.compile(r"\b(rate|ratio|score|percent|percentage|pct|age|months|term|price|index|"
                                 r"average|avg|mean|latitude|longitude)\b", re.I)


def _plain_number(value: Any) -> str:
    return str(value or "").replace(",", "").strip()


_TEXT_TYPES = ("varchar", "char", "string", "text")
_DATE_NAME = re.compile(r"\b(date|period|month|day|as of|asof|effective|yyyymm|dt|yearmonth|year month)\b", re.I)
_ID_NAME = re.compile(r"\b(id|key|number|no|nbr|num|code|sequence|seq)\b", re.I)
_DATE_TEXT = re.compile(r"\b(date|month|period|YYYY-?MM(-?DD)?)\b", re.I)


def _value_pattern(value: str, numeric: bool) -> str:
    """The date pattern one value follows, checked as a real calendar date; '' when it isn't one."""
    v = _plain_number(value) if numeric else str(value).strip()
    if numeric and re.fullmatch(r"\d+\.0+", v):
        v = v.split(".")[0]
    for pattern, rx, fmt in (("yyyyMM", r"\d{6}", "%Y%m"), ("yyyyMMdd", r"\d{8}", "%Y%m%d"),
                             ("yyyy-MM", r"\d{4}-\d{2}", "%Y-%m"), ("yyyy-MM-dd", r"\d{4}-\d{2}-\d{2}", "%Y-%m-%d")):
        if numeric and "-" in pattern:
            continue
        if re.fullmatch(rx, v):
            try:
                d = datetime.datetime.strptime(v, fmt)
            except ValueError:
                return ""
            return pattern if 1900 <= d.year <= 2199 else ""
    return ""


def stored_date_pattern(attr: dict) -> str:
    """The pattern of a date stored as a number or as text (yyyyMM, yyyyMMdd; text also yyyy-MM, yyyy-MM-dd),
    or ''. Every sampled value and the min and max must be valid dates of one pattern (a 6- or 8-digit code
    can match at its extremes only by chance), and a second signal is needed: a date-like name (date,
    period, month, as of, effective), or a description that says it is a date. A name that reads as an
    identifier (id, key, number, code, sequence) needs the description."""
    typ = (attr.get("type") or "").lower()
    numeric, text = typ in _NUMERIC, typ in _TEXT_TYPES
    if not (numeric or text):
        return ""
    desc = attr.get("description") or ""
    words = _words(attr.get("name") or "")
    described = bool(_DATE_TEXT.search(desc))
    named = bool(_DATE_NAME.search(words)) and not _ID_NAME.search(words)
    prof = attr.get("profile") or {}
    values = [v for v in list(prof.get("values") or []) + [prof.get("min"), prof.get("max")]
              if v not in (None, "") and str(v).strip().lower() not in ("null", "none")]
    if numeric:   # a NULL rendered as 0 by CAST is not a value
        values = [v for v in values if _plain_number(v) not in ("0", "0.0")]
    patterns = {_value_pattern(v, numeric) for v in values}
    if values and len(patterns) == 1 and "" not in patterns and len({str(v) for v in values}) >= 2:
        return next(iter(patterns)) if (named or described) else ""
    hit = re.search(r"\bYYYY-?MM(-?DD)?\b", desc)
    if not values and hit and (named or described):
        return ("yyyyMMdd" if hit.group(1) else "yyyyMM") if numeric else ("yyyy-MM-dd" if hit.group(1) and "-" in hit.group(0)
                                                                          else "yyyyMMdd" if hit.group(1) else
                                                                          "yyyy-MM" if "-" in hit.group(0) else "yyyyMM")
    return ""


def looks_like_integer_date(attr: dict) -> bool:
    """A number stored as a date (see stored_date_pattern)."""
    return (attr.get("type") or "").lower() in _NUMERIC and bool(stored_date_pattern(attr))


def looks_like_stored_date(attr: dict) -> bool:
    """A date stored as a number or as text."""
    return bool(stored_date_pattern(attr))


_HELPER_METRIC = re.compile(r"\b(numerator|denominator|helper)\s*$", re.I)
TIME_INTERVALS = ("year", "quarter", "month", "week", "day")


def has_time_attributes(name: str, all_names: list[str]) -> bool:
    """Studio's time attributes show on the connector as '<date> (<interval>)__id / __start / __desc'."""
    pre = name.lower() + " ("
    return any(n.lower().startswith(pre) and n.lower().endswith(")__id")
               and n.lower()[len(pre):-len(")__id")] in TIME_INTERVALS for n in all_names)


def date_pattern(attr: dict) -> str:
    """The pattern a date stored as a number or text follows (for set-column-types ... date <pattern>)."""
    return stored_date_pattern(attr)


def maturity_signals(scope: dict) -> dict[str, Any]:
    """Signals from the scope and an estimated maturity level (memory/reference_model_assessment.md
    "Maturity levels"). An estimate to confirm during the assessment, not a grade."""
    attrs = scope.get("attributes") or []
    metrics = scope.get("metrics") or []
    names = [a["name"].lower() for a in attrs]
    described = sum(1 for o in attrs + metrics if (o.get("description") or "").strip())
    total = len(attrs) + len(metrics) or 1
    derived = [m["name"] for m in metrics if m.get("formula") and not _RAW_METRIC.match(m["formula"].strip())
               and not _HELPER_METRIC.search(m["name"])]   # a weighted average's helpers are one metric, not three
    default_named = [o["name"] for o in attrs + metrics if _DEFAULT_NAME.match(o["name"].strip())]
    attribute_like_metrics = [m["name"] for m in metrics if _ATTRIBUTE_LIKE.search(_words(m["name"]))
                              and m.get("formula") and _RAW_METRIC.match(m["formula"].strip())]
    numeric_attributes = [a["name"] for a in attrs if a.get("type", "").lower() in _NUMERIC]
    decoded = any(re.search(r"(__|\b)(description|desc|label)\b", n) for n in names)
    latest_flag = any(("latest" in n or "current period" in n or "is current" in n) for n in names)
    names = names + [n.split("__", 1)[0] for n in names if "__" in n]
    constants = [a["name"] for a in attrs if (a.get("profile") or {}).get("distinct_values") == 1]
    # A date stored as a number (yyyyMM, yyyyMMdd) is a defect whatever parents it has: converted to a date it
    # takes time attributes (year, quarter, month intervals) that group correctly and enable time-intelligence
    # metrics; lookups for its parents are the fallback when it can't be converted.
    integer_dates = [a["name"] for a in attrs if looks_like_integer_date(a)]
    text_dates = [a["name"] for a in attrs if (a.get("type") or "").lower() in _TEXT_TYPES and looks_like_stored_date(a)]
    summed_nonadditive = [m["name"] for m in metrics if m.get("formula") and
                          re.match(r"^sum\(", m["formula"].strip(), re.I) and _SUMMED_NONADDITIVE.search(_words(m["name"]))
                          and not _weighted_part(m)]
    text_metrics = [m["name"] for m in metrics if m.get("type", "").lower() in ("varchar", "char", "string", "text")]
    share = round(described / total, 3)
    structural = len(summed_nonadditive) >= 3 or len(integer_dates) + len(text_dates) >= 2 or bool(text_metrics)
    if not derived:
        level = "M0" if share < 0.5 or structural else "M1"
    elif len(derived) < 5:
        level = "M2" if decoded else "M1"
    else:
        level = "M4" if (share >= 0.9 and latest_flag and decoded) else "M3"
    if structural and level in ("M3", "M4"):
        level = "M2"   # governed metrics on top of structural defects (summed scores, numeric dates, text amounts)
    return {"estimated_level": level, "described_share": share, "derived_metrics": len(derived),
            "raw_metrics": len(metrics) - len(derived), "default_named_objects": default_named,
            "metrics_that_look_like_attributes": attribute_like_metrics,
            "numeric_attributes": numeric_attributes, "decoded_codes": decoded,
            "latest_period_flag": latest_flag, "single_value_attributes": constants,
            "dates_stored_as_numbers": integer_dates, "dates_stored_as_text": text_dates,
            "dates_without_time_attributes": [a["name"] for a in attrs if "__" not in a["name"]
                                              and (a.get("type", "").lower() in ("date", "timestamp")
                                                   or looks_like_stored_date(a))
                                              and not has_time_attributes(a["name"], [x["name"] for x in attrs])],
            "summed_rates_scores_or_ages": summed_nonadditive,
            "metrics_stored_as_text": text_metrics,
            "note": "estimate from names, descriptions and formulas; confirm during the assessment"}


# ── roles: columns read as the wrong kind of object ─────────────────────────────────────────
# A numeric column becomes a fact metric in an automated build. Some of those are attributes (codes, flags, small
# counts a user groups by), some are entity characteristics (one value per entity, repeated on every row of a
# snapshot, so a Sum across rows multiplies them), and some continuous values sit only in an attribute. The
# checks read the data, not just the names: integer range, share of non-integer values, and variation within
# the entity.

ROLE_LOW_SPAN = 30            # an integer measure spanning at most this many values may be a code, a band or a count
ROLE_CONSTANT_SHARE = 0.01    # at most this share of entities with more than one value = one value per entity
ROLE_MEASURE_DISTINCT = 1000  # a numeric attribute with at least this many values may be a measure
_COUNT_LIKE = re.compile(r"\b(number of|count|num of|units|unit count|qty|quantity|items|lines)\b", re.I)
_CODE_LIKE = re.compile(r"\b(id|code|key|zip|postal|flag|indicator|status|type|class|category|band|bucket|"
                        r"tier|grade|year|month|quarter|week|day|number)\b", re.I)


def roles_profile_sql(model: str, metrics: list[str], where: str = "") -> str:
    parts = []
    for i, name in enumerate(metrics):
        col = quote_ident(name)
        parts += [f"CAST(MIN({col}) AS VARCHAR) AS n{i}", f"CAST(MAX({col}) AS VARCHAR) AS x{i}",
                  f"CAST(COUNT({col}) AS VARCHAR) AS c{i}",
                  f"CAST(COUNT(CASE WHEN {col} <> FLOOR({col}) THEN {col} END) AS VARCHAR) AS f{i}"]
    clause = f" WHERE {where}" if where else ""
    return f"SELECT {', '.join(parts)} FROM {quote_model(model)}{clause}"  # nosec B608  # identifiers are quoted; the connector is read-only


def roles_variation_sql(model: str, entity: str, metrics: list[str], where: str = "", anchor: str = "") -> str:
    """Entities, fact rows (counted with the anchor metric), and per metric the entities whose rows hold more
    than one value."""
    inner = ", ".join(f"MAX({quote_ident(n)}) - MIN({quote_ident(n)}) AS v{i}" for i, n in enumerate(metrics))
    outer = ", ".join(f"CAST(SUM(CASE WHEN v{i} > 0 THEN 1 ELSE 0 END) AS VARCHAR) AS v{i}" for i in range(len(metrics)))
    clause = f" WHERE {where}" if where else ""
    return (f"SELECT CAST(COUNT(*) AS VARCHAR) AS entities, CAST(SUM(rows_) AS VARCHAR) AS row_count, {outer} "  # nosec B608  # identifiers are quoted; the connector is read-only
            f"FROM (SELECT {quote_ident(entity)} AS e_, COUNT({quote_ident(anchor or metrics[0])}) AS rows_, {inner} "
            f"FROM {quote_model(model)}{clause} "
            f"GROUP BY 1) t")


def _num(v: Any) -> float | None:
    try:
        return float(_plain_number(v))
    except (TypeError, ValueError):
        return None


def classify_roles(scope: dict, profiles: dict[str, dict], variation: dict[str, Any] | None, *,
                   entity: str = "", low_span: int = ROLE_LOW_SPAN) -> list[dict[str, Any]]:
    """-> candidates [{name, object, role, reason, evidence, suggested_actions}]. Pure: the queries are run by
    review_roles. Roles: attribute (a code or flag read as a measure), both (a small count or a 0/1 flag: group
    by it and add it up), entity_characteristic (one value per entity on a multi-row fact), measure (continuous
    values held only as an attribute), review (a small integer range the names don't explain)."""
    out: list[dict[str, Any]] = []
    rows_per_entity = None
    if variation and variation.get("entities"):
        rows_per_entity = (variation.get("row_count") or 0) / variation["entities"]
    multi_row = rows_per_entity is not None and rows_per_entity >= 1.5
    metric_names = {m["name"].lower() for m in scope.get("metrics") or []}
    for name, p in profiles.items():
        if p.get("error") or p.get("pending"):
            continue
        lo, hi, n, frac = _num(p.get("min")), _num(p.get("max")), _num(p.get("non_blank")), _num(p.get("non_integer"))
        if lo is None or hi is None or not n:
            continue
        integer, span = frac == 0, hi - lo
        words = _words(name)
        varying = ((variation or {}).get("varying") or {}).get(name)
        constant = multi_row and varying is not None and varying <= ROLE_CONSTANT_SHARE * variation["entities"]
        ev = {"min": lo, "max": hi, "non_blank": n, "integer": integer,
              "entities_with_several_values": varying, "rows_per_entity": round(rows_per_entity, 2) if rows_per_entity else None}
        role, reason, acts = None, "", []
        attr_act = {"kind": "add_column_attribute",
                    "args": {"column": name, "name": f"<{name} as an attribute: a business name, not the metric's>"}}
        level_act = {"kind": "create_level_metric",
                     "args": {"name": f"<{name}, once per entity>", "source_metric": name, "entity_attribute": entity,
                              "inner": "Max", "outer": "Sum"}}
        if integer and span <= 1:
            role, reason = "both", ("a 0/1 flag read as a measure: an attribute to filter on, and its sum counts the "
                                    "rows flagged")
            acts = [attr_act]
        elif integer and span <= low_span:
            if _COUNT_LIKE.search(words):
                role, reason = "both", ("a small count per row: group and filter by it as an attribute, add it up or "
                                        "average it as a measure")
                acts = [attr_act]
            elif _CODE_LIKE.search(words):
                role, reason = "attribute", ("an integer code, band or calendar part read as a measure: adding it up "
                                             "means nothing, and users can't group or filter by it")
                acts = [attr_act]
            else:
                role, reason = "review", (f"integers spanning {int(span) + 1} values: possibly a code, a band or a "
                                          f"small count. Check the data dictionary")
                acts = [attr_act]
        if constant:
            note = ("one value per entity, repeated on each of its rows: a Sum across rows multiplies it by the "
                    "rows per entity. Read it once per entity")
            if role:
                reason = f"{reason}; also {note}"
                acts = acts + [level_act]
            else:
                role, reason, acts = "entity_characteristic", note, [level_act]
        if role:
            out.append({"name": name, "object": "metric", "role": role, "reason": reason, "evidence": ev,
                        "suggested_actions": acts})
    for a in scope.get("attributes") or []:
        prof = a.get("profile") or {}
        name, typ = a["name"], (a.get("type") or "").lower()
        distinct = _num(prof.get("distinct_values"))   # a number, a "1,234" string, or an error text
        if typ not in _NUMERIC or name == entity or "__" in name or looks_like_integer_date(a):
            continue
        if _CODE_LIKE.search(_words(name)) or any(name.lower() in m for m in metric_names):
            continue
        decimal_type = typ in ("double", "decimal", "real", "float")
        if (decimal_type or (distinct or 0) >= ROLE_MEASURE_DISTINCT) and (distinct or 0) >= 50:
            out.append({"name": name, "object": "attribute", "role": "measure",
                        "reason": "continuous values held only as an attribute: users can group by them but not add "
                                  "them up or average them",
                        "evidence": {"type": typ, "distinct_values": distinct, "min": prof.get("min"),
                                     "max": prof.get("max")},
                        "suggested_actions": [{"kind": "manual", "steps": [
                            f"Add a fact metric on the column behind '{name}' (or a derived metric over the "
                            f"attribute), with the aggregation its meaning calls for"]}]})
    return out


def review_roles(runner: Runner, project: str, scope: dict, *, entity: str = "", where: str = "", batch: int = 8,
                 low_span: int = ROLE_LOW_SPAN, log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    model = scope["model"]
    entity = entity or guess_entity(scope)
    names = raw_fact_metrics(scope.get("metrics") or [])
    anchor = ((scope.get("anchor") or {}).get("metric")) or ""
    profiles: dict[str, dict[str, Any]] = {}
    variation: dict[str, Any] = {"varying": {}}
    pending = 0
    for start in range(0, len(names), max(batch, 1)):
        chunk = names[start:start + max(batch, 1)]
        log(f"roles: profile {len(chunk)} measures")
        for kind, sql in (("profile", roles_profile_sql(model, chunk, where)),
                          ("variation", roles_variation_sql(model, entity, chunk, where, anchor) if entity else "")):
            if not sql:
                continue
            try:
                parsed = parse_query_result(runner.call("query", {"project": project, "query": sql}))
            except PendingReply:
                pending += 1
                if kind == "profile":
                    profiles.update({n: {"pending": True} for n in chunk})
                continue
            row = parsed["rows"][0] if parsed["ok"] and parsed["rows"] else None
            if row is None:
                if kind == "profile":
                    profiles.update({n: {"error": parsed["error"] or "no rows"} for n in chunk})
                continue
            if kind == "profile":
                for i, n in enumerate(chunk):
                    profiles[n] = {"min": row.get(f"n{i}"), "max": row.get(f"x{i}"), "non_blank": row.get(f"c{i}"),
                                   "non_integer": row.get(f"f{i}")}
            else:
                variation["entities"] = _num(row.get("entities"))
                variation["row_count"] = _num(row.get("row_count"))
                for i, n in enumerate(chunk):
                    variation["varying"][n] = _num(row.get(f"v{i}"))
    candidates = classify_roles(scope, profiles, variation if variation.get("entities") else None,
                                entity=entity, low_span=low_span)
    return {"version": 1, "model": model, "entity": entity, "generated": time.strftime("%Y-%m-%d"),
            "measures_checked": len(names), "pending_queries": pending,
            "rows_per_entity": (round(variation["row_count"] / variation["entities"], 2)
                                if variation.get("entities") and variation.get("row_count") else None),
            "profiles": profiles, "candidates": candidates,
            "notes": ["Evidence from the data, not a verdict: the business meaning decides. A column can need both "
                      "roles (an attribute to group by and a measure to add up).",
                      "add_column_attribute keeps the fact metric and adds an attribute on the same column; give the "
                      "attribute a name that differs from every metric, or the connector shows two columns with "
                      "one name.",
                      "Fix roles before building metrics on these columns: a metric built on the wrong role is "
                      "rebuilt later."]}


# ── probe: automatic checks on attributes and metrics ───────────────────────────────────────
# Questions only find defects they happen to exercise. The probe exercises each chosen object in several query
# shapes and compares them, because the common defects only show in some shapes: a lookup missing a key drops
# rows once a filter or a second attribute joins the query; a Max metric looks additive when grouped and returns
# one entity's value once filtered; a metric on a mapping table ignores filters on the fact. Fact rows are
# counted with the anchor metric (an aggregate over attributes only reads lookups). Shapes:
#   filter  = WHERE <filter attribute> = <its latest or largest value>   (the period, when given)
#   partner = a second attribute with 3-30 values, grouped together with the object
# Checks:
#   attribute: rows kept alone / with the filter / with the partner (each against the matching anchor count);
#              values listed by an attribute-only query but with no fact rows (named, not counted);
#              a numeric attribute that is read from its lookup (COUNT over it far below the fact rows)
#   metric:    the filtered total differs from the filter's row in the grouped query (shape-dependent);
#              the filtered total equals the unfiltered one though the filter drops most rows (ignores filters);
#              the same value in every group, grouped and filtered; summed metrics whose groups don't add up;
#              a value for one period only without "latest" in its name or description

PROBE_MAX_VALUES = 200
_LATEST_WORDS = re.compile(r"\b(latest|current period|as of|point in time|most recent)\b", re.I)


def changed_objects(old_scope: dict | None, new_scope: dict, findings: dict | None = None,
                    phases: list[int] | None = None) -> dict[str, list[str]]:
    """Attributes and metrics added or changed since the old scope, plus objects named by the actions of findings
    applied in `phases`: what a phase touched, for verify-changes to probe."""
    def index(scope, key):
        return {o["name"]: (o.get("type"), o.get("formula"), o.get("description")) for o in (scope or {}).get(key) or []}
    out: dict[str, list[str]] = {}
    for key in ("attributes", "metrics"):
        old, new = index(old_scope, key), index(new_scope, key)
        out[key] = [n for n, sig in new.items() if old_scope is None or n not in old or old[n][:2] != sig[:2]]
    names = {"attributes": {a["name"].lower(): a["name"] for a in new_scope.get("attributes") or []},
             "metrics": {m["name"].lower(): m["name"] for m in new_scope.get("metrics") or []}}
    for f in (findings or {}).get("findings") or []:
        for a in f.get("actions") or []:
            if phases and action_phase(f, a) not in phases:
                continue
            args = a.get("args") or {}
            vals = [args.get(k) for k in ("name", "attribute", "column", "source_metric", "new_name")]
            vals += [n.get("name") if isinstance(n, dict) else n for n in args.get("new_attributes") or []]
            for v in vals:
                for key in ("attributes", "metrics"):
                    hit = names[key].get(str(v or "").lower())
                    if hit and hit not in out[key]:
                        out[key].append(hit)
    return out


_LATEST_FLAG = re.compile(r"\b(latest|current|most recent|last)\b", re.I)
_TRUE_VALUES = ("y", "yes", "1", "true", "t")
_TABLE_OF = re.compile(r"\(\s*([^(){}]*?)\.\{")


def _companion_attributes(scope: dict, period: str = "", split: str = "",
                          filter_spec: str = "") -> tuple[str, str | None, str]:
    """(filter attribute, filter value or None to choose from the data, partner attribute).
    Filter: --filter "<attribute>=<value>" if given; else a latest-period flag (two values, "latest" or
    "current" in its name), the filter analysts of a snapshot use and the one that exposes lookups missing
    keys; else the period (its latest value). Partner: --split, else another attribute with 3-30 values."""
    entity = guess_entity(scope)
    attrs = [a for a in scope.get("attributes") or [] if "__" not in a["name"] and a["name"] != entity]
    small = [a["name"] for a in attrs if 3 <= (_num((a.get("profile") or {}).get("distinct_values")) or 0) <= 30]
    value = None
    if filter_spec and "=" in filter_spec:
        filt, value = (x.strip() for x in filter_spec.split("=", 1))
    else:
        flag = next((a["name"] for a in attrs if _LATEST_FLAG.search(_words(a["name"]))
                     and _num((a.get("profile") or {}).get("distinct_values")) == 2), "")
        filt = flag or period or split or (small[0] if small else "")
    partner = split if split and split != filt else next((n for n in small if n not in (filt, period)), "")
    return filt, value, partner


def _close(a: float | None, b: float | None) -> bool:
    """Equal within the connector's display rounding (plain values show about 3 decimals)."""
    if a is None or b is None:
        return a == b
    return abs(a - b) <= max(5e-4 * max(abs(a), abs(b)), 0.005)


def _sql_literal(value: Any, typ: str = "") -> str:
    """A literal for a value read back as text: unquoted only for a numeric column (a varchar holding digits
    compared with a number is a type error)."""
    text = str(value)
    if (typ or "").lower() in _NUMERIC and re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?|-?\d+(\.\d+)?", text.strip()):
        return text.strip().replace(",", "")   # the connector shows thousands separators ("201,812")
    return "'" + text.replace("'", "''") + "'"


def _blank(v: Any) -> bool:
    return v is None or str(v).strip().lower() in ("", "null", "none")


_TABLE_OF_BARE = re.compile(r"^\w+\(\s*([^(){}]+)\.[A-Za-z_]\w*\s*\)$")


def _metric_table(m: dict) -> str:
    f = (m.get("formula") or "").strip()
    hit = _TABLE_OF.search(f) or _TABLE_OF_BARE.match(f)
    return hit.group(1).strip().lower() if hit else ""


def probe_objects(runner: Runner, project: str, scope: dict, *, attributes: list[str] | None = None,
                  metrics: list[str] | None = None, period: str = "", split: str = "", filter_spec: str = "",
                  where: str = "", max_values: int = PROBE_MAX_VALUES, batch: int = 6,
                  log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    model = scope["model"]
    anchor = (scope.get("anchor") or {}).get("metric")
    if not anchor:
        raise SystemExit("probe: scope.json has no anchor metric; run scope again (it picks one)")
    entity = guess_entity(scope)
    attrs_by = {a["name"]: a for a in scope.get("attributes") or []}
    mets_by = {m["name"]: m for m in scope.get("metrics") or []}
    if attributes is None:
        attributes = [n for n, a in attrs_by.items()
                      if "__" not in n and n != entity and
                      0 < (_num((a.get("profile") or {}).get("distinct_values")) or 0) <= max_values]
    if metrics is None:
        metrics = list(mets_by)
    M, A = quote_model(model), quote_ident(anchor)
    base = f" WHERE {where}" if where else ""

    def cond(extra: str) -> str:
        return f" WHERE {where} AND {extra}" if where else f" WHERE {extra}"

    pending = 0
    warnings: list[str] = []

    def ask(sql: str) -> dict | None:
        nonlocal pending
        try:
            parsed = parse_query_result(runner.call("query", {"project": project, "query": sql}))
        except PendingReply:
            pending += 1
            return None
        return parsed

    def rows_of(res: dict | None) -> list[dict] | None:
        return res["rows"] if res and res["ok"] else None

    def entities(where_sql: str) -> float | None:   # anchored: entities with fact rows
        if not entity:
            return None
        E = quote_ident(entity)
        sql = f"SELECT CAST(COUNT(*) AS VARCHAR) AS n FROM (SELECT {E} AS e_ FROM {M}{where_sql} GROUP BY 1 HAVING COUNT({A}) > 0) t"  # nosec B608  # identifiers are quoted; the connector is read-only
        r = rows_of(ask(sql))
        return _num(r[0].get("n")) if r else None

    filt, filt_value, partner = _companion_attributes(scope, period, split, filter_spec)
    total_r = rows_of(ask(f"SELECT CAST(COUNT({A}) AS VARCHAR) AS n FROM {M}{base}"))  # nosec B608
    total = _num(total_r[0].get("n")) if total_r else None
    filt_rows = None
    if filt and filt_value is None:
        fr = rows_of(ask(f"SELECT CAST({quote_ident(filt)} AS VARCHAR) AS v, CAST(COUNT({A}) AS VARCHAR) AS n "  # nosec B608
                         f"FROM {M}{base} GROUP BY 1"))
        filled = [(str(r.get("v")), _num(r.get("n")) or 0) for r in fr or []
                  if not _blank(r.get("v")) and _num(r.get("n"))]
        if filled:
            truthy = [v for v, _ in filled if v.strip().lower() in _TRUE_VALUES]
            if len(filled) <= 2 and truthy:          # a flag: its "yes" value
                filt_value = truthy[0]
            elif filt == period:                     # the latest period
                filt_value = max(filled, key=lambda x: x[0])[0]
            else:                                    # the largest group
                filt_value = max(filled, key=lambda x: x[1])[0]
    filt_type = (attrs_by.get(filt) or {}).get("type") or ""
    fcond = cond(f"{quote_ident(filt)} = {_sql_literal(filt_value, filt_type)}") if filt_value is not None else ""
    if fcond:   # what the filter keeps, counted directly: fact rows and entities
        fr = rows_of(ask(f"SELECT CAST(COUNT({A}) AS VARCHAR) AS n FROM {M}{fcond}"))  # nosec B608
        filt_rows = _num(fr[0].get("n")) if fr else None
    ent_total, ent_filtered = (entities(base), entities(fcond)) if fcond else (None, None)
    if partner and total:   # a partner that drops rows itself would blame every attribute grouped with it
        pr = rows_of(ask(f"SELECT CAST({quote_ident(partner)} AS VARCHAR) AS v, CAST(COUNT({A}) AS VARCHAR) AS n "  # nosec B608
                         f"FROM {M}{base} GROUP BY 1"))
        if pr is not None and sum(_num(r.get("n")) or 0 for r in pr) < total - 0.5:
            warnings.append(f"attribute '{partner}': grouping by it alone loses fact rows; not used as a partner")
            partner = ""

    # gaps: periods with no fact rows inside a partner value's range (a deal-month missing from the data)
    gaps: list[dict[str, Any]] = []
    if partner and period and partner != period:
        P, T = quote_ident(partner), quote_ident(period)
        sql = f"SELECT CAST({P} AS VARCHAR) AS p, CAST({T} AS VARCHAR) AS t, CAST(COUNT({A}) AS VARCHAR) AS n FROM {M}{base} GROUP BY 1, 2"  # nosec B608  # identifiers are quoted; the connector is read-only
        gr = rows_of(ask(sql))
        if gr:
            periods = sorted({str(r.get("t")) for r in gr if not _blank(r.get("t"))})
            have: dict[str, set[str]] = {}
            for r in gr:
                if not _blank(r.get("p")) and not _blank(r.get("t")) and (_num(r.get("n")) or 0) > 0:
                    have.setdefault(str(r.get("p")), set()).add(str(r.get("t")))
            for pv, ts in sorted(have.items()):
                lo, hi = periods.index(min(ts)), periods.index(max(ts))
                missing = [t for t in periods[lo:hi + 1] if t not in ts]
                if missing:
                    gaps.append({"partner": pv, "missing": missing})
                    warnings.append(f"'{partner}' = {pv}: no fact rows for {period} {', '.join(missing[:6])}"
                                    f"{' …' if len(missing) > 6 else ''}, inside its range (missing data, or a load gap)")

    attr_out = []
    for n in [x for x in attributes if x in attrs_by]:
        log(f"probe attribute {n}")
        a, N = attrs_by[n], quote_ident(n)
        item: dict[str, Any] = {"name": n, "issues": [], "shapes": {}}
        listing = rows_of(ask(f"SELECT CAST({N} AS VARCHAR) AS v FROM {M}{base} GROUP BY 1"))  # nosec B608  # attribute only: the lookup
        alone = rows_of(ask(f"SELECT CAST({N} AS VARCHAR) AS v, CAST(COUNT({A}) AS VARCHAR) AS n FROM {M}{base} GROUP BY 1"))  # nosec B608
        shapes = [("alone", alone, total)]
        if fcond and n != filt:
            sql = f"SELECT CAST({N} AS VARCHAR) AS v, CAST(COUNT({A}) AS VARCHAR) AS n FROM {M}{fcond} GROUP BY 1"  # nosec B608  # identifiers are quoted; the connector is read-only
            shapes.append((f"filtered to {filt} = {filt_value}", rows_of(ask(sql)), filt_rows))
        if partner and n != partner:
            P = quote_ident(partner)
            sql = f"SELECT CAST({P} AS VARCHAR) AS p, CAST({N} AS VARCHAR) AS v, CAST(COUNT({A}) AS VARCHAR) AS n FROM {M}{base} GROUP BY 1, 2"  # nosec B608  # identifiers are quoted; the connector is read-only
            shapes.append((f"grouped with {partner}", rows_of(ask(sql)), total))
        if alone is None and listing is None:
            item["pending" if pending else "error"] = True
            attr_out.append(item)
            continue
        for label, rows, expected in shapes:
            if rows is None or expected is None:
                continue
            kept = sum(_num(r.get("n")) or 0 for r in rows)
            item["shapes"][label] = {"rows": kept, "expected": expected}
            if kept < expected - 0.5:
                item["issues"].append(f"{label}: keeps {kept:,.0f} of {expected:,.0f} fact rows ({expected - kept:,.0f} "
                                      f"lost: a key its lookup lacks, often the blank one)")
        if listing is not None and alone is not None:
            with_data = {str(r.get("v")) for r in alone if (_num(r.get("n")) or 0) > 0 and not _blank(r.get("v"))}
            only_lookup = sorted({str(r.get("v")) for r in listing if not _blank(r.get("v"))} - with_data)
            item["lookup_only_values"] = only_lookup
            if only_lookup:
                shown = ", ".join(only_lookup[:12]) + (f" and {len(only_lookup) - 12} more" if len(only_lookup) > 12 else "")
                item["issues"].append(f"{len(only_lookup)} value(s) only in its lookup (listed, but no fact rows): {shown}")
        if (a.get("type") or "").lower() in _NUMERIC and not _CODE_LIKE.search(_words(n)) \
                and not looks_like_integer_date(a) and total:
            cr = rows_of(ask(f"SELECT CAST(COUNT({N}) AS VARCHAR) AS n FROM {M}{base}"))  # nosec B608
            counted = _num(cr[0].get("n")) if cr else None
            item["count_over_attribute"] = counted
            if counted is not None and counted < 0.5 * total:
                item["issues"].append(f"numeric attribute read from its lookup: COUNT over it gives {counted:,.0f}, the "
                                      f"fact has {total:,.0f} rows, so AVG or SUM over it averages the lookup's values")
        warnings += [f"attribute '{n}': {x}" for x in item["issues"]]
        attr_out.append(item)

    # Metrics are read plain, as analysts write them: wrapping an aggregate (CAST, ROUND, arithmetic) can bypass a
    # fact metric's own aggregation. The connector's thousands separators are parsed.
    anchor_table = _metric_table(mets_by.get(anchor) or {})
    met_out = []
    mnames = [n for n in metrics if n in mets_by]
    for start in range(0, len(mnames), max(batch, 1)):
        chunk = mnames[start:start + max(batch, 1)]
        log(f"probe {len(chunk)} metrics")
        plain = ", ".join(f"SUM({quote_ident(n)}) AS m{i}" for i, n in enumerate(chunk))
        wrapped = ", ".join(f"CAST(SUM({quote_ident(n)}) AS VARCHAR) AS m{i}" for i, n in enumerate(chunk))
        tot = rows_of(ask(f"SELECT {plain} FROM {M}{base}"))  # nosec B608
        tot_w = rows_of(ask(f"SELECT {wrapped} FROM {M}{base}"))  # nosec B608
        averages = [(i, n) for i, n in enumerate(chunk)
                    if (re.match(r"^\s*(\w+)\(", mets_by[n].get("formula") or "") or [None, ""])[1].lower()
                    in ("avg", "average", "mean")]
        tot_a = rows_of(ask("SELECT " + ", ".join(f"AVG({quote_ident(n)}) AS m{i}" for i, n in averages)  # nosec B608
                            + f" FROM {M}{base}")) if averages else None
        by_t = rows_of(ask(f"SELECT CAST({quote_ident(period)} AS VARCHAR) AS g, {plain} FROM {M}{base} GROUP BY 1")) if period else None  # nosec B608
        by_f = (by_t if filt == period else
                rows_of(ask(f"SELECT CAST({quote_ident(filt)} AS VARCHAR) AS g, {plain} FROM {M}{base} GROUP BY 1"))) if fcond else None  # nosec B608
        ftot = rows_of(ask(f"SELECT {plain} FROM {M}{fcond}")) if fcond else None  # nosec B608
        by_p = rows_of(ask(f"SELECT CAST({quote_ident(partner)} AS VARCHAR) AS g, {plain} FROM {M}{fcond or base} "  # nosec B608
                           f"GROUP BY 1")) if partner else None
        for i, n in enumerate(chunk):
            m = mets_by[n]
            key = f"m{i}"
            item = {"name": n, "issues": [], "filter": f"{filt} = {filt_value}" if fcond else None, "partner": partner or None}
            if tot is None:
                item["pending" if pending else "error"] = True
                met_out.append(item)
                continue
            t = _num(tot[0].get(key)) if tot else None
            t_w = _num(tot_w[0].get(key)) if tot_w else None
            item["total"], item["total_wrapped"] = t, t_w
            item["notes"] = []
            agg = (re.match(r"^\s*(\w+)\(", m.get("formula") or "") or [None, ""])[1].lower()
            if tot_w is not None and not _close(t or 0, t_w or 0):   # an empty column: NULL plain, 0 wrapped
                text = (f"wrapping changes its value: SUM gives {t}, CAST(SUM(...)) gives {t_w}; an expression "
                        f"around it bypasses its own aggregation ({agg or 'not a sum'})")
                if agg in ("avg", "average", "mean"):   # an average: expected; analysts must read it plain
                    item["notes"].append(text + ". Expected for an average: read it plain")
                else:
                    item["issues"].append(text + ". If a total is meant, the aggregation is wrong; if not, say "
                                                 "what it returns in its description")
            if tot_a is not None and agg in ("avg", "average", "mean"):
                t_avg = _num(tot_a[0].get(key))
                item["total_avg"] = t_avg
                if t is not None and t_avg is not None and abs(t - t_avg) >= 0.05 and float(t).is_integer() \
                        and int(t) == int(t_avg):
                    item["issues"].append(f"plain SUM reads it as a whole number, truncated ({t:g} for {t_avg}): its "
                                          f"data type ({m.get('type') or '?'}) drops the fraction. Set the metric's "
                                          f"type to a decimal, or read it with AVG(...) until then")
            groups_t = [(r.get("g"), _num(r.get(key))) for r in by_t or []]
            filled_t = [v for _, v in groups_t if v not in (None, 0)]
            groups_f = [(r.get("g"), _num(r.get(key))) for r in by_f or []]
            f_total = _num(ftot[0].get(key)) if ftot else None
            item["filtered_total"] = f_total
            in_grouped = next((v for g, v in groups_f if str(g) == str(filt_value)), None)
            item["filter_row_in_grouped"] = in_grouped
            if fcond and ftot is not None and by_f is not None and f_total is not None and in_grouped is not None \
                    and not _close(f_total, in_grouped):
                item["issues"].append(f"depends on the query's shape: {f_total} when filtered to {filt} = {filt_value}, "
                                      f"{in_grouped} in that row of the query grouped by {filt}")
            filled_f = [v for _, v in groups_f if v not in (None, 0)]
            kept_share = (ent_filtered / ent_total) if ent_total and ent_filtered is not None else \
                         ((filt_rows / total) if total and filt_rows is not None else None)
            if fcond and t and f_total is not None and _close(f_total, t) and kept_share is not None \
                    and kept_share < 0.9 and len(filled_f) >= 2:   # one filled group = that group only, not this
                item["issues"].append(f"ignores a filter that keeps {kept_share:.0%} of "
                                      f"{'entities' if ent_total else 'fact rows'}: same value ({t}) with and without "
                                      f"{filt} = {filt_value} (reads a lookup, or a level rule that ignores the filter)")
            additive = bool(m.get("formula") and re.match(r"^\s*sum\(", m["formula"], re.I)
                            and _RAW_METRIC.match(m["formula"].strip()))
            if additive and t is not None and filled_t and not _close(sum(filled_t), t):
                item["issues"].append(f"the groups by '{period}' add up to {sum(filled_t):,.2f}, not the total {t:,.2f}")
            if len(filled_t) >= 3 and len(set(filled_t)) == 1:
                item["issues"].append(f"returns the same value ({filled_t[0]:,.2f}) in every group of '{period}'")
            p_filled = [v for v in (_num(r.get(key)) for r in by_p or []) if v not in (None, 0)]
            if len(p_filled) >= 3 and len(set(p_filled)) == 1:
                item["issues"].append(f"returns the same value ({p_filled[0]:,.2f}) in every group of '{partner}'"
                                      + (f" when filtered to {filt} = {filt_value}" if fcond else "")
                                      + " (a Max where a Sum was meant, or a level rule that ignores the split)")
            if period and len(groups_t) >= 3 and len(filled_t) == 1 \
                    and not _LATEST_WORDS.search(f"{n} {m.get('description') or ''}"):
                item["issues"].append("returns a value for one period only, and neither its name nor its description "
                                      "says it is a latest-period metric")
            table = _metric_table(m)
            if table and anchor_table and table != anchor_table:
                item["issues"].append(f"reads table '{table}', not the fact table of the anchor ('{anchor_table}'): if "
                                      f"it is a lookup or mapping table, aggregating it reads that table's rows, not "
                                      f"the facts (fine if it is another fact table)")
            warnings += [f"metric '{n}': {x}" for x in item["issues"]]
            met_out.append(item)
    return {"version": 4, "model": model, "generated": time.strftime("%Y-%m-%d"), "anchor": anchor,
            "total_rows": total, "entities": ent_total,
            "filter": {"attribute": filt or None, "value": filt_value, "rows": filt_rows, "entities": ent_filtered},
            "partner": partner or None, "gaps": gaps, "pending_queries": pending,
            "attributes": attr_out, "metrics": met_out, "warnings": warnings,
            "notes": ["Each object is checked in several query shapes (alone, filtered, with a second attribute) "
                      "because the common defects only show in some of them.",
                      "Metrics are read plain, as analysts write them; the CAST variant is compared, not used.",
                      "Fact rows are counted with the anchor metric; attribute-only queries read lookups.",
                      "A warning is evidence to confirm, not a verdict: a metric with a deliberate level rule repeats "
                      "its value or ignores a filter by design (say so in its description)."]}


# ── run ──────────────────────────────────────────────────────────────────────────────────────

def _select(questions: list[dict], only: str = "", levels: str = "") -> list[dict]:
    ids = {s.strip() for s in only.split(",") if s.strip()}
    lv = {int(s) for s in levels.split(",") if s.strip()}
    return [q for q in questions if (not ids or q.get("id") in ids) and (not lv or q.get("level") in lv)]


CHARS_PER_TOKEN = 4   # rough English/SQL average; costs are estimates of connector I/O, not billed tokens


def query_cost(sql: str, reply_text: str) -> dict[str, int]:
    """What one connector query moved: the SQL sent and the reply received, with an estimated token count.
    Claude's own reasoning around the query is not included (a chat can't see its token counts)."""
    sent, received = len(sql or ""), len(reply_text or "")
    return {"queries": 1, "sql_chars": sent, "reply_chars": received,
            "est_tokens": -(-(sent + received) // CHARS_PER_TOKEN)}


def add_costs(*costs: dict | None) -> dict[str, int]:
    out = {"queries": 0, "sql_chars": 0, "reply_chars": 0, "est_tokens": 0}
    for c in costs:
        for k in out:
            out[k] += int((c or {}).get(k) or 0)
    return out


def _execute(runner: Runner, project: str, sql: str, max_rows: int) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        reply = runner.call("query", {"project": project, "query": sql})
        parsed = parse_query_result(reply)
        parsed["cost"] = query_cost(sql, tool_text(reply) or json.dumps(reply.get("structuredContent") or ""))
    except PendingReply:
        return {"ok": False, "pending": True, "error": "reply not saved yet", "columns": [], "rows": [],
                "row_count": 0, "truncated": False, "seconds": None}
    except Exception as e:  # network or MCP errors: record and keep going
        parsed = {"ok": False, "columns": [], "rows": [], "error": f"{type(e).__name__}: {e}"}
    rows = parsed["rows"]
    return {"ok": parsed["ok"], "error": parsed["error"], "columns": parsed["columns"],
            "rows": rows[:max_rows], "row_count": len(rows), "truncated": len(rows) > max_rows,
            "seconds": round(time.monotonic() - t0, 3), "cost": parsed.get("cost") or query_cost(sql, "")}


def model_description_cost(runner: Runner, project: str, model: str) -> dict[str, int] | None:
    """Size of the get_semantics reply: what every analyst session reads before its first query. It grows with
    each metric and description added, so it is tracked per round next to the answer costs."""
    try:
        reply = runner.call("get_semantics", {"project": project, "model": model})
    except PendingReply:
        return None
    except Exception:  # network or MCP errors: the questions still run
        return None
    if reply.get("isError"):
        return None
    text = tool_text(reply)
    return {"chars": len(text), "est_tokens": -(-len(text) // CHARS_PER_TOKEN)}


def run_bank(runner: Runner, project: str, bank: dict, *, only: str = "", levels: str = "",
             max_rows: int = 1000, log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    model = bank["model"]
    results = []
    semantics_cost = model_description_cost(runner, project, model)
    questions = _select(bank.get("questions") or [], only, levels)
    for i, q in enumerate(questions):
        log(f"run {i + 1}/{len(questions)}: {q.get('id')}")
        entry: dict[str, Any] = {"id": q.get("id"), "level": q.get("level")}
        best = q.get("best_answer") or {}
        if (best.get("sql") or "").strip():   # also for questions the governed path can't answer
            best_sql = svm.inject_model(best["sql"], model)
            entry["best_run"] = {"sql": best_sql, **_execute(runner, project, best_sql, max_rows)}
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
            "semantics_cost": semantics_cost, "results": results}


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
    one_to_one = [{"a": t["child"], "b": t["parent"], "evidence": t.get("evidence")} for t in tests
                  if t["kind"] == "pair" and t["verdict"] == "one_to_one"]
    m2o = [(t["child"], t["parent"]) for t in tests if t["kind"] == "pair" and t["verdict"] == "many_to_one"]
    evidence = {(t["child"], t["parent"]): t.get("evidence") for t in tests   # a failed query has none
                if t["kind"] == "pair" and t.get("verdict") != "error"}
    direct = [{"child": c, "parent": p, "evidence": evidence[(c, p)]} for c, p in _reduce(m2o)]
    out: dict[str, Any] = {"version": 1, "model": model, "entity": entity, "generated": time.strftime("%Y-%m-%d"),
                           "tested_pairs": pairs_tested, "pending_queries": pending, "tests": tests,
                           "failed_queries": sorted({t.get("error") or "" for t in tests if t.get("verdict") == "error"}),
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


# ── references and join paths ────────────────────────────────────────────────────────────────
# Tables and attributes change in many ways (a form added, a table replaced, an object renamed), so instead of
# predicting each edit, every round tests the outcome: are the names saved SQL uses still there, and does every
# attribute still join each fact table? With no join path, Mosaic cross-joins: the attribute's every value
# gets the fact table's full total, and a metric filtering on it filters nothing.

_FORM_NAME = re.compile(r"^(?P<base>.+?)(?:__(?P<f1>[^_][^()]*)| \((?P<f2>[^()]+)\))$")
_SQL_LITERAL = re.compile(r"('(?:[^']|'')*')")


def _object_ids(export: dict | None) -> dict[str, str]:
    """Object id -> name (lower case) for the export's attributes and metrics: a rename keeps the id."""
    model = _export_model(export)
    out: dict[str, str] = {}
    for key in ("attributes", "factMetrics", "metrics"):
        for o in model.get(key) or []:
            info = o.get("information") or {}
            oid = info.get("objectId") or o.get("id") or info.get("id")
            if oid and info.get("name"):
                out[str(oid)] = str(info["name"]).lower()
    return out


def _is_interval(name: str, base: str) -> bool:
    rest = name[len(base):]
    return any(rest.startswith(f" ({i})") for i in TIME_INTERVALS)


def reference_changes(old_scope: dict, new_scope: dict, *, export: dict | None = None,
                      old_export: dict | None = None) -> list[dict[str, Any]]:
    """Connector names (attributes, metrics) the old round had and this one doesn't, each with the name that
    replaced it when there is exactly one: the same object id renamed (both exports), the attribute's key
    form when a form was added (`attr` -> `attr__id`), or the plain name when forms were dropped."""
    renamed: dict[str, str] = {}
    if export and old_export:
        old_ids, new_ids = _object_ids(old_export), _object_ids(export)
        renamed = {old_ids[i]: new_ids[i] for i in old_ids if i in new_ids and old_ids[i] != new_ids[i]}
    key_forms: dict[str, str] = {}
    if export:
        for base, a in export_forms(new_scope, export).items():
            key = next((f for f in a["forms"] if f["key"] and f["visible"]), None)
            if key:
                key_forms[base.casefold()] = str(key["query_as"])
    out = []
    for kind in ("attributes", "metrics"):
        old = {o["name"].casefold(): o["name"] for o in old_scope.get(kind) or []}
        new = {o["name"].casefold(): o["name"] for o in new_scope.get(kind) or []}
        for k, name in old.items():
            if k in new:
                continue
            cands, how = [], ""
            if renamed.get(k) in new:
                cands, how = [new[renamed[k]]], "the same object, renamed (export ids)"
            elif kind == "attributes":
                if key_forms.get(k, "").casefold() in new:
                    cands, how = [new[key_forms[k].casefold()]], "the attribute's key form (export)"
                else:
                    forms = [n for kk, n in new.items() if (kk.startswith(k + "__") or kk.startswith(k + " ("))
                             and not _is_interval(kk, k)]
                    ids = [n for n in forms if re.search(r"(__id| \(id\))$", n.casefold())]
                    cands, how = (ids, "the attribute's id form") if len(ids) == 1 else (forms, "a form of the attribute")
                if not cands:
                    m = _FORM_NAME.match(name)
                    base = (m.group("base") if m else "").casefold()
                    if base and base in new and not _is_interval(k, base):
                        cands, how = [new[base]], "the attribute's plain name (its other forms were dropped)"
            out.append({"name": name, "kind": kind[:-1], "replacement": cands[0] if len(cands) == 1 else None,
                        "candidates": cands, "how": how or "no match"})
    return out


def replace_identifier(sql: str, old: str, new: str) -> tuple[str, int, int]:
    """Rewrite the quoted identifier "old" as "new" outside string literals. Returns (sql, quoted rewrites,
    unquoted mentions left alone: a bare word may be a keyword or another name, so a person checks it)."""
    parts = _SQL_LITERAL.split(sql)
    quoted = re.compile('"' + re.escape(old) + '"', re.I)
    bare = re.compile(r'(?<![\w".])' + re.escape(old) + r'(?![\w"(])', re.I) \
        if re.fullmatch(r"[A-Za-z_]\w*", old) else None
    done = left = 0
    for i in range(0, len(parts), 2):
        parts[i], n = quoted.subn(quote_ident(new).replace("\\", "\\\\"), parts[i])
        done += n
        if bare:
            left += len(bare.findall(parts[i]))
    return "".join(parts), done, left


def _saved_sql(q: dict) -> list[tuple[str, str]]:
    out = [("question", q.get("sql") or "")]
    out.append(("best_answer", (q.get("best_answer") or {}).get("sql") or ""))
    exp = q.get("expected") or {}
    if exp.get("source") == "same_model_sql":
        out.append(("expected", exp.get("sql") or ""))
    return [(w, t) for w, t in out if t.strip()]


def apply_reference_changes(bank: dict, changes: list[dict], *, round_no: int | None = None,
                            apply: bool = False) -> dict[str, Any]:
    """Find saved SQL (question, best answer, expected) naming a name the model no longer has. With apply,
    rewrite it when the name has exactly one replacement (reason kind model_change, history kept); the rest
    is unresolved: no replacement, several candidates, or an unquoted mention."""
    gone = [c for c in changes]
    rewritten, unresolved = [], []
    for q in bank.get("questions") or []:
        texts = dict(_saved_sql(q))
        new_texts = dict(texts)
        notes: dict[str, list[str]] = {}
        for c in gone:
            for where, text in list(new_texts.items()):
                probe_sql, n_quoted, n_bare = replace_identifier(text, c["name"], c["replacement"] or c["name"])
                if not n_quoted and not n_bare:
                    continue
                if c["replacement"] and n_quoted:
                    new_texts[where] = probe_sql
                    notes.setdefault(where, []).append(f'"{c["name"]}" -> "{c["replacement"]}" ({c["how"]})')
                if not c["replacement"] or n_bare:
                    unresolved.append({"question": q.get("id"), "where": where, "name": c["name"],
                                       "candidates": c["candidates"],
                                       "why": "unquoted mention: check it by hand" if c["replacement"] and n_bare
                                       else ("several candidates" if c["candidates"] else "no replacement found")})
        for where, text in new_texts.items():
            if text == texts.get(where):
                continue
            entry = {"question": q.get("id"), "where": where, "changes": notes.get(where, []), "sql": text}
            if apply:
                reason = "the model renamed: " + "; ".join(notes.get(where, []))
                if where == "question":
                    res = set_question_sql(bank, q["id"], sql=text, reason_kind="model_change", reason=reason,
                                           round_no=round_no)
                elif where == "best_answer":
                    res = set_best_answer(bank, q["id"], sql=text, reason_kind="model_change", reason=reason,
                                          round_no=round_no)
                else:
                    exp = q["expected"]
                    exp.setdefault("sql_history", []).append({"sql": exp.get("sql"), "until_round": round_no,
                                                              "reason_kind": "model_change", "reason": reason})
                    exp["sql"] = text
                    res = {"ok": True}
                if not res.get("ok"):
                    unresolved.append({"question": q.get("id"), "where": where, "name": ", ".join(notes.get(where, [])),
                                       "candidates": [], "why": "; ".join(res.get("problems") or [])})
                    continue
            rewritten.append(entry)
    return {"rewritten": rewritten, "unresolved": unresolved, "applied": apply}


def integrity_finding(fid: str, title: str, category: str, observed: str, recommendation: str, *,
                      round_no: int | None, questions: list[str] | None = None, impact: str = "high",
                      source: str = "", actions: list[dict] | None = None,
                      checks: list[dict] | None = None) -> dict[str, Any]:
    """A finding a person must answer: proposed and flagged needs_answer. Its actions, when the engine can
    work one out, are a proposal: approving the finding plans them, declining drops them. Its checks name
    the command whose next run shows it fixed (verify-changes marks it verified from that run)."""
    return {"id": fid, "title": title, "category": category, "impact": impact, "effort": "small",
            "evidence": {"questions": list(questions or []), "observed": observed},
            "recommendation": recommendation, "actions": list(actions or []), "risks": [], "depends_on": [],
            "tests": list(questions or []), "checks": list(checks or []), "status": "proposed",
            "needs_answer": True, "introduced_in_round": round_no, "source": source}


def reference_findings(changes: list[dict], usage: dict, round_no: int | None) -> list[dict[str, Any]]:
    by_name: dict[str, list[dict]] = {}
    for u in usage["unresolved"]:
        by_name.setdefault(u["name"], []).append(u)
    out = []
    for name, items in by_name.items():
        qs = sorted({str(u["question"]) for u in items})
        cands = next((u["candidates"] for u in items if u["candidates"]), [])
        out.append(integrity_finding(
            f"N-{_slug(name)}", f"Saved SQL names '{name}', which the model no longer has", "attribute",
            f"{len(qs)} question(s) ({', '.join(qs[:10])}) name '{name}' in {sorted({u['where'] for u in items})}: "
            + "; ".join(sorted({u['why'] for u in items})) + (f". Candidates: {cands}" if cands else ""),
            "Say which name replaces it (the SQL is then rewritten with reason kind model_change), or decline to "
            "rewrite the questions as not answerable. A best answer whose value moves after a rewrite needs your "
            "confirmation.", round_no=round_no, questions=qs, source="references"))
    return out


def _anchors_by_fact(scope: dict) -> list[dict[str, Any]]:
    """One anchor per fact table (the fullest raw fact metric reading it): an attribute can join one fact
    table and not another, so join paths are tested against each."""
    metrics = scope.get("metrics") or []
    raw = set(raw_fact_metrics(metrics))
    counts = (scope.get("anchor") or {}).get("counts") or {}
    by: dict[str, list[str]] = {}
    for m in metrics:
        if m["name"] in raw:
            by.setdefault(_metric_table(m), []).append(m["name"])
    out = [{"table": t or None, "anchor": max(names, key=lambda n: counts.get(n, -1))} for t, names in by.items()]
    if not out and (scope.get("anchor") or {}).get("metric"):
        out = [{"table": None, "anchor": scope["anchor"]["metric"]}]
    return out


def _export_fact_tables(model: dict) -> list[str]:
    """Tables the fact metrics read. Exports keep a fact metric's expressions either on it or under `fact`."""
    return sorted({str(t.get("name")) for fm in model.get("factMetrics") or []
                   for e in ((fm.get("fact") or {}).get("expressions") or []) + (fm.get("expressions") or [])
                   for t in e.get("tables") or [] if t.get("name")})


def _ref_name(ref: Any, ids: dict[str, str]) -> str:
    if isinstance(ref, str):
        return ids.get(ref) or ref.lower()
    if not isinstance(ref, dict):
        return ""
    info = ref.get("information") or {}
    oid = str(ref.get("objectId") or ref.get("id") or info.get("objectId") or "")
    return ids.get(oid) or str(ref.get("name") or info.get("name") or "").lower()


def _export_relationships(model: dict, export: dict | None) -> list[dict[str, str]]:
    """Declared parent-child relationships by attribute name: under `hierarchy.relationships` (as exports
    keep them) or on each attribute."""
    ids = _object_ids(export)
    found: list[Any] = []
    hier = model.get("hierarchy") or {}
    found += hier.get("relationships") or [] if isinstance(hier, dict) else []
    for a in model.get("attributes") or []:
        rr = a.get("relationships")
        found += (rr.get("relationships") if isinstance(rr, dict) else rr) or []
    rels = set()
    for r in found:
        if not isinstance(r, dict):
            continue
        p, c = _ref_name(r.get("parent"), ids), _ref_name(r.get("child"), ids)
        if p and c:
            rels.add((p, c, str(r.get("relationshipType") or r.get("type") or "")))
    return [{"parent": p, "child": c, "type": t} for p, c, t in sorted(rels)]


def _attribute_tables(model: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for a in model.get("attributes") or []:
        name = str((a.get("information") or {}).get("name") or "").lower()
        tabs = {str(t.get("name")) for f in a.get("forms") or [] for e in f.get("expressions") or []
                for t in e.get("tables") or [] if t.get("name")}
        lt = (a.get("attributeLookupTable") or {}).get("name")
        if lt:
            tabs.add(str(lt))
        if name and tabs:
            out[name] = tabs
    return out


def export_join_graph(export: dict | None) -> dict[str, Any]:
    """From the export: tables joined through attributes mapped to columns in both, which tables each
    attribute reaches, the fact tables it can't reach, and the declared relationships by name."""
    model = _export_model(export)
    if not model:
        return {}
    facts = _export_fact_tables(model)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    attr_tables = _attribute_tables(model)
    for tabs in attr_tables.values():
        first = sorted(tabs)[0]
        for t in tabs:
            parent[find(t)] = find(first)
    for t in facts:
        find(t)
    unreachable = []
    for name, tabs in sorted(attr_tables.items()):
        comps = {find(t) for t in tabs}
        missing = [f for f in facts if find(f) not in comps]
        if facts and missing:
            unreachable.append({"attribute": name, "tables": sorted(tabs), "facts_not_reached": missing,
                                "reaches_no_fact": len(missing) == len(facts)})
    tables = sorted({str((t.get("information") or {}).get("name") or t.get("name")) for t in model.get("tables") or []
                     if (t.get("information") or {}).get("name") or t.get("name")} | {x for v in attr_tables.values() for x in v})
    return {"fact_tables": facts, "tables": tables, "unreachable": unreachable,
            "relationships": _export_relationships(model, export)}


def join_path_sql(model: str, attribute: str, anchor: str, where: str = "") -> str:
    clause = f" WHERE {where}" if where else ""
    a = quote_ident(attribute)
    return (f"SELECT {a} AS v, COUNT({quote_ident(anchor)}) AS n FROM {quote_model(model)}{clause} "  # nosec B608  # identifiers are quoted; the connector is read-only
            f"GROUP BY {a} ORDER BY 2 ASC LIMIT 2")


def _typed_literal(value: Any, typ: str) -> str:
    """A literal of the attribute's type. The connector shows numbers with thousands separators
    ("201,812"), so a numeric value loses them and is written unquoted."""
    t = (typ or "").lower()
    if t in _NUMERIC or t.startswith(("decimal", "numeric")):
        raw = str(value).strip()
        if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?|-?\d+(\.\d+)?", raw):
            return raw.replace(",", "")
    text = str(value).replace("'", "''")
    if t.startswith("date"):
        return f"DATE '{text[:10]}'"
    if t.startswith("timestamp"):
        return f"TIMESTAMP '{text}'"
    return _sql_literal(value, typ)


def filter_test_sql(model: str, attribute: str, anchor: str, value: Any, typ: str, where: str = "") -> str:
    cond = f"{quote_ident(attribute)} = {_typed_literal(value, typ)}"
    return (f"SELECT COUNT({quote_ident(anchor)}) AS n FROM {quote_model(model)} "  # nosec B608  # identifiers are quoted; the value is a typed literal; the connector is read-only
            f"WHERE {cond}" + (f" AND ({where})" if where else ""))


_JOIN_ERROR = re.compile(r"\b(join|joins|joined|cross|path|relationship|unrelated|not related|no common)\b", re.I)


def _testable_attributes(scope: dict, export: dict | None) -> list[dict[str, Any]]:
    """One connector name per attribute (time intervals left out): its plain name, else its key form (the
    export's, else `__id` / `(id)`, else its first form). Attributes with one value are tested too: a blank
    is a second group. With the export, each attribute also carries the tables its forms read, so it is
    skipped only against a fact table it is mapped to directly."""
    attrs = scope.get("attributes") or []
    types = {a["name"]: a.get("type") or "" for a in attrs}
    interval = re.compile(r" \((" + "|".join(TIME_INTERVALS) + r")\)")
    names = [a["name"] for a in attrs if not interval.search(a["name"])]
    plain = {n.casefold() for n in names}
    key_forms: dict[str, str] = {}
    if export:
        for base, a in export_forms(scope, export).items():
            key = next((f for f in a["forms"] if f["key"] and f["visible"]), None)
            if key:
                key_forms[base.casefold()] = str(key["query_as"])
    by_base: dict[str, list[str]] = {}
    for n in names:
        m = _FORM_NAME.match(n)
        by_base.setdefault((m.group("base") if m else n).casefold(), []).append(n)
    tables = _attribute_tables(_export_model(export)) if export else {}
    out = []
    for base, members in by_base.items():
        if base in plain:
            pick = next(n for n in members if n.casefold() == base)
        elif key_forms.get(base, "").casefold() in {x.casefold() for x in members}:
            pick = next(n for n in members if n.casefold() == key_forms[base].casefold())
        else:
            ids = [n for n in members if re.search(r"(__id| \(id\))$", n.casefold())]
            pick = ids[0] if ids else sorted(members)[0]
        out.append({"name": pick, "attribute": base, "type": types.get(pick, ""),
                    "tables": sorted(tables[base]) if base in tables else None})
    return out


def _join_attributes(export: dict | None) -> dict[str, dict[str, str]]:
    """For each attribute on a lookup table, the attribute of that table that joins the fact (its forms read
    both): the child a parent on the table needs a relationship to. Only when exactly one does."""
    model = _export_model(export)
    if not model:
        return {}
    facts = {f.casefold() for f in _export_fact_tables(model)}
    tables = _attribute_tables(model)
    display = {str((a.get("information") or {}).get("name") or "").lower(): str((a.get("information") or {}).get("name"))
               for a in model.get("attributes") or []}
    lookup = {str((a.get("information") or {}).get("name") or "").lower(): (a.get("attributeLookupTable") or {}).get("name")
              for a in model.get("attributes") or []}
    out: dict[str, dict[str, str]] = {}
    for n, t in lookup.items():
        if not t or str(t).casefold() in facts:
            continue
        joiners = [c for c, tabs in tables.items() if c != n and t in tabs and {x.casefold() for x in tabs} & facts]
        if len(joiners) == 1:
            out[n] = {"parent": display.get(n, n), "child": display.get(joiners[0], joiners[0]), "table": str(t)}
    return out


def _lost_relationship_spec(export: dict | None, r: dict, typ: str | None) -> dict[str, str] | None:
    model = _export_model(export)
    if not model:
        return None
    attrs = {str((a.get("information") or {}).get("name") or "").lower(): a for a in model.get("attributes") or []}
    p, c = attrs.get(r["parent"]), attrs.get(r["child"])
    if not p or not c:
        return None   # one of them is gone: a person decides
    shared = sorted(set(_attribute_tables(model).get(r["parent"], set())) & set(_attribute_tables(model).get(r["child"], set())))
    return {"parent": str(p["information"]["name"]), "child": str(c["information"]["name"]),
            "table": shared[0] if len(shared) == 1 else "", "type": typ or "one_to_many"}


def _relationship_action(spec: dict[str, str]) -> dict[str, Any]:
    rel = {"parent_attribute": spec["parent"], "child_attribute": spec["child"],
           "type": spec.get("type") or "one_to_many"}
    if spec.get("table"):
        rel["relationship_table"] = spec["table"]
    return {"kind": "wire_relationships", "args": {"relationships": [rel]}}


def resolved_findings(findings_doc: dict, current: list[dict]) -> dict[str, list[str]]:
    """Findings an earlier join-paths or references run raised: the ones this run no longer raises are
    resolved (their check passes now), the rest still present."""
    now = {f["id"] for f in current}
    mine = [f for f in findings_doc.get("findings") or [] if f.get("source") in ("join-paths", "references")]
    return {"resolved": [f["id"] for f in mine if f["id"] not in now],
            "still_present": [f["id"] for f in mine if f["id"] in now]}


def check_join_paths(runner: Runner | None, project: str, scope: dict, *, export: dict | None = None,
                     previous: dict | None = None, where: str = "", round_no: int | None = None,
                     max_attributes: int = 300, log: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    """Does every attribute join each fact table, and does filtering on it filter? Through the connector,
    per fact table's anchor: COUNT(<anchor>) grouped by the attribute, two smallest groups; both equal to
    the anchor's total is a cross join (no join path), and an error naming a join is the same verdict. Then
    COUNT(<anchor>) WHERE attribute = its smallest value: it should equal that value's group; equal to the
    total means the filter is ignored when the attribute isn't selected. Attributes mapped directly to a
    fact table (export) aren't tested against it. From the export: tables each attribute reaches, and the
    declared relationships, compared with the previous round's when both have some."""
    model = scope["model"]
    graph = export_join_graph(export) if export else {}
    facts = _anchors_by_fact(scope)
    todo = _testable_attributes(scope, export)[:max_attributes]
    results: dict[str, dict[str, dict]] = {}
    skipped: dict[str, list[str]] = {}
    for fact in facts:
        if runner is None:
            break
        anchor, ftable = fact["anchor"], (fact.get("table") or "")
        clause = f" WHERE {where}" if where else ""
        try:
            tot = parse_query_result(runner.call("query", {"project": project, "query":
                                                           f"SELECT COUNT({quote_ident(anchor)}) AS n FROM {quote_model(model)}{clause}"}))  # nosec B608  # identifiers are quoted; the connector is read-only
            fact["total"] = _num(tot["rows"][0].get("n")) if tot["ok"] and tot["rows"] else None
        except PendingReply:   # ask for the group queries in the same pass: one replay round, not two
            fact["total"] = "pending"
        log(f"join paths against {anchor}: {len(todo)} attributes")
        for att in todo:
            n = att["name"]
            # mapped to this fact table directly: it can't cross-join it, but its filter still needs testing
            if att["tables"] is not None and ftable and ftable.casefold() in {t.casefold() for t in att["tables"]}:
                skipped.setdefault(anchor, []).append(n)
            try:
                parsed = parse_query_result(runner.call("query", {"project": project,
                                                                  "query": join_path_sql(model, n, anchor, where)}))
            except PendingReply:
                results.setdefault(n, {})[anchor] = {"verdict": "pending"}
                continue
            if not parsed["ok"]:
                err = parsed["error"] or "no reply"
                results.setdefault(n, {})[anchor] = {"verdict": "join_error" if _JOIN_ERROR.search(err) else "error",
                                                     "error": err[:300]}
                continue
            rows = [(r.get("v"), _num(r.get("n"))) for r in parsed["rows"]]
            counts = [c for _, c in rows]
            total = fact["total"]
            if total == "pending":
                verdict = "pending"
            elif len(rows) < 2:
                verdict = "single_value"
            elif total and all(c is not None and _close(c, total) for c in counts):
                verdict = "cross_join"
            else:
                verdict = "joined"
            res = {"verdict": verdict, "smallest_groups": [[v, c] for v, c in rows], "total": total}
            pick = next(((v, c) for v, c in rows if not _blank(v)), None)
            if verdict in ("joined", "single_value") and pick and total not in (None, "pending"):
                try:
                    fq = parse_query_result(runner.call("query", {"project": project, "query": filter_test_sql(
                        model, n, anchor, pick[0], att["type"], where)}))
                    got = _num(fq["rows"][0].get("n")) if fq["ok"] and fq["rows"] else None
                    fv = ("error" if got is None else "ok" if pick[1] is not None and _close(got, pick[1]) else
                          "ignored" if _close(got, total) else "mismatch")
                    res["filter"] = {"value": pick[0], "sent": _typed_literal(pick[0], att["type"]),
                                     "grouped": pick[1], "filtered": got, "verdict": fv,
                                     **({"error": (fq.get("error") or "")[:300]} if not fq["ok"] else {})}
                except PendingReply:
                    res["filter"] = {"value": pick[0], "verdict": "pending"}
            results.setdefault(n, {})[anchor] = res
    unjoined: dict[str, list[str]] = {}
    filter_problems: dict[str, list[dict]] = {}
    for n, by_anchor in results.items():
        bad = [a for a, r in by_anchor.items() if r["verdict"] in ("cross_join", "join_error")]
        if bad:
            unjoined[n] = bad
        for a, r in by_anchor.items():
            if (r.get("filter") or {}).get("verdict") in ("ignored", "mismatch"):
                filter_problems.setdefault(n, []).append({"anchor": a, **r["filter"]})
    for u in graph.get("unreachable") or []:
        if u["reaches_no_fact"] and not any(t["attribute"] == u["attribute"] and t["name"] in results for t in todo):
            unjoined.setdefault(u["attribute"], ["(export: no shared column with any fact table)"])
    flagged = set(unjoined) | set(filter_problems)
    filtering = []
    for m in scope.get("metrics") or []:
        f = m.get("formula") or ""
        for n in sorted(flagged):
            base = (_FORM_NAME.match(n).group("base") if _FORM_NAME.match(n) else n)
            names = {n, base}
            # a whole name between delimiters ({x}, "x", [x], (x)), never a word inside another name
            if any(re.search(r"[{\"\[(]\s*" + re.escape(x) + r"\s*[}\"\])]", f, re.I) for x in names):
                filtering.append({"metric": m["name"], "attribute": n})
    changes: dict[str, Any] = {}
    prev_rels = (previous or {}).get("relationships")
    if previous is not None and graph.get("relationships") and prev_rels:
        old = {(r["parent"], r["child"]) for r in prev_rels}
        new = {(r["parent"], r["child"]) for r in graph["relationships"]}
        changes = {"lost": [{"parent": p, "child": c} for p, c in sorted(old - new)],
                   "added": [{"parent": p, "child": c} for p, c in sorted(new - old)]}
    elif previous is not None and graph:
        changes = {"note": "not compared: " + ("the previous round recorded no relationships" if not prev_rels
                                               else "this export has no relationships")}
    findings = []
    all_anchors = [f["anchor"] for f in facts]
    for n, bad in sorted(unjoined.items()):
        none = all(a in bad for a in all_anchors) or bad[0].startswith("(export")
        mets = [x["metric"] for x in filtering if x["attribute"] == n]
        ev = results.get(n, {})
        detail = "; ".join(f"with {a}: {r['verdict']}" + (f" (every group = {r.get('total')})"
                                                         if r["verdict"] == "cross_join" else
                                                         f" ({r.get('error')})" if r.get("error") else "")
                           for a, r in ev.items() if a in bad)
        exp = next((u for u in graph.get("unreachable") or [] if u["attribute"] == n.lower()), None)
        findings.append(integrity_finding(
            f"J-{_slug(n)}", f"'{n}' has no join path to " + ("any fact table" if none else "every fact table"),
            "relationship",
            (detail or "no shared column with a fact table in the export")
            + (f". Export: its tables {exp['tables']} reach none of {exp['facts_not_reached']}" if exp else "")
            + (f". Metrics that may filter on it (filtering nothing): {mets}" if mets else ""),
            "Every value of the attribute gets the fact's full total (a cross join). Map the attribute's key to the "
            "fact's column (or the lookup table's key to an attribute the fact has), or remove the attribute. Say "
            "which, or decline if it is meant to stand alone.", round_no=round_no,
            impact="high" if none else "medium", source="join-paths",
            checks=[{"command": "join-paths", "attribute": n, "expect": "joined"}]))
    rels = graph.get("relationships") or []
    joins = _join_attributes(export) if export else {}
    for n, probs in sorted(filter_problems.items()):
        if n in unjoined:
            continue
        mets = [x["metric"] for x in filtering if x["attribute"] == n]
        base = (_FORM_NAME.match(n).group("base") if _FORM_NAME.match(n) else n).lower()
        mine = [f"{r['parent']} -> {r['child']}" for r in rels if base in (r["parent"], r["child"])]
        rel_note = ("" if not graph else f". Declared relationships: {mine}" if mine else
                    ". The export declares no relationship for it: a filter on an attribute that isn't selected "
                    "needs one to the attribute its table joins the fact through")
        detail = "; ".join(f"with {p['anchor']}: {n} = {p.get('sent') or repr(p['value'])} counts {p['filtered']} rows filtered, "
                           f"{p['grouped']} grouped" + (" (the full total: the filter is ignored)"
                                                        if p["verdict"] == "ignored" else "") for p in probs)
        findings.append(integrity_finding(
            f"J-filter-{_slug(n)}", f"Filtering on '{n}' doesn't filter the fact", "relationship",
            detail + rel_note + (f". Metrics that filter on it: {mets}" if mets else ""),
            "Grouped by the attribute the counts are right, but a filter on it alone isn't applied to the fact: "
            "usually a missing parent-child relationship to the attribute its table joins the fact through. "
            + (f"Proposed: {joins[base]['parent']} -> {joins[base]['child']} through {joins[base]['table']} "
               f"(one to many). Approve to create it, Modify to change it, or Decline."
               if base in joins and not mine else "Create the relationship (wire_relationships); say which, or decline."),
            round_no=round_no, source="join-paths",
            actions=[_relationship_action(joins[base])] if base in joins and not mine else None,
            checks=[{"command": "join-paths", "attribute": n, "expect": "filter ok"}]))
    prev_types = {(r["parent"], r["child"]): r.get("type") for r in prev_rels or []}
    for r in changes.get("lost") or []:
        spec = _lost_relationship_spec(export, r, prev_types.get((r["parent"], r["child"])))
        findings.append(integrity_finding(
            f"L-{_slug(r['parent'])}-{_slug(r['child'])}", f"Relationship {r['parent']} -> {r['child']} was lost",
            "relationship", "declared in the previous round's export, missing from this one",
            "Restore it (wire_relationships), or decline if the change was intended. Metrics and SQL that filter "
            "on the parent may now repeat totals or ignore the filter.", round_no=round_no, source="join-paths",
            actions=[_relationship_action(spec)] if spec else None,
            checks=[{"command": "join-paths", "relationship": [r["parent"], r["child"]], "expect": "present"}]))
    return {"version": 2, "model": model, "facts": facts, "attributes": results,
            "mapped_to_fact": skipped,
            "not_tested": [t["name"] for t in todo if t["name"] not in results] if runner is not None
            else [t["name"] for t in todo],
            "unjoined": unjoined, "filter_problems": filter_problems, "metrics_filtering_flagged": filtering,
            "export": {k: v for k, v in graph.items() if k != "relationships"} if graph else None,
            "relationships": graph.get("relationships") if graph else (previous or {}).get("relationships"),
            "relationship_changes": changes, "findings": findings,
            "note": "single_value: only one group, so a cross join can't be told apart (its filter is still tested)"}


def source_info(**paths: str | None) -> dict[str, Any]:
    """Which scope and export an output was made from: file name and a short content hash, so outputs made
    from different exports (a table replaced in between) can be told apart. Names only, never content."""
    out: dict[str, Any] = {}
    for key, path in paths.items():
        if not path:
            continue
        try:
            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()[:12]
        except OSError:
            digest = None
        out[key] = {"file": os.path.basename(path), "sha": digest}
    return out


def relationship_diff(before: dict, after: dict) -> dict[str, Any]:
    """Relationships in one export and not the other (by attribute name), with the action that restores each
    lost one where the export shows its table."""
    b = {(r["parent"], r["child"]): r for r in _export_relationships(_export_model(before), before)}
    a = {(r["parent"], r["child"]): r for r in _export_relationships(_export_model(after), after)}
    lost = [b[k] for k in sorted(set(b) - set(a))]
    restore = []
    for r in lost:
        spec = _lost_relationship_spec(after, r, r.get("type"))
        if spec:
            restore.append(_relationship_action(spec))
    return {"before": len(b), "after": len(a), "lost": lost, "added": [a[k] for k in sorted(set(a) - set(b))],
            "restore_actions": restore}


def merge_findings(doc: dict, new: list[dict]) -> dict[str, Any]:
    """Add findings from references / join-paths. A finding already there keeps its status and decision once
    the user answered it (only its evidence is refreshed); one still proposed takes the new evidence,
    recommendation, proposed actions and checks. Returns what was added and what was already there."""
    have = {f.get("id"): f for f in doc.setdefault("findings", [])}
    added, kept = [], []
    for f in new:
        old = have.get(f["id"])
        if old is None:
            doc["findings"].append(f)
            added.append(f["id"])
        else:
            old["evidence"] = f["evidence"]
            if (old.get("status") or "proposed") == "proposed":
                for k in ("recommendation", "actions", "checks", "impact", "title"):
                    if k in f:
                        old[k] = f[k]
            kept.append({"id": f["id"], "status": old.get("status")})
    return {"added": added, "already_there": kept}


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
        # "status description" describes "status code"; "status bucket__desc" describes the bucket, not the code
        if stem and re.fullmatch(rf"{re.escape(stem)}\s*(\(|__)?\s*(description|desc|name|label)\)?", o):
            return True
    return False


def _entity_table_suggestion(relationships: dict) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    static = (relationships.get("suggestions") or {}).get("entity_level_attributes") or []
    if len(static) >= 5:
        out.append({"id": "L-entity", "kind": "entity_table", "attribute": relationships.get("entity"),
                    "reason": f"{len(static)} attributes are fixed per entity but repeat on every fact row; "
                              "an entity table is optional — recommend it only if it fixes a question "
                              "(counts, once-per-entity amounts) or the fact is large",
                    "columns": [relationships.get("entity")] + static[:50]})
    return out


_CODE_TOKEN = re.compile(r"^[A-Z0-9][A-Z0-9_\-./+]{0,9}$")


def looks_like_codes(values: list[str], description: str = "") -> bool:
    """Values that need a lookup to be read: short tokens (up to 4 characters, digits or upper case: "01",
    "XX", "RA", "P"), or values explained only in the description text. Words ("High", "Prepaid", "REO sale")
    are readable, and longer tokens ("19DNA1", "PREPAID") are names or identifiers analysts already use."""
    vals = [str(v).strip() for v in values if str(v).strip()]
    if not vals:
        return False
    if all(len(v) <= 2 or (len(v) <= 4 and (v.isupper() or any(ch.isdigit() for ch in v))) for v in vals):
        return True
    return bool(re.search(r"\b\w{1,4}\s*[=:]\s*\w", description or ""))   # "01 = current, 02 = 30 days"


_DESC_FORM = re.compile(r"\b(desc|description|label|name|text)\b", re.I)


def _export_model(export: dict | None) -> dict | None:
    m = (export or {}).get("model")
    return (m[0] if m else None) if isinstance(m, list) else m


def export_forms(scope: dict, export: dict | None) -> dict[str, dict[str, Any]]:
    """Per connector attribute: its table, and each form with its physical column, whether the connector lists
    it, and the name to query it by. The connector shows a key form as the attribute and other forms as
    '<attribute>__<form>' or '<attribute> (<form>)'; a form listed by neither is hidden from analysts (it can
    still be queried as '<attribute> (<form>)')."""
    model = _export_model(export)
    if not model:
        return {}
    listed = {a["name"].casefold() for a in scope.get("attributes") or []}
    out: dict[str, dict[str, Any]] = {}
    for a in model.get("attributes") or []:
        mname = str((a.get("information") or {}).get("name") or "")
        cname = mname.lower()
        # the connector may list an attribute only through its forms ("<attribute> (id)", "<attribute>__<form>")
        if cname.casefold() not in listed and not any(n.startswith(cname.casefold() + " (") or
                                                       n.startswith(cname.casefold() + "__") for n in listed):
            continue
        key_id = (a.get("keyForm") or {}).get("id")
        forms, tables = [], []
        for f in a.get("forms") or []:
            by_table: dict[str, str] = {}   # a form can read a column in several tables (the fact and a lookup)
            for e in f.get("expressions") or []:
                et = (e.get("expression") or {}).get("text") or ""
                ecol = et[1:-1] if re.fullmatch(r"\[[^\[\]]+\]", et.strip()) else ""
                for t in e.get("tables") or []:
                    if t.get("name"):
                        tables.append(str(t["name"]))
                        if ecol:
                            by_table.setdefault(str(t["name"]), ecol)
            text = ((f.get("expressions") or [{}])[0].get("expression") or {}).get("text") or ""
            col = text[1:-1] if re.fullmatch(r"\[[^\[\]]+\]", text.strip()) else ""
            fname = str(f.get("name") or "").lower()
            is_key = f.get("id") == key_id
            cands = ([cname, f"{cname} ({fname})", f"{cname}__{fname}", f"{cname} (id)", f"{cname}__id"] if is_key
                     else [f"{cname}__{fname}", f"{cname} ({fname})"])
            visible = next((c for c in cands if c.casefold() in listed), None)
            forms.append({"form": f.get("name"), "category": f.get("category"), "column": col, "key": is_key,
                          "columns_by_table": by_table,
                          "visible": bool(visible), "query_as": visible or f"{cname} ({fname})",
                          "describes": not is_key and bool(_DESC_FORM.search(f"{f.get('name')} {f.get('category')}"))})
        out[cname] = {"table": (a.get("attributeLookupTable") or {}).get("name"), "forms": forms,
                      "tables": list(dict.fromkeys(tables))}
    return out


def hidden_forms(scope: dict, export: dict | None) -> list[dict[str, Any]]:
    """Forms the model has but the connector doesn't list (get_semantics omits them, so analysts and agents never
    see them). A label hidden this way looks like a missing description. A hidden key form is the worse case:
    analysts can't filter or group on the codes the glossary uses."""
    return [{"attribute": n, "form": f["form"], "category": f["category"], "query_as": f["query_as"], "key": f["key"]}
            for n, a in export_forms(scope, export).items() for f in a["forms"] if not f["visible"]]


def existing_lookup_tables(scope: dict, *, relationships: dict | None = None, probes: dict | None = None,
                           export: dict | None = None) -> dict[str, dict[str, Any]]:
    """Lookup and mapping tables the model already has: {table: {attributes, key, columns, source, evidence}}.
    From a model export (definitive: each attribute's lookup table, the fact tables being those its fact
    metrics read), else inferred: attributes that list values with no fact rows (probe), grouped by the
    parent-child relationships between them (relationships). The key is the most detailed attribute: one that
    determines the others in its table."""
    names = {a["name"].casefold(): a["name"] for a in scope.get("attributes") or []}
    edges = [(str(r["child"]), str(r["parent"])) for r in ((relationships or {}).get("suggestions") or {})
             .get("parent_child") or []]
    groups: dict[str, dict[str, Any]] = {}
    model = _export_model(export)
    forms = export_forms(scope, export)
    conn: dict[str, str] = {}   # attribute -> the name the connector answers it by
    if model:
        tables = {(t.get("information") or {}).get("name"): t for t in model.get("tables") or []}
        fact_tables = {str(tb.get("name")) for fm in model.get("factMetrics") or []
                       for e in (fm.get("fact") or fm).get("expressions") or [] for tb in e.get("tables") or []}
        by_table: dict[str, list[str]] = {}
        declared: dict[str, str] = {}
        for a in model.get("attributes") or []:
            t = (a.get("attributeLookupTable") or {}).get("name")
            raw = str((a.get("information") or {}).get("name") or "").casefold()
            n = names.get(raw) or (raw if raw in forms else None)
            if not n:
                continue
            # the connector may list it only through a form ("zero balance__code"): profiles, relationships and
            # queries use that name
            key_form = next((f for f in (forms.get(n.lower()) or {}).get("forms", []) if f["key"]), None)
            conn[n] = n if n.casefold() in names else (key_form["query_as"] if key_form else n)
            if t:
                declared[n] = t
                by_table.setdefault(t, []).append(n)
        if not fact_tables and by_table:   # no metrics read in the export: the table most attributes use is the fact
            fact_tables = {max(by_table, key=lambda t: len(by_table[t]))}
        # an attribute also belongs to every other table its forms read: the attribute that joins a mapping table
        # to the fact often has the fact as its lookup table, and a description table may be set on no attribute
        for n in list(conn):
            for t in (forms.get(n.lower()) or {}).get("tables", []):
                if t not in fact_tables and n not in by_table.setdefault(t, []):
                    by_table[t].append(n)
        for t, attrs in by_table.items():
            if t in fact_tables:
                continue
            cols = [c.get("columnName") or (c.get("information") or {}).get("name")
                    for c in ((tables.get(t) or {}).get("physicalTable") or {}).get("columns") or []]
            read_as: dict[str, str] = {}
            for n in attrs:   # a column read by several forms: prefer one the connector lists, else the first
                for f in (forms.get(n.lower()) or {}).get("forms", []):
                    col = (f.get("columns_by_table") or {}).get(t) or ("" if f.get("columns_by_table") else f["column"])
                    if col and (col not in read_as or f["visible"] and not read_as.get("_v_" + col)):
                        read_as[col] = f["query_as"]
                        if f["visible"]:
                            read_as["_v_" + col] = "1"
            read_as = {k: v for k, v in read_as.items() if not k.startswith("_v_")}
            groups[t] = {"attributes": attrs, "columns": [c for c in cols if c], "source": "model export",
                         "read_as": read_as, "connector_names": {n: conn.get(n, n) for n in attrs},
                         "declared": [n for n in attrs if declared.get(n) == t],
                         # attributes whose forms read the fact too: the columns that join the table to the fact
                         "joins_fact": [n for n in attrs if set((forms.get(n.lower()) or {}).get("tables", []))
                                        & fact_tables],
                         "described": [n for n in attrs if any(f["describes"] for f in (forms.get(n.lower()) or {})
                                                               .get("forms", []))],
                         "evidence": ["each attribute's lookup table in the model export"]}
    else:
        lookup_only = {a["name"]: a.get("lookup_only_values") or [] for a in (probes or {}).get("attributes") or []
                       if a.get("lookup_only_values")}
        members = set(lookup_only)
        for c, p in edges:   # a parent or child of a lookup-only attribute is in the same table, as a rule
            if c in members or p in members:
                members |= {c, p}
        seen: set[str] = set()
        for start in sorted(members):
            if start in seen:
                continue
            comp, todo = set(), [start]
            while todo:
                x = todo.pop()
                if x in comp:
                    continue
                comp.add(x)
                todo += [p for c, p in edges if c == x and p in members] + [c for c, p in edges if p == x and c in members]
            seen |= comp
            ev = [f"'{n}' lists {len(lookup_only[n])} value(s) with no fact rows" for n in sorted(comp) if n in lookup_only]
            ev += [f"'{c}' determines '{p}'" for c, p in edges if c in comp and p in comp]
            groups[f"<inferred: {start}>"] = {"attributes": sorted(comp), "columns": [], "source": "inferred",
                                              "evidence": ev}
    prof = {a["name"]: _distinct(a) or 0 for a in scope.get("attributes") or []}
    for g in groups.values():
        attrs = g["attributes"]
        cn = g.get("connector_names") or {}
        back = {cn.get(n, n): n for n in attrs}
        pairs = [(back.get(c, c), back.get(p, p)) for c, p in edges]
        parents = {p for c, p in pairs if c in attrs and p in attrs}
        children = [a for a in attrs if a not in parents]
        # the key is the column that joins the fact (export), else the most detailed attribute
        pool = [a for a in (children or attrs) if a in (g.get("joins_fact") or [])] or g.get("joins_fact") \
            or children or attrs
        g["key"] = max(pool, key=lambda n: prof.get(cn.get(n, n), 0))
        g["determined"] = [a for a in attrs if a != g["key"]]
    return groups


def lookup_suggestions(scope: dict, *, period: str = "", max_code_values: int = 200,
                       relationships: dict | None = None, time_attributes: bool = True,
                       probes: dict | None = None, export: dict | None = None) -> list[dict[str, Any]]:
    """Lookups and calendar parents. Existing lookup or mapping tables (from a model export, else inferred from
    the probe's lookup-only values and the relationships) are extended rather than duplicated: one CSV per table,
    keyed on its most detailed code, carrying the existing columns plus descriptions. Calendar parents (year, quarter, month) come from time attributes on a date
    (converting a number stored as a date first); with time_attributes=False they come from lookup CSVs instead,
    the fallback when a column can't be converted."""
    attrs = scope.get("attributes") or []
    names = [a["name"] for a in attrs]
    out: list[dict[str, Any]] = []
    existing = existing_lookup_tables(scope, relationships=relationships, probes=probes, export=export)
    in_table: dict[str, str] = {}
    for t, g in existing.items():   # an attribute in several tables: its own lookup first
        for n in g["attributes"]:
            for name in {n, (g.get("connector_names") or {}).get(n, n)}:
                if name not in in_table or n in (g.get("declared") or []):
                    in_table[name] = t
    extend: dict[str, list[str]] = {}
    for a in attrs:
        prof = a.get("profile") or {}
        d = prof.get("distinct_values")
        is_text = a.get("type", "").lower() in ("varchar", "char", "string", "text") and not looks_like_stored_date(a)
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
        if values and not looks_like_codes(values, a.get("description") or ""):
            continue   # words, readable as they are: a missing definition is a description change, not a lookup
        short_codes = bool(values) and all(len(v) <= 4 for v in values)
        if not (_CODE_NAME.search(_words(a["name"])) or short_codes or values):
            continue
        desc = (a.get("description") or "").lower()
        if a["name"] in in_table:   # the model already has its table: extend it, keyed on the most detailed code
            extend.setdefault(in_table[a["name"]], []).append(a["name"])
            continue
        out.append({"id": f"L-{_slug(a['name'])}", "kind": "code_lookup", "attribute": a["name"],
                    "distinct_values": d,
                    "reason": "coded values with no description form or attribute"
                              + ("; the codes are explained only in the description text" if "=" in desc or ":" in desc
                                 else ""),
                    "columns": [a["name"], f"{a['name']} description"],
                    "new_forms": [f"{a['name']} description"], "new_attributes": []})
    for table, coded in extend.items():
        g = existing[table]
        described = set(g.get("described") or [])
        need = [n for n in [g["key"]] + g["determined"] if n in coded and n not in described]
        if not need:
            continue   # every coded attribute of the table already has a description form (maybe hidden)
        from_export = g["source"] == "model export"
        keep = g["columns"] if from_export and g["columns"] else [g["key"]] + g["determined"]
        descs = [f"{n} description" for n in need]
        out.append({"id": f"X-{_slug(g['key'])}", "kind": "extend_lookup", "attribute": g["key"],
                    "table": table, "table_source": g["source"], "evidence": g["evidence"],
                    "keep_columns": keep, "read_as": g.get("read_as") or {c: c for c in keep},
                    "headers": "table columns" if from_export else "connector names",
                    "reason": f"the model already has this table ({g['source']}): add descriptions to it, keyed on its "
                              f"most detailed code '{g['key']}', instead of a new table and a second join"
                              + ("" if from_export else
                                 "; the table and its columns are inferred, and the headers are connector names: "
                                 "export the model (export-model, --model-export) before replacing the table"),
                    "columns": keep + descs, "new_forms": descs, "new_attributes": [],
                    "key_column": next((c for c, v in (g.get("read_as") or {}).items()
                                        if v == (g.get("connector_names") or {}).get(g["key"], g["key"])), keep[0] if keep else g["key"]),
                    "load_format": (load_formats(export).get(_table_key(table)) if from_export else None)})
    lost_by = {a["name"]: [x for x in a.get("issues") or [] if "keeps" in x and "fact rows" in x]
               for a in (probes or {}).get("attributes") or []}
    for n, lost in lost_by.items():
        if lost and n in names:   # rows lost through an attribute's lookup, whether or not it gets a CSV
            g = existing.get(in_table.get(n, ""), {})
            key = g.get("key") if g else None   # the column that joins the fact
            key = (g.get("connector_names") or {}).get(key, key) if key else None
            out.append({"id": f"B-{_slug(n)}", "kind": "blank_key", "attribute": n, "rows_lost": lost,
                        "count_on": key if key and key != n else n,
                        "count_on_source": g.get("source") if key and key != n else None,
                        "reason": "the probe saw fact rows lost through this attribute's lookup (often a blank key "
                                  "that no lookup row matches): give blank a code in the source, then a lookup row "
                                  "for it",
                        "columns": [], "new_forms": [], "new_attributes": []})
    for h in hidden_forms(scope, export):
        out.append({"id": f"H-{_slug(h['attribute'])}-{_slug(str(h['form']))}", "kind": "hidden_form",
                    "attribute": h["attribute"], "form": h["form"], "category": h["category"],
                    "query_as": h["query_as"], "key_form": bool(h.get("key")),
                    "reason": f"the model has the {'key ' if h.get('key') else ''}form '{h['form']}' (category "
                              f"{h['category']}) but the connector doesn't list it, so analysts and agents never see it"
                              + ("; it holds the codes, so nobody can filter or group on the codes the glossary and "
                                 "source use" if h.get("key") else "")
                              + f". It should answer as \"{h['query_as']}\" (check with a query). Make it visible "
                                f"instead of adding a column that duplicates it",
                    "action": {"kind": "show_forms", "args": {"attribute": h["attribute"], "forms": [h["form"]]}},
                    "columns": [], "new_forms": [], "new_attributes": []})
    period_attr = period
    if not period_attr:
        dated = [a for a in attrs if (a.get("type", "").lower() in ("date", "timestamp") or looks_like_stored_date(a))
                 and _PERIOD_NAME.search(_words(a["name"])) and (_distinct(a) or 0) >= 2]
        if dated:
            preferred = re.compile(r"\b(period|reporting|snapshot|as of|activity|month)\b", re.I)
            period_attr = max(dated, key=lambda a: (bool(preferred.search(_words(a["name"]))),
                                                    _distinct(a) or 0))["name"]
    latest = any("latest" in n.lower() or "current period" in n.lower() for n in names)
    if time_attributes:
        for a in attrs:
            if "__" in a["name"] or (_distinct(a) or 0) < 2:
                continue
            is_date = a.get("type", "").lower() in ("date", "timestamp")
            if not (is_date or looks_like_stored_date(a)) or has_time_attributes(a["name"], names):
                continue
            pattern = "" if is_date else date_pattern(a)
            month_grain = pattern == "yyyyMM"
            ef = export_forms(scope, export).get(a["name"].lower()) or {}
            homes = ef.get("tables") or ([ef["table"]] if ef.get("table") else [])
            out.append({"id": f"T-{_slug(a['name'])}", "kind": "time_attributes", "attribute": a["name"],
                        "table": homes[0] if homes else None, "tables": homes,
                        "shared_table_note": (f"the attribute reads {', '.join(repr(h) for h in homes)}: converting its "
                                              f"type changes {'each of these tables' if len(homes) > 1 else 'that table'}, "
                                              f"so check whether other models read the same files") if homes else None,
                        "convert": {"type": "date", "date_format": pattern} if pattern else None,
                        "intervals": ["year", "quarter", "month"] if month_grain or is_date
                        else ["year", "quarter", "month", "day"],
                        "reason": (f"a date stored as {'text' if (a.get('type') or '').lower() in _TEXT_TYPES else 'a number'}: "
                                   "convert it to a date (change_column_type, "
                                   f"date_format {pattern}), then add time attributes" if pattern else
                                   "a date without time attributes") +
                                  " (add_time_attributes): year, quarter and month intervals that group correctly, "
                                  "enable time-intelligence metrics, and need no lookup table",
                        "columns": [], "new_forms": [], "new_attributes": []})
        if period_attr and not latest:   # time attributes give the calendar, not "latest": a small lookup does
            out.append({"id": f"L-{_slug(period_attr)}", "kind": "period_lookup", "attribute": period_attr,
                        "reason": "no latest-period flag: point-in-time metrics resolve 'latest' per group without "
                                  "one (calendar parents come from the period's time attributes)",
                        "columns": [period_attr, "latest period flag"],
                        "new_forms": [], "new_attributes": ["latest period flag"]})
        if relationships:
            out += _entity_table_suggestion(relationships)
        return out
    if period_attr and not latest:
        out.append({"id": f"L-{_slug(period_attr)}", "kind": "period_lookup", "attribute": period_attr,
                    "reason": "no latest-period flag: point-in-time metrics resolve 'latest' per group without one, "
                              "and calendar parents (year, quarter) need a period table",
                    "columns": [period_attr, "year", "quarter", "month", "latest period flag"],
                    "new_forms": ["month"], "new_attributes": ["year", "quarter", "latest period flag"]})
    # Calendar parents of other dates come from a lookup keyed on the date, not a derived attribute: a lookup
    # is faster at run time and can be replaced without deleting the metrics built on it.
    for a in attrs:
        if a["name"] == period_attr or (_distinct(a) or 0) < 2:
            continue
        if not (a.get("type", "").lower() in ("date", "timestamp") or looks_like_stored_date(a)):
            continue
        year = f"{a['name']} year"
        if any(n.lower() in (year.lower(), f"{a['name']} yr".lower()) for n in names):
            continue
        out.append({"id": f"L-{_slug(a['name'])}", "kind": "date_lookup", "attribute": a["name"],
                    "reason": "no calendar parents: grouping by year or quarter needs a lookup keyed on the date "
                              "(preferred to a derived attribute: faster, and replaceable without deleting the "
                              "metrics built on it)",
                    "columns": [a["name"], year, f"{a['name']} quarter"],
                    "new_forms": [], "new_attributes": [year, f"{a['name']} quarter"]})
    if relationships:
        out += _entity_table_suggestion(relationships)
    return out


def _period_parts(value: str) -> tuple[str, str, str]:
    m = re.match(r"^(\d{4})-?(\d{2})", _plain_number(value))
    if not m:
        return "", "", ""
    year, month = m.group(1), int(m.group(2))
    return year, f"{year}-Q{(month - 1) // 3 + 1}", f"{month:02d}"


# A table's load format, from the export's import settings. Without one, a template is written the safe way: no
# quote character, so a value holding the separator is a problem to fix (rephrase), never quoted.
ASSUMED_LOAD_FORMAT = {"separator": ",", "quote": "", "header": True, "encoding": "utf-8", "assumed": True}


def _table_key(name: Any) -> str:
    n = str(name or "").strip().casefold()
    return re.sub(r"\.(csv|txt|tsv)$", "", n)


def _load_format(opts: dict) -> dict[str, Any]:
    def pick(*keys: str) -> Any:
        for k in keys:
            for kk, v in opts.items():
                if str(kk).casefold() == k.casefold():
                    return v
        return None
    sep = pick("separator", "delimiter", "columnSeparator", "fieldSeparator")
    sep = {"\\t": "\t", "tab": "\t", "comma": ",", "pipe": "|", "semicolon": ";"}.get(str(sep).lower(), sep) \
        if sep is not None else ","
    quote = pick("quote", "quoteChar", "quoteCharacter", "textQualifier")
    header = pick("header", "hasHeader", "firstRowHeader", "firstRowAsHeader")
    if isinstance(header, str):
        header = header.strip().lower() in ("true", "1", "yes")
    enc = pick("encoding", "charset", "fileEncoding")
    return {"separator": str(sep), "quote": "" if quote is None else str(quote),
            "header": True if header is None else bool(header), "encoding": str(enc or "utf-8"),
            "assumed": False, **({"quote_unset": True} if quote is None else {})}


def load_formats(export: dict | None) -> dict[str, dict[str, Any]]:
    """Each table's import settings (separator, quote character, header row, encoding) from the export's pipeline
    (importSource.dataOptions), keyed by table name without its extension. A reload is read with these settings,
    so a template must be written with them."""
    out: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            src = node.get("importSource")
            if isinstance(src, dict) and isinstance(src.get("dataOptions"), dict):
                for name in (node.get("name"), (node.get("information") or {}).get("name"), src.get("name"),
                             src.get("fileName"), src.get("file")):
                    if isinstance(name, str) and name:
                        out.setdefault(_table_key(name), _load_format(src["dataOptions"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(export)
    return out


def _model_load_format(export: dict | None) -> dict[str, Any]:
    """The format most of the model's tables load with: the one a new table should use too."""
    fmts = list(load_formats(export).values())
    if not fmts:
        return dict(ASSUMED_LOAD_FORMAT)
    keyed = [(f["separator"], f["quote"], f["header"], f["encoding"]) for f in fmts]
    best = max(dict.fromkeys(keyed), key=keyed.count)
    return {"separator": best[0], "quote": best[1], "header": best[2], "encoding": best[3], "assumed": False,
            "from": "the format most of the model's tables use"}


def _codec(name: str) -> str:
    try:
        return codecs.lookup(name).name
    except LookupError:
        return "utf-8"


def _value_problems(value: str, fmt: dict) -> list[str]:
    sep, q = fmt["separator"], fmt.get("quote") or ""
    bad = []
    if "\n" in value or "\r" in value:
        bad.append("a line break")
    if not q:
        if sep in value:
            bad.append(f"the separator {sep!r}")
        if '"' in value:
            bad.append("a double quote (the table has no quote character)")
    return bad


def _write_csv(path: str, header: list[str], rows: list[list[Any]], fmt: dict | None = None) -> list[str]:
    """Write a file in the table's load format. With no quote character nothing is quoted: a value holding the
    separator, a quote or a line break would shift columns on reload, so it is reported, not quoted."""
    fmt = fmt or ASSUMED_LOAD_FORMAT
    sep, q = fmt["separator"], fmt.get("quote") or ""
    problems: list[str] = []
    lines = []
    body = ([header] if fmt.get("header", True) else []) + rows
    for i, r in enumerate(body, 1):
        cells = ["" if v is None else str(v) for v in r]
        for j, c in enumerate(cells):
            bad = _value_problems(c, fmt)
            if bad:
                problems.append(f"line {i}, column '{header[j] if j < len(header) else j}': {c[:40]!r} holds "
                                + " and ".join(bad))
        if q:
            buf = io.StringIO()
            csv.writer(buf, delimiter=sep, quotechar=q, lineterminator="").writerow(cells)
            lines.append(buf.getvalue())
        else:
            lines.append(sep.join(cells))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding=_codec(fmt.get("encoding") or "utf-8"), newline="") as f:
        f.write("\n".join(lines) + "\n")
    return problems


def _write_sources(path: str, key_header: str, keys: list[Any], desc_cols: list[str]) -> None:
    """Sources for the description columns, one row per key: kept out of the load file (the table has no such
    column), read by the check and by reviewers. Not loaded, so ordinary CSV quoting is fine here."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([key_header] + [f"{d} source" for d in desc_cols])
        w.writerows([[k] + [""] * len(desc_cols) for k in keys])


def _finish_template(entry: dict, path: str, header: list[str], rows: list[list[Any]], fmt: dict) -> dict:
    """Write the load file and its sources file; record the format and the fixed columns' rows (those the filler
    must not change) so check-lookup can compare a filled file with them."""
    problems = _write_csv(path, header, rows, fmt)
    descs = [c for c in entry.get("new_forms") or [] if c in header]
    fixed = [c for c in header if c not in descs]
    out = {**entry, "csv": path, "load_format": fmt,
           "template": {"columns": header, "fixed_columns": fixed,
                        "rows": [[r[header.index(c)] for c in fixed] for r in rows]}}
    if descs:
        key_col = entry.get("key_column") or header[0]
        src = re.sub(r"\.csv$", "", path) + "_sources.csv"
        _write_sources(src, key_col, [r[header.index(key_col)] if key_col in header else r[0] for r in rows], descs)
        out["sources_csv"] = src
    if problems:
        out["format_problems"] = problems
    return out


def build_lookups(runner: Runner, project: str, scope: dict, suggestions: list[dict], csv_dir: str, *,
                  where: str = "", max_values: int = 1000, batch: int = 8,
                  export: dict | None = None) -> list[dict[str, Any]]:
    """Write a CSV template per suggestion: key values from the data (reusing the scope's samples,
    querying the rest in batches); description columns left blank for the agent to fill from cited
    sources (or the user to fill)."""
    os.makedirs(csv_dir, exist_ok=True)
    model = scope["model"]
    new_fmt = _model_load_format(export)   # a new table loads like the model's other tables
    profiles = {a["name"]: a.get("profile") or {} for a in scope.get("attributes") or []}
    known: dict[str, Any] = {}
    wanted: dict[str, float] = {}
    for sug in suggestions:
        if sug["kind"] in ("entity_table", "time_attributes", "extend_lookup", "blank_key", "hidden_form"):
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
        if sug["kind"] == "time_attributes":
            done.append({**sug, "csv": None, "note": "no CSV: a type change (if needed) and add_time_attributes"})
            continue
        if sug["kind"] in ("blank_key", "hidden_form"):
            extra = {}
            if sug["kind"] == "blank_key":
                on = sug.get("count_on") or sug["attribute"]
                b = _blank_key_rows(runner, project, scope, on, where)
                extra = _blank_note(b, on, sug.get("rows_lost")) if b else {
                    "blank_key_note": f"no blank '{on}' rows" + (
                        f": the rows are lost some other way (a key value with no row in the table, not a blank); "
                        f"compare the fact's '{on}' values with the table's" if sug.get("count_on_source") == "model export"
                        else f": '{sug['attribute']}' has no column in the fact, so the rows are lost through its "
                             f"table's key; name that key with a model export (export-model, --model-export)")}
            done.append({**sug, **extra, "csv": None})
            continue
        blank = _blank_key_rows(runner, project, scope, sug["attribute"], where)
        if sug["kind"] == "extend_lookup":
            done.append(_extend_template(runner, project, model, sug, csv_dir, where, blank,
                                         sug.get("load_format") or new_fmt))
            continue
        got = known.get(sug["attribute"])
        if got == "pending":
            done.append({**sug, "csv": None, "pending": True})
            continue
        if got is None:
            done.append({**sug, "csv": None, "error": "values could not be read"})
            continue
        values = [_plain_number(v) if sug["kind"] in ("period_lookup", "date_lookup") else v
                  for v in got if v not in (None, "")]
        dropped = _null_artifacts(values, profiles.get(sug["attribute"], {}), scope, sug["attribute"])
        values = [v for v in values if v not in dropped]
        path = os.path.join(csv_dir, f"{_slug(sug['attribute'])}_lookup.csv")
        if sug["kind"] == "code_lookup":
            rows = [[v, ""] for v in values]
        elif sug["kind"] == "date_lookup":
            rows = [[v, *_period_parts(v)[:2]] for v in values]
        else:
            latest = max(values) if values else None
            parts = (lambda v: _period_parts(v)) if len(sug["columns"]) > 2 else (lambda v: ())
            rows = [[v, *parts(v), "Y" if v == latest else "N"] for v in values]
        entry = {**_finish_template(sug, path, sug["columns"], [[str(x) for x in r] for r in rows], new_fmt),
                 "rows": len(values), **_blank_note(blank, sug["attribute"])}
        if dropped:
            entry["note"] = (f"dropped {sorted(dropped)}: outside the attribute's min/max, so a NULL rendered as a "
                             f"number by CAST — represent 'no value' explicitly if users need it")
        done.append(entry)
    return done


def _blank_key_rows(runner: Runner, project: str, scope: dict, attribute: str, where: str = "") -> float | None:
    """Fact rows whose key is blank, counted with the anchor metric. A lookup row can't match a blank key, so
    those rows vanish from any query that joins the lookup (a filter or a second attribute)."""
    anchor = (scope.get("anchor") or {}).get("metric")
    if not anchor:
        return None
    k = quote_ident(attribute)
    cond = f"({k} IS NULL OR CAST({k} AS VARCHAR) = '')" + (f" AND ({where})" if where else "")
    sql = f"SELECT CAST(COUNT({quote_ident(anchor)}) AS VARCHAR) AS n FROM {quote_model(scope['model'])} WHERE {cond}"  # nosec B608  # identifiers are quoted; the connector is read-only
    try:
        parsed = parse_query_result(runner.call("query", {"project": project, "query": sql}))
    except PendingReply:
        return None
    return _num(parsed["rows"][0].get("n")) if parsed["ok"] and parsed["rows"] else None


def _blank_note(blank: float | None, attribute: str, lost: list[str] | None = None) -> dict[str, Any]:
    if not blank:
        return {}
    if lost:
        return {"blank_key_fact_rows": blank, "rows_lost": lost,
                "blank_key_warning": f"{blank:,.0f} fact rows have a blank '{attribute}', and the probe saw rows lost "
                                     f"through it ({'; '.join(lost)}): give blank a code in the source "
                                     f"(source_change), then a lookup row for that code."}
    return {"blank_key_fact_rows": blank,
            "blank_key_note": f"{blank:,.0f} fact rows have a blank '{attribute}'; the probe's shapes kept them (a "
                              f"blank matches no lookup row, but this join keeps them). Re-check after any lookup "
                              f"change."}


def _read_table_rows(runner: Runner, project: str, model: str, read_as: dict[str, str], columns: list[str],
                     where: str = "") -> tuple[list[list[str]] | None, str | None]:
    """A table's rows through the connector: an attribute-only query reads the lookup itself. Blanks stay blank
    (CAST alone turns a NULL into 0). Columns with no connector name come back blank."""
    readable = [c for c in columns if read_as.get(c)]
    if not readable:
        return [], None
    cols = ", ".join(f"CASE WHEN {quote_ident(read_as[c])} IS NULL THEN '' ELSE CAST({quote_ident(read_as[c])} AS VARCHAR) "
                     f"END AS c{i}" for i, c in enumerate(readable))
    clause = f" WHERE {where}" if where else ""
    sql = f"SELECT {cols} FROM {quote_model(model)}{clause} GROUP BY {', '.join(str(i + 1) for i in range(len(readable)))}"  # nosec B608  # identifiers are quoted; the connector is read-only
    parsed = parse_query_result(runner.call("query", {"project": project, "query": sql}))
    if not parsed["ok"]:
        return None, parsed["error"] or "the table's rows could not be read"
    pos = {c: i for i, c in enumerate(readable)}
    return sorted([[(r.get(f"c{pos[c]}") or "") if c in pos else "" for c in columns] for r in parsed["rows"]]), None


def _extend_template(runner: Runner, project: str, model: str, sug: dict, csv_dir: str, where: str,
                     blank: float | None, fmt: dict | None = None) -> dict[str, Any]:
    """The existing table's rows (an attribute-only query reads the lookup itself), every column kept under the
    table's own names, description columns blank to fill from cited sources. Blanks stay blank: CAST alone
    turns a NULL into 0, which a reload would write into the table."""
    keep, read_as = sug["keep_columns"], sug.get("read_as") or {}
    try:
        rows, err = _read_table_rows(runner, project, model, read_as, keep, where)
    except PendingReply:
        return {**sug, "csv": None, "pending": True}
    if rows is None:
        return {**sug, "csv": None, "error": err}
    extra = len(sug["columns"]) - len(keep)
    path = os.path.join(csv_dir, f"{_slug(sug['attribute'])}_lookup.csv")
    fmt = fmt or sug.get("load_format") or ASSUMED_LOAD_FORMAT
    built = _finish_template(sug, path, sug["columns"], [r + [""] * extra for r in rows], fmt)
    unread = [c for c in keep if not read_as.get(c)]
    note = ("starts from the existing table's rows, every column kept under the table's own names, written in the "
            "table's load format; descriptions to fill from cited sources, named in the sources file (or 'model data' "
            "when derived from the table itself)")
    if not sug.get("load_format"):
        note += "; the table's load format isn't in the export, so the file uses the model's usual format: confirm it"
    if unread:
        note += (f". Columns no attribute reads through the connector are left blank: {unread} — copy them from the "
                 f"source file before reloading")
    return {**built, "rows": len(rows), "unreadable_columns": unread, **_blank_note(blank, sug["attribute"]),
            "note": note}


def _split_lines(text: str, fmt: dict) -> list[list[str]]:
    sep, q = fmt["separator"], fmt.get("quote") or ""
    if q:
        return [r for r in csv.reader(io.StringIO(text, newline=""), delimiter=sep, quotechar=q) if r != []]
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:-1].split(sep) if ln.endswith("\r") else ln.split(sep) for ln in lines]


def check_lookup_csv(entry: dict, csv_path: str, *, live_rows: list[list[str]] | None = None) -> dict[str, Any]:
    """Is a filled lookup file load-ready? Read with the table's own load format: every line splits into the
    expected fields; no value holds the separator, a quote or a line break when the table has no quote
    character; the header is the table's columns plus the added description columns, exactly; the fixed
    columns (the existing table's, and the keys) are unchanged from the template, row for row, blanks still
    blank. With live_rows (the reloaded table read through the connector), the file's readable columns must
    match the model's rows."""
    fmt = entry.get("load_format") or ASSUMED_LOAD_FORMAT
    tmpl = entry.get("template") or {}
    expected = list(tmpl.get("columns") or entry.get("columns") or [])
    fixed = list(tmpl.get("fixed_columns") or [])
    problems: list[str] = []
    warnings: list[str] = []
    try:
        with open(csv_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return {"ok": False, "csv": csv_path, "problems": [f"can't read the file: {e}"], "warnings": []}
    enc = _codec(fmt.get("encoding") or "utf-8")
    try:
        text = raw.decode(enc)
    except UnicodeDecodeError as e:
        return {"ok": False, "csv": csv_path, "problems": [f"not valid {fmt.get('encoding')}: {e}"], "warnings": []}
    if text.startswith("\ufeff"):
        problems.append("the file starts with a byte-order mark, which the loader reads into the first header")
        text = text[1:]
    if "\r\n" in text and not fmt.get("quote"):
        warnings.append("Windows line endings: the loader may keep a carriage return in the last column")
    rows = _split_lines(text, fmt)
    if fmt.get("header", True):
        header = rows[0] if rows else []
        rows = rows[1:]
        if header != expected:
            missing = [c for c in expected if c not in header]
            extra = [c for c in header if c not in expected]
            problems.append("the header doesn't match the table's columns"
                            + (f"; missing {missing}" if missing else "") + (f"; not in the table {extra}" if extra else "")
                            + ("; same columns in another order" if not missing and not extra else "")
                            + (" (sources go in the sources file, not the load file)" if any(
                                c.strip().lower() == "source" for c in extra) else ""))
    else:
        header = expected
    bad_len = [(i, len(r)) for i, r in enumerate(rows, 2 if fmt.get("header", True) else 1) if len(r) != len(expected)]
    if bad_len:
        problems.append(f"{len(bad_len)} line(s) don't split into {len(expected)} fields with separator "
                        f"{fmt['separator']!r}" + ("" if fmt.get("quote") else " and no quote character")
                        + ": " + ", ".join(f"line {i} has {n}" for i, n in bad_len[:10])
                        + (" — a value holds the separator: rephrase it" if any(n > len(expected) for _, n in bad_len)
                           else ""))
    for i, r in enumerate(rows, 2 if fmt.get("header", True) else 1):
        for j, v in enumerate(r):
            bad = [b for b in _value_problems(v, fmt) if not b.startswith("the separator")]
            if bad:
                problems.append(f"line {i}, column '{header[j] if j < len(header) else j}': holds " + " and ".join(bad))
    idx = {c: header.index(c) for c in fixed if c in header}
    if tmpl.get("rows") is not None and len(idx) == len(fixed):
        got = [[r[idx[c]] if idx[c] < len(r) else "" for c in fixed] for r in rows]
        want = [[str(x) for x in r] for r in tmpl["rows"]]
        if len(got) != len(want):
            problems.append(f"{len(got)} rows, the template has {len(want)}: rows were added or removed")
        changed = [(i, w, g) for i, (w, g) in enumerate(zip(want, got), 1) if w != g]
        filled = [(i, c) for i, w, g in changed for c, a, b in zip(fixed, w, g) if a == "" and b != ""]
        if changed:
            problems.append(f"{len(changed)} row(s) change the table's existing columns {fixed}, which must stay as "
                            f"the template has them: " + "; ".join(f"row {i}: {w} -> {g}" for i, w, g in changed[:5]))
        if filled:
            problems.append(f"{len(filled)} blank value(s) filled in existing columns (a blank must stay blank): "
                            + ", ".join(f"row {i} '{c}'" for i, c in filled[:10]))
    elif fixed and len(idx) != len(fixed):
        warnings.append("the existing columns couldn't all be found, so rows weren't compared with the template")
    descs = [c for c in expected if c not in fixed and c in header]
    filled_desc = sum(1 for r in rows for c in descs if header.index(c) < len(r) and r[header.index(c)].strip())
    src = entry.get("sources_csv")
    if descs and src and os.path.exists(src):
        with open(src, encoding="utf-8", newline="") as f:
            srows = list(csv.reader(f))[1:]
        cited = sum(1 for r in srows for v in r[1:] if v.strip())
        if filled_desc and not cited:
            warnings.append("descriptions are filled but the sources file names no source")
    live: dict[str, Any] = {}
    if live_rows is not None:
        read_as = entry.get("read_as") or {}
        cols = [c for c in expected if read_as.get(c) and c in header]
        file_set = {tuple(r[header.index(c)] if header.index(c) < len(r) else "" for c in cols) for r in rows}
        model_set = {tuple(r) for r in live_rows}
        only_file, only_model = sorted(file_set - model_set), sorted(model_set - file_set)
        live = {"columns": cols, "rows_in_model": len(model_set), "rows_in_file": len(file_set),
                "only_in_file": [list(x) for x in only_file[:20]], "only_in_model": [list(x) for x in only_model[:20]]}
        if only_file or only_model:
            problems.append(f"the reloaded table doesn't match the file on {cols}: {len(only_file)} row(s) only in the "
                            f"file, {len(only_model)} only in the model (a shifted column or a cut value)")
    return {"ok": not problems, "csv": csv_path, "load_format": fmt, "rows": len(rows),
            "descriptions_filled": filled_desc, "problems": problems, "warnings": warnings,
            **({"live": live} if live else {})}


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


def _csv_header(path: str, fmt: dict | None = None) -> list[str]:
    fmt = fmt or {"separator": ",", "quote": '"'}
    try:
        with open(path, encoding=_codec(fmt.get("encoding") or "utf-8"), newline="") as f:
            first = f.readline().rstrip("\r\n").lstrip("\ufeff")
    except (OSError, UnicodeDecodeError):
        return []
    rows = _split_lines(first + "\n", {"separator": fmt.get("separator") or ",", "quote": fmt.get("quote") or ""})
    return rows[0] if rows else []


def lookup_steps(args: dict) -> str:
    """Manual steps for adding a lookup table. `new_attributes` become attributes of their own (related
    to the key attribute): anything a metric filters on, such as a flag, must be an attribute, because
    an embedded filter targets attributes, not forms. `new_forms` become forms of the key attribute
    (descriptions, labels)."""
    key = args.get("key_attribute") or "<key attribute>"
    attrs = list(args.get("new_attributes") or [])
    forms = list(args.get("new_forms") or [])
    if args.get("extends_table"):
        steps = [f"Replace the data of the existing table '{args['extends_table']}' with {args.get('csv') or '<csv file>'} "
                 f"(it keeps {', '.join(args.get('keep_columns') or [])}): its attributes keep their columns, so the "
                 f"metrics that filter on them keep working.",
                 f"Then re-run the questions and metrics that filter on attributes of '{args['extends_table']}'."]
    else:
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
    steps += [f"Give the CSV's key column the same type as '{key}' (codes such as 01 stay text; set-column-types "
              f"can change it after the table is added).",
              f"Check that '{key}' uses the new table as its lookup (auto-detection may keep the fact table).",
              "Load the file as it is, with the table's own import settings (separator, quote character, header, "
              "encoding)" + (f": {_format_text(args['load_format'])}" if args.get("load_format") else "") +
              ". check-lookup has confirmed it splits into the table's columns; its sources are in a separate file "
              "that is not loaded.",
              "The phase republishes once at the end; then re-run the questions that depend on this lookup."]
    return " ".join(f"{i}. {t}" for i, t in enumerate(steps, 1))


def _format_text(fmt: dict) -> str:
    sep = {"\t": "tab"}.get(fmt.get("separator"), repr(fmt.get("separator")))
    q = repr(fmt["quote"]) if fmt.get("quote") else "no quote character"
    return f"separator {sep}, {q}, {'a' if fmt.get('header', True) else 'no'} header row, {fmt.get('encoding')}"


def lookups_checklist(items: list[tuple[dict, bool]]) -> str:
    """One manual step for every lookup table of a phase: shared instructions once, then one line per table.
    `items` is [(add_lookup_table args, key retyped by a command after this step)]."""
    lines = []
    for i, (a, retyped) in enumerate(items, 1):
        key = a.get("key_attribute") or "<key attribute>"
        bits = [f"{a.get('csv') or '<csv file>'} -> key '{key}'"
                + (f" (REPLACES the data of existing table '{a['extends_table']}', keeping "
                   f"{', '.join(a.get('keep_columns') or [])})" if a.get("extends_table") else "")]
        if a.get("new_attributes"):
            bits.append("attributes: " + ", ".join(a["new_attributes"]))
        if a.get("new_forms"):
            bits.append("forms: " + ", ".join(a["new_forms"]))
        if not (a.get("new_attributes") or a.get("new_forms")):
            bits.append("new columns: attributes if a metric filters on them, otherwise forms")
        if not retyped:
            bits.append(f"key column typed like '{key}'")
        if a.get("load_format"):
            bits.append(f"load with {_format_text(a['load_format'])}")
        lines.append(f"({i}) " + "; ".join(bits) + ".")
    head = (f"{'Add these ' + str(len(items)) + ' lookup tables, each the same way' if len(items) > 1 else 'Add this lookup table'}"
            f": add the CSV as a new table; map its key column to the EXISTING attribute named (keep it: "
            f"deleting or replacing it deletes the derived metrics that filter on it); create the listed attributes "
            f"as attributes of their own, related to the key, since metrics filter on them; add the listed forms as "
            f"forms of the key (display only); check the key attribute uses the new table as its lookup "
            f"(auto-detection may keep the fact table); load each file with its table's import settings (shown per "
            f"table); sources are in separate files that are not loaded."
            + (" Key types that must change are set by the commands right after this step." if any(r for _, r in items)
               else "") + " The phase republishes once at the end.")
    return head + " Tables: " + " ".join(lines)


def split_test_steps(steps) -> tuple[list[str], list[str]]:
    """Manual steps -> (work, tests). Tests ("Test after republish: ...", "Check that ...") are checked by
    /verify-changes after the phase; they don't stop the apply script."""
    if isinstance(steps, (list, tuple)):
        items = [str(x).strip() for x in steps if str(x).strip()]
    else:
        items = [x.strip() for x in re.split(r"(?:^|\s)\d+\.\s+", str(steps or "")) if x.strip()]
    tests = [x for x in items if _TEST_STEP.match(x)]
    return [x for x in items if x not in tests], tests


def column_type_set_args(changes: list[dict]) -> list[str]:
    """--set COLUMN TYPE [DATE_FORMAT] for each change_column_type action."""
    out: list[str] = []
    for c in changes:
        out += ["--set", str(c.get("column") or ""), str(c.get("type") or "").lower()]
        if c.get("date_format"):
            out.append(str(c["date_format"]))
    return out


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


def rows_digest(rows: list[dict] | None) -> str:
    """A stable fingerprint of a result, to tell whether it changed between rounds."""
    return hashlib.sha256(json.dumps(sorted(json.dumps(r, sort_keys=True) for r in rows or []))
                          .encode("utf-8")).hexdigest()[:12]


def _compare(q: dict, rows: list[dict], ref: list[dict], tol: float) -> tuple[str, dict | None, str | None]:
    """-> (verdict, comparison, error note) with the question's key and measures."""
    try:
        cmp = svm.compare_rows(rows or [], ref or [], list(q.get("key") or []), list(q.get("measures") or []),
                               float(tol), str(q.get("id")))
        return {"ok": "pass", "mismatch": "fail", "empty": "empty"}.get(cmp["status"], "fail"), cmp, None
    except SystemExit as e:
        msg = str(e)
        if "duplicate keys" in msg:
            msg += (" — the result repeats a group. Grouping by an expression of an attribute doesn't regroup in "
                    "Mosaic: rewrite the SQL to group by the attribute or its time attribute (reason kind sql_error)")
        return "error", None, f"comparison: {msg}"


_SQL_KW_END = re.compile(r"\b(having|order\s+by|limit|union|except|intersect|window|qualify|offset|fetch)\b", re.I)


def _split_top(text: str, sep: str = ",") -> list[str]:
    out, depth, cur = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [x.strip() for x in out if x.strip()]


def _strip_alias(item: str) -> tuple[str, str]:
    m = re.match(r"^(.*?)\s+(?:as\s+)?(\"[^\"]+\"|[A-Za-z_]\w*)$", item.strip(), re.I | re.S)
    if m and not re.fullmatch(r"\"[^\"]+\"|[A-Za-z_]\w*", item.strip()) and not m.group(1).rstrip().endswith(
            (".", "(")):
        return m.group(1).strip(), m.group(2).strip('"').lower()
    return item.strip(), ""


def grouped_expressions(sql: str) -> list[str]:
    """Expressions a query groups by, at any level, instead of a plain column: in Mosaic grouping by an
    expression computed from an attribute doesn't regroup (each underlying value stays its own row), even in
    an outer query over a subquery. Group by the attribute itself, or its time attribute."""
    text = _SQL_LITERAL.sub("''", sql or "")
    depth, d = [], 0
    for ch in text:
        if ch == "(":
            d += 1
            depth.append(d)
        elif ch == ")":
            depth.append(d)
            d -= 1
        else:
            depth.append(d)
    low = text.lower()

    def at(i: int) -> int:
        return depth[i] if i < len(depth) else 0

    def region_end(i: int, dd: int) -> int:
        j = i
        while j < len(text) and not (text[j] == ")" and at(j) == dd):
            j += 1
        return j

    def find(word: str, start: int, stop: int, dd: int) -> int:
        for m in re.finditer(word, low[start:stop]):
            k = start + m.start()
            if at(k) == dd:
                return k
        return -1
    found: list[str] = []
    bare = re.compile(r'(?:(?:"[^"]+"|[A-Za-z_]\w*)\.)?(?:"[^"]+"|[A-Za-z_]\w*)')
    for m in re.finditer(r"\bselect\b", low):
        i = m.start()
        dd = at(i)
        end = region_end(i, dd)
        f = find(r"\bfrom\b", m.end(), end, dd)
        g = find(r"\bgroup\s+by\b", m.end(), end, dd)
        if f < 0 or g < 0 or g < f:
            continue
        gstart = g + len(re.match(r"group\s+by", low[g:]).group(0))
        stop = end
        for kw in re.finditer(_SQL_KW_END, low[gstart:end]):
            if at(gstart + kw.start()) == dd:
                stop = gstart + kw.start()
                break
        select_items = _split_top(text[m.end():f])
        aliases = {}
        for it in select_items:
            expr, alias = _strip_alias(it)
            if alias:
                aliases[alias] = expr
        for item in _split_top(text[gstart:stop]):
            expr = item
            if re.fullmatch(r"\d+", item) and 0 < int(item) <= len(select_items):
                expr = _strip_alias(select_items[int(item) - 1])[0]
            elif item.strip('"').lower() in aliases:
                expr = aliases[item.strip('"').lower()]
            e = expr.strip()
            if not bare.fullmatch(e) and not re.fullmatch(r"-?\d+(\.\d+)?|''", e):
                found.append(e)
    return found


def _sql_lint(sql: str) -> list[str]:
    bad = grouped_expressions(sql)
    return [f"it groups by an expression of an attribute ({', '.join(b[:60] for b in bad)}), which doesn't regroup in "
            f"Mosaic: group by the attribute itself or its time attribute ((year), (month) forms)"] if bad else []


def best_answer_version(q: dict) -> int:
    return len((q.get("best_answer") or {}).get("history") or []) + 1


def baseline_check(q: dict, res: dict) -> dict[str, Any]:
    """An ungraded question with a frozen, reviewed answer: is today's answer the same? A difference is
    'changed', to review (an intended fix or a regression), never a pass or fail: a baseline says what the
    model answered when someone checked it, not that it was right."""
    base = q["baseline"]
    try:
        cmp = svm.compare_rows(res.get("rows") or [], list(base.get("rows") or []), list(q.get("key") or []),
                               list(q.get("measures") or []), float(base.get("tolerance", 1e-6)), str(q.get("id")),
                               allow_empty=True)
    except SystemExit as e:   # no numeric columns: compare the rows as they are
        same = sorted(json.dumps(r, sort_keys=True) for r in res.get("rows") or []) == \
               sorted(json.dumps(r, sort_keys=True) for r in base.get("rows") or [])
        return {"baseline": "same" if same else "changed", "baseline_round": base.get("round"),
                "baseline_note": f"compared as text ({e})"}
    return {"baseline": "same" if cmp["status"] == "ok" else "changed", "baseline_round": base.get("round"),
            "baseline_comparison": cmp}


BEST_ANSWER_REASONS = ("initial", "decision", "definition", "data_change", "sql_error", "model_change")
# decision: the user decided a definition. definition: Claude chose one; the workbook asks the user to confirm.
_WS = re.compile(r"\s+")


def set_best_answer(bank: dict, qid: str, *, sql: str = "", not_answerable: str = "", reason_kind: str,
                    reason: str = "", rationale: str = "", round_no: int | None = None) -> dict[str, Any]:
    """Record a question's best answer: expert SQL over the model's own columns (or why none exists), with
    its rationale. Later changes are overrides with a recorded reason: the user's decision, a data change,
    or an error found in the earlier SQL. The history keeps every earlier version."""
    q = next((x for x in bank.get("questions") or [] if x.get("id") == qid), None)
    problems = []
    if q is None:
        return {"ok": False, "problems": [f"unknown question {qid}"]}
    if reason_kind not in BEST_ANSWER_REASONS:
        problems.append(f"reason kind must be one of {', '.join(BEST_ANSWER_REASONS)}")
    if bool(sql.strip()) == bool(not_answerable.strip()):
        problems.append("give the best-answer SQL or a not-answerable reason, not both")
    current = q.get("best_answer")
    if reason_kind == "initial" and current:
        problems.append(f"{qid} has a best answer; change it with reason kind decision, definition, data_change, "
                        f"sql_error or model_change")
    if reason_kind != "initial" and not current:
        problems.append(f"{qid} has no best answer yet; set it with reason kind initial")
    if reason_kind != "initial" and not reason.strip():
        problems.append("an override needs --reason: what was decided, what changed in the data, or what was wrong")
    if sql.strip() and _WS.sub(" ", sql.strip()).lower() == _WS.sub(" ", (q.get("sql") or "").strip()).lower():
        problems.append("the best answer must be independent of the question's own SQL, or it proves nothing")
    problems += [f"the best answer: {p}" for p in _sql_lint(sql)]
    if problems:
        return {"ok": False, "problems": problems}
    history = list((current or {}).get("history") or [])
    if current:
        history.append({k: current.get(k) for k in ("sql", "not_answerable", "rationale", "round", "reason_kind",
                                                     "reason") if current.get(k) is not None})
    q["best_answer"] = {k: v for k, v in {"sql": sql.strip() or None, "not_answerable": not_answerable.strip() or None,
                                          "rationale": rationale.strip() or (current or {}).get("rationale"),
                                          "round": round_no, "reason_kind": reason_kind, "reason": reason.strip() or None,
                                          "history": history}.items() if v not in (None, [])}
    return {"ok": True, "id": qid, "version": best_answer_version(q)}


QUESTION_SQL_REASONS = ("new_object", "sql_error", "type_change", "decision", "model_change")
# model_change: the model renamed a column the SQL names (a form added, an object renamed); rewritten by
# `references --apply`, never to change what the SQL computes.


def set_question_sql(bank: dict, qid: str, *, sql: str = "", not_answerable: bool = False, reason_kind: str,
                     reason: str, round_no: int | None = None, path: str = "") -> dict[str, Any]:
    """Rewrite a question's governed SQL: a phase added the object that answers it (new_object), the SQL was
    wrong (sql_error), a type change altered the values it filters on (type_change), or the user decided
    (decision). The earlier SQL stays in `sql_history`, so the round it became answerable is on record."""
    q = next((x for x in bank.get("questions") or [] if x.get("id") == qid), None)
    if q is None:
        return {"ok": False, "problems": [f"unknown question {qid}"]}
    problems = []
    if reason_kind not in QUESTION_SQL_REASONS:
        problems.append(f"reason kind must be one of {', '.join(QUESTION_SQL_REASONS)}")
    if not reason.strip():
        problems.append("give --reason: the object that now answers it, or what was wrong")
    if bool(sql.strip()) == bool(not_answerable):
        problems.append("give the new SQL, or --not-answerable, not both")
    if sql.strip() and _WS.sub(" ", sql.strip()).lower() == _WS.sub(" ", (q.get("sql") or "").strip()).lower():
        problems.append("the SQL is unchanged")
    best = (q.get("best_answer") or {}).get("sql") or ""
    if sql.strip() and best and _WS.sub(" ", sql.strip()).lower() == _WS.sub(" ", best.strip()).lower():
        problems.append("the question's SQL can't be its best answer: then the grade proves nothing")
    if path and path not in PATHS:
        problems.append(f"path must be one of {', '.join(PATHS)}")
    problems += [f"the SQL: {p}" for p in _sql_lint(sql)]
    if problems:
        return {"ok": False, "problems": problems}
    history = list(q.get("sql_history") or [])
    history.append({"sql": q.get("sql") or None, "expected_path": q.get("expected_path"), "until_round": round_no,
                    "reason_kind": reason_kind, "reason": reason.strip()})
    q["sql_history"] = history
    q["sql"] = sql.strip() if sql.strip() else ""
    if not_answerable:
        q["expected_path"] = "not_answerable"
    elif path:
        q["expected_path"] = path
    elif q.get("expected_path") == "not_answerable":
        q["expected_path"] = "governed_metric" if reason_kind == "new_object" else "simple_sql"
    return {"ok": True, "id": qid, "expected_path": q.get("expected_path"), "versions": len(history) + 1}


def freeze_baselines(bank: dict, results: dict, ids: list[str], *, round_no: int | None, note: str = "",
                     force: bool = False) -> dict[str, Any]:
    """Save reviewed answers of ungraded questions as baselines (in place on `bank`)."""
    by_id = {r.get("id"): r for r in results.get("results") or []}
    done, skipped = [], []
    for q in bank.get("questions") or []:
        if q.get("id") not in ids:
            continue
        res = by_id.get(q["id"])
        if (q.get("expected") or {}).get("source") not in (None, "none") and not force:
            skipped.append({"id": q["id"], "why": "has an expected value; a baseline is for ungraded questions"})
        elif not res or not res.get("ok"):
            skipped.append({"id": q["id"], "why": "no successful result to freeze"})
        else:
            q["baseline"] = {"rows": res.get("rows") or [], "round": round_no, "note": note}
            done.append(q["id"])
    missing = [i for i in ids if i not in {q.get("id") for q in bank.get("questions") or []}]
    return {"frozen": done, "skipped": skipped, "unknown": missing}


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
        best_q = q.get("best_answer") or {}
        if best_q:
            item["best_answer_version"] = best_answer_version(q)
            if best_q.get("not_answerable"):
                item["best_answer"] = f"not answerable: {best_q['not_answerable']}"
        if res is not None:
            item["cost"] = add_costs(res.get("cost"), (res.get("expected_run") or {}).get("cost"))
            brun = res.get("best_run") or {}
            if brun:
                item["best_cost"] = add_costs(brun.get("cost"))
                item["best_digest"] = rows_digest(brun.get("rows")) if brun.get("ok") else None
                if not brun.get("ok"):
                    item["best_answer_error"] = brun.get("error")
        if res is None:
            item["verdict"] = "not_run"
        elif res.get("skipped"):
            item["verdict"] = "ungraded" if q.get("expected_path") == "not_answerable" else "error"
            item["note"] = res.get("error")
            item["no_sql"] = True
        elif not res.get("ok"):
            # a timeout says the query is slow, not that the model answered wrong
            timed = bool(re.search(r"timed?\s*out|timeout|deadline exceeded", str(res.get("error") or ""), re.I))
            item.update({"verdict": "timeout" if timed else "error", "note": res.get("error")})
        else:
            ref, why = _reference_rows(q, res, bank_dir)
            tol = exp.get("tolerance", tolerance if tolerance is not None else 1e-6)
            outside = exp.get("source") in ("inline", "reference_file")
            best = res.get("best_run") or {}
            if not outside and best.get("ok"):
                # no outside reference: the best answer ranks above a plain same-model check
                ref, why = list(best.get("rows") or []), ""
                item["strength"], item["graded_against"] = "best_answer", "best_answer"
            if ref is None:
                item.update({"verdict": "ungraded" if why == "no expected value" else "error", "note": why})
            else:
                verdict, cmp, err = _compare(q, res.get("rows") or [], ref, tol)
                item["verdict"] = verdict
                if cmp:
                    item["comparison"] = cmp
                if err:
                    item["note"] = err
            if outside and best.get("ok") and ref is not None:   # an outside reference checks the best answer too
                item["best_vs_reference"] = _compare(q, best.get("rows") or [], ref, tol)[0]
            item["seconds"] = res.get("seconds")
            item["rows_digest"] = rows_digest(res.get("rows"))
            if item["verdict"] == "ungraded" and q.get("baseline") and res.get("ok"):
                item.update(baseline_check(q, res))
        graded.append(item)

    summary: dict[str, Any] = {"total": len(graded), "by_verdict": {}, "by_level": {}, "by_expected_path": {},
                               "by_strength": {}, "by_use_case": {}}
    for g in graded:
        summary["by_verdict"][g["verdict"]] = summary["by_verdict"].get(g["verdict"], 0) + 1
        for key, val in (("by_level", str(g["level"])), ("by_expected_path", str(g["expected_path"])),
                         ("by_strength", str(g["strength"])), ("by_use_case", str(g["use_case"]))):
            bucket = summary[key].setdefault(val, {})
            bucket[g["verdict"]] = bucket.get(g["verdict"], 0) + 1
    summary["cost"] = {"answers": add_costs(*[g.get("cost") for g in graded]),
                       "best_answers": add_costs(*[g.get("best_cost") for g in graded]), "by_level": {}}
    for g in graded:
        lvl = summary["cost"]["by_level"].setdefault(str(g["level"]), {"answers": add_costs(), "best_answers": add_costs()})
        lvl["answers"] = add_costs(lvl["answers"], g.get("cost"))
        lvl["best_answers"] = add_costs(lvl["best_answers"], g.get("best_cost"))
    summary["cost"]["model_description"] = results.get("semantics_cost")
    summary["cost"]["note"] = (f"connector I/O only: SQL sent plus replies received, ~{CHARS_PER_TOKEN} characters "
                               f"per token; Claude's own reasoning is not included")
    summary["baseline"] = {}
    for g in graded:
        if g.get("baseline"):
            summary["baseline"][g["baseline"]] = summary["baseline"].get(g["baseline"], 0) + 1
    out: dict[str, Any] = {"version": 1, "model": bank.get("model"), "summary": summary, "questions": graded,
                           "baseline_changes": [g["id"] for g in graded if g.get("baseline") == "changed"]}
    if previous:
        before = {g.get("id"): g.get("verdict") for g in previous.get("questions") or []}
        changes = [{"id": g["id"], "before": before[g["id"]], "after": g["verdict"]}
                   for g in graded if g["id"] in before and before[g["id"]] != g["verdict"]]
        out["changes"] = changes
        out["regressions"] = [c["id"] for c in changes if c["before"] == "pass"]
        prev = {g.get("id"): g for g in previous.get("questions") or []}
        moved, overridden, suspicious = [], [], []
        for g in graded:
            p = prev.get(g["id"]) or {}
            if not (p.get("best_digest") and g.get("best_digest")) or p["best_digest"] == g["best_digest"]:
                continue
            if p.get("best_answer_version") == g.get("best_answer_version"):
                moved.append(g["id"])            # same SQL, different result: the data or model changed under it
                if g.get("verdict") == "pass" and p.get("verdict") != "pass" and \
                        p.get("rows_digest") != g.get("rows_digest"):
                    suspicious.append(g["id"])   # both answers moved this round and now agree: check why
            else:
                overridden.append(g["id"])
        out["best_answer_moved"], out["best_answer_overridden"] = moved, overridden
        out["suspicious_agreement"] = suspicious
        # Answerability: questions that could not run before and now do, and paths that got simpler.
        rank = {p: i for i, p in enumerate(PATHS)}
        runnable_before = {g.get("id") for g in previous.get("questions") or []
                           if g.get("verdict") not in ("error", "timeout", "not_run") and not g.get("no_sql")
                           and not str(g.get("note") or "").startswith("no sql")}
        newly, simpler = [], []
        for g in graded:
            p = prev.get(g["id"])
            if not p:
                continue
            if g["id"] not in runnable_before and g.get("verdict") not in ("error", "timeout", "not_run") and not g.get("no_sql"):
                newly.append(g["id"])
            before_path, after_path = p.get("expected_path"), g.get("expected_path")
            if before_path in rank and after_path in rank and rank[after_path] < rank[before_path]:
                simpler.append({"id": g["id"], "before": before_path, "after": after_path})
        out["answerability"] = {"newly_runnable": newly, "simpler_path": simpler,
                                "governed_now": sum(1 for g in graded if g.get("expected_path") == "governed_metric"),
                                "governed_before": sum(1 for g in previous.get("questions") or []
                                                       if g.get("expected_path") == "governed_metric")}
    return out


# ── plan ─────────────────────────────────────────────────────────────────────────────────────

def _phase_of(f: dict) -> int | None:
    if f.get("phase") in PHASES:
        return int(f["phase"])
    return CATEGORY_PHASE.get(str(f.get("category") or ""))


def action_phase(f: dict, action: dict) -> int | None:
    """The phase an action runs in: merged kinds have a fixed phase, the rest run in the finding's."""
    return MERGED_KINDS.get(str(action.get("kind") or "")) or _phase_of(f)


def pending_actions(f: dict) -> list[dict]:
    """Actions of an applied or verified finding whose phase isn't in its `applied_phases` yet. A finding can
    span phases (a metric in phase 4, its description in phase 6): verifying the first part must not drop the
    rest from the plan."""
    applied = f.get("applied_phases")
    if f.get("status") not in ("applied", "verified") or not isinstance(applied, list):
        return []
    done = {int(p) for p in applied if str(p).isdigit()}
    return [a for a in f.get("actions") or [] if action_phase(f, a) not in done]


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

    for key in ("name", "column", "source_metric", "entity_attribute", "attribute"):   # templates from roles.json
        if re.fullmatch(r"<[^<>]*>", str(a.get(key) or "").strip()):
            problems.append(f"{kind}: args.{key} is still a placeholder ({a[key]}); fill it in")

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
    if kind == "create_level_metric":
        need("name", "source_metric", "entity_attribute")
        argv = base + ["create-level-metric", "--model-id", model_id, "--name", str(a.get("name", "")),
                       "--source-metric", str(a.get("source_metric", "")),
                       "--entity-attribute", str(a.get("entity_attribute", ""))]
        for flag, key in (("--inner", "inner"), ("--outer", "outer"), ("--format", "format"),
                          ("--description", "description")):
            opt(argv, flag, key)
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "create_weighted_average":
        need("name", "weight_metric", "entity_attribute")
        if bool(a.get("value_attribute")) == bool(a.get("value_metric")):
            problems.append(f"{kind}: give exactly one of args.value_attribute or args.value_metric")
        argv = base + ["create-weighted-average", "--model-id", model_id, "--name", str(a.get("name", ""))]
        opt(argv, "--value-attribute", "value_attribute")
        opt(argv, "--value-metric", "value_metric")
        argv += ["--weight-metric", str(a.get("weight_metric", "")), "--entity-attribute",
                 str(a.get("entity_attribute", ""))]
        for flag, key in (("--numerator-name", "numerator_name"), ("--denominator-name", "denominator_name"),
                          ("--format", "format"), ("--description", "description")):
            opt(argv, flag, key)
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
        elif _TYPE_CHANGE_TEXT.search(a.get("change") or ""):
            problems.append(f"{kind}: '{a.get('column') or a.get('change')}' looks like a column type change: use "
                            f"change_column_type (made in the model, no reload), or reword the change if the source "
                            f"values themselves must change")
        return {"dry_run": None, "apply": None, "manual": None, "problems": problems}
    if kind == "change_column_type":
        need("column", "type")
        t = str(a.get("type") or "").lower()
        if t and t not in COLUMN_TYPES:
            problems.append(f"{kind}: type '{a.get('type')}' is not one of {', '.join(COLUMN_TYPES)}")
        if t in ("date", "timestamp") and not a.get("date_format"):
            problems.append(f"{kind}: {t} needs args.date_format (the source pattern, e.g. yyyyMM)")
        argv = base + ["set-column-types", "--model-id", model_id] + column_type_set_args([a])
        opt(argv, "--table", "table")
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "add_time_attributes":
        bad = [x for x in a.get("intervals") or [] if str(x).lower() not in TIME_INTERVALS]
        if not a.get("attribute") or not a.get("intervals") or bad:
            problems.append(f"{kind}: args need attribute and intervals from {', '.join(TIME_INTERVALS)}"
                            + (f" (unknown: {bad})" if bad else ""))
        return {"dry_run": [], "apply": [], "manual": None, "problems": problems}
    if kind == "rename_describe":
        items = a.get("items")
        if not isinstance(items, list) or not items:
            problems.append(f"{kind}: args.items must list object_type, current_name and new_name or description")
        for it in items if isinstance(items, list) else []:
            if str(it.get("object_type") or "").lower() not in ("attribute", "fact_metric"):
                problems.append(f"{kind}: '{it.get('current_name')}': object_type must be attribute or fact_metric "
                                f"(a derived metric takes its description when it is created)")
            if not it.get("current_name") or not (it.get("new_name") or it.get("description") is not None):
                problems.append(f"{kind}: each item needs current_name and a new_name or description")
            if len(str(it.get("description") or "")) > DESCRIPTION_LIMIT:
                problems.append(f"{kind}: the description of '{it.get('current_name')}' is over {DESCRIPTION_LIMIT} "
                                f"characters")
        argv = base + ["rename-describe", "--model-id", model_id, "--spec", RENAME_DESCRIBE_SPEC]
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "add_column_attribute":   # one column as-is: an attribute role for a column read as a measure
        need("name", "column")
        argv = base + ["add-column-attribute", "--model-id", model_id, "--name", str(a.get("name", "")),
                       "--column", str(a.get("column", ""))]
        for flag, key in (("--description", "description"), ("--display-format", "display_format"),
                          ("--data-type", "data_type"), ("--table", "table")):
            opt(argv, flag, key)
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "create_formula_attribute":
        need("name", "formula")
        if a.get("requested_by_user") is not True:
            problems.append(f"{kind}: '{a.get('name')}' is a derived attribute. Prefer a lookup table (faster at run "
                            f"time, and replaceable without deleting the metrics built on it); plan a derived "
                            f"attribute only when the user asks for one, then set args.requested_by_user")
        argv = base + ["create-formula-attribute", "--model-id", model_id, "--name", str(a.get("name", "")),
                       "--formula", str(a.get("formula", ""))]
        for flag, key in (("--description", "description"), ("--display-format", "display_format"),
                          ("--data-type", "data_type"), ("--table", "table")):
            opt(argv, flag, key)
        if a.get("update"):
            argv.append("--update")
        if a.get("unverified_ok"):
            argv.append("--unverified-ok")
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "wire_relationships":
        rels = a.get("relationships")
        if not isinstance(rels, list) or not rels:
            problems.append(f"{kind}: args.relationships must list parent_attribute, child_attribute, type "
                            f"and (on a model with several tables) relationship_table")
        for r in rels if isinstance(rels, list) else []:
            missing = [k for k in ("parent_attribute", "child_attribute") if not r.get(k)]   # table: optional
            if missing:
                problems.append(f"{kind}: a relationship is missing {missing}")
            if r.get("type", "one_to_many") not in RELATIONSHIP_TYPES:
                problems.append(f"{kind}: type '{r.get('type')}' is not one of {', '.join(RELATIONSHIP_TYPES)}")
        argv = base + ["wire-relationships", "--model-id", model_id, "--hints", RELATIONSHIP_HINTS]
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "add_lookup_table":
        need("csv", "key_attribute")
        if a.get("extends_table"):   # replacing an existing table's data: it must keep every column read from it
            keep = [str(c) for c in a.get("keep_columns") or []]
            cols = [str(c) for c in a.get("columns") or []] or _csv_header(str(a.get("csv") or ""), a.get("load_format"))
            if not keep:
                problems.append(f"{kind}: extends_table needs keep_columns (the columns its attributes read)")
            elif cols:
                lost = [c for c in keep if c.casefold() not in {x.casefold() for x in cols}]
                if lost:
                    problems.append(f"{kind}: the CSV for '{a['extends_table']}' drops {lost}, which attributes still "
                                    f"read; add them back or the attributes lose their source")
        cols = [str(c) for c in a.get("columns") or []] or _csv_header(str(a.get("csv") or ""), a.get("load_format"))
        if any(c.strip().lower() == "source" for c in cols):
            problems.append(f"{kind}: {a.get('csv')} has a source column the table doesn't have; move sources to the "
                            f"sources file, then run check-lookup")
        chk = a.get("check") or {}
        if chk and not chk.get("ok"):
            problems.append(f"{kind}: check-lookup failed for {a.get('csv')}: " + "; ".join(chk.get("problems") or []))
        out = {"dry_run": None, "apply": None, "manual": lookup_steps(a), "problems": problems}
        if a.get("extends_table"):
            # no API replaces a file table's data in place, so the reload is by hand, and a reload has dropped
            # attribute relationships (seen live): export before and after, and stop if any was lost
            slug = _slug(str(a["extends_table"]))
            before, after = f"ignore/exports/before_reload_{slug}.json", f"ignore/exports/after_reload_{slug}.json"
            out["guard_before"] = [base + ["export-model", "--model-id", model_id, "--out", before]]
            out["guard_after"] = [base + ["export-model", "--model-id", model_id, "--out", after],
                                  [python, ASSESS_SCRIPT, "relationship-diff", "--before", before, "--after", after]]
        return out
    if kind == "show_forms":
        forms = _arg_list(a.get("forms"))
        if not a.get("attribute") or not forms:
            problems.append(f"{kind}: args need attribute and forms (names or categories)")
        argv = base + ["show-forms", "--model-id", model_id, "--attribute", str(a.get("attribute", "")),
                       "--forms", ",".join(forms)]
        return {"dry_run": argv + ["--dry-run"], "apply": argv, "manual": None, "problems": problems}
    if kind == "publish":
        return {"dry_run": None, "apply": base + ["publish", "--model-id", model_id] + PUBLISH_WAIT, "manual": None,
                "problems": problems}
    if kind == "certify":
        return {"dry_run": None, "apply": base + ["certify", "--object-id", model_id], "manual": None,
                "problems": problems}
    steps = action.get("steps") or a.get("steps")
    if isinstance(steps, list):    # a list of steps reads as one numbered paragraph
        steps = " ".join(f"{i}. {str(x).strip().rstrip('.')}." for i, x in enumerate(steps, 1) if str(x).strip())
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


def form_base_name(name: str) -> str:
    """'<attribute>__<form>' or '<attribute> (<form>)' (how the connector shows a form) -> '<attribute>', else ''."""
    n = (name or "").strip()
    if "__" in n:
        return n.split("__", 1)[0].strip()
    hit = re.match(r"^(.*\S)\s+\(([^()]+)\)$", n)
    return hit.group(1) if hit else ""


def build_plan(findings_doc: dict, *, model_id: str = "", statuses: tuple[str, ...] = PLANNED_STATUSES,
               python: str = "python3", quoting: str = "posix", project: str = "",
               bank: str = "question_bank.json") -> dict[str, Any]:
    findings_doc = copy.deepcopy(findings_doc)    # inline specs become file paths below; leave the input alone
    problems: list[str] = list(check_findings(findings_doc)["problems"])
    warnings: list[str] = []
    every = findings_doc.get("findings") or []
    by_id = {f.get("id"): f for f in every}
    chosen = [f for f in every if f.get("status") in statuses]
    for f in every:   # applied or verified, with actions in phases not applied yet: plan the rest
        rest = pending_actions(f)
        if rest:
            phases_left = sorted({action_phase(f, a) for a in rest if action_phase(f, a) is not None})
            chosen.append({**f, "actions": rest, "phase": phases_left[0] if phases_left else f.get("phase")})
            warnings.append(f"{f['id']} is {f.get('status')} (phases {sorted(f.get('applied_phases') or [])} applied); "
                            f"its actions in phase(s) {phases_left} are still planned")
        elif f.get("status") in ("applied", "verified") and not isinstance(f.get("applied_phases"), list):
            spans = sorted({action_phase(f, a) for a in f.get("actions") or []} - {None})
            if len(spans) > 1:
                warnings.append(f"{f['id']} is {f.get('status')} and has actions in phases {spans}, but no "
                                f"applied_phases: if a phase isn't applied yet, record applied_phases (the phases "
                                f"that are) so the plan includes the rest")
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

    # Merged kinds run once for the whole plan, at the start of their phase: every source_change becomes one
    # rebuild in phase 1; every change_column_type becomes one set-column-types command per table in phase 2.
    merged: dict[str, list[dict]] = {k: [] for k in MERGED_KINDS}
    for f in ordered:
        for a in f.get("actions") or []:
            if a.get("kind") in MERGED_KINDS:
                merged[a["kind"]].append({**(a.get("args") or {}), "finding": f["id"]})
    merged_steps: dict[int, list[dict]] = {}
    if merged["source_change"]:
        sc = merged["source_change"]
        merged_steps.setdefault(MERGED_KINDS["source_change"], []).append(
            {"finding": ", ".join(dict.fromkeys(c["finding"] for c in sc)),
             "title": "Rebuild the source data once", "kind": "source_change", "dry_run": None,
             "apply": None, "manual": source_change_text(sc),
             "risks": ["Re-map existing attributes and fact metrics to the reloaded columns; deleting "
                       "them deletes the derived metrics that reference them."]})
    by_table: dict[str, list[dict]] = {}
    for c in merged["change_column_type"]:
        by_table.setdefault(c.get("table") or "", []).append(c)
    for table, items in by_table.items():
        dup = _dupes([str(c.get("column") or "").casefold() for c in items])
        if dup:
            problems.append(f"change_column_type: column(s) {dup} changed twice; keep one type per column")
        argv = [python, BUILD_SCRIPT, "set-column-types", "--model-id", mid] + column_type_set_args(items)
        if table:
            argv += ["--table", table]
        merged_steps.setdefault(MERGED_KINDS["change_column_type"], []).append(
            {"finding": ", ".join(dict.fromkeys(c["finding"] for c in items)),
             "title": f"Change column types in the model{' (' + table + ')' if table else ''} (no reload)",
             "kind": "change_column_type", "dry_run": cmd(argv + ["--dry-run"]), "apply": cmd(argv),
             "manual": None,
             "risks": ["The data changes only after the republish at the end of this phase; check the values "
                       "then (codes keep leading zeros, letter codes survive, dates parse)."]})
    rels = [dict(r) for c in merged["wire_relationships"] for r in c.get("relationships") or []
            if isinstance(r, dict)]
    files: dict[str, Any] = {}
    if merged["wire_relationships"]:
        files[RELATIONSHIP_HINTS] = {"relationships": rels}
        argv = [python, BUILD_SCRIPT, "wire-relationships", "--model-id", mid, "--hints", RELATIONSHIP_HINTS]
        merged_steps.setdefault(MERGED_KINDS["wire_relationships"], []).append(
            {"finding": ", ".join(dict.fromkeys(c["finding"] for c in merged["wire_relationships"])),
             "title": "Declare attribute relationships", "kind": "wire_relationships",
             "dry_run": cmd(argv + ["--dry-run"]), "apply": cmd(argv), "manual": None,
             "risks": ["The dry run lists any relationship it cannot wire (unknown attribute or table) and why."]})
    if merged["rename_describe"]:
        items = [dict(it, finding=c["finding"]) for c in merged["rename_describe"] for it in c.get("items") or []
                 if isinstance(it, dict)]
        dup = _dupes([f"{it.get('object_type')}:{str(it.get('current_name') or '').casefold()}" for it in items])
        if dup:
            problems.append(f"rename_describe: {dup} changed twice; merge them into one item")
        # A connector form name ('<attribute>__<form>', '<attribute> (<form>)') stands for its attribute: forms
        # share the attribute's description and can't be renamed by rename-describe.
        by_attr: dict[str, list[dict]] = {}
        for it in items:
            if str(it.get("object_type") or "").lower() != "attribute":
                continue
            cur = str(it.get("current_name") or "")
            base = form_base_name(cur)
            if base:
                if it.get("new_name"):
                    problems.append(f"{it['finding']}: rename_describe renames '{cur}', a connector form name of "
                                    f"'{base}'; forms can't be renamed. Rename the attribute or drop new_name")
                else:
                    warnings.append(f"{it['finding']}: '{cur}' looks like a connector form name; its description is "
                                    f"set on attribute '{base}' (shown on every form). Write one description for the "
                                    f"attribute that covers its forms")
            by_attr.setdefault((base or cur).casefold(), []).append(it)
        for key, group in by_attr.items():
            texts = {str(it.get("description")) for it in group if it.get("description") is not None}
            if len(group) > 1 and len(texts) > 1:
                problems.append(f"rename_describe: attribute '{key}' gets {len(texts)} different descriptions "
                                f"({', '.join(str(it.get('current_name')) for it in group)}); forms share the "
                                f"attribute's description, so give it one item")
        files[RENAME_DESCRIBE_SPEC] = {"items": [{k: v for k, v in it.items() if k != "finding"} for it in items]}
        argv = [python, BUILD_SCRIPT, "rename-describe", "--model-id", mid, "--spec", RENAME_DESCRIBE_SPEC]
        merged_steps.setdefault(MERGED_KINDS["rename_describe"], []).append(
            {"finding": ", ".join(dict.fromkeys(c["finding"] for c in merged["rename_describe"])),
             "title": "Set names and descriptions", "kind": "rename_describe",
             "dry_run": cmd(argv + ["--dry-run"]), "apply": cmd(argv), "manual": None,
             "risks": ["Takes effect at once (no republish). Derived metrics keep working after a rename: "
                       "they refer to objects by id."]})
    if merged["add_time_attributes"]:
        tas = merged["add_time_attributes"]
        files[TIME_ATTRIBUTES_SPEC] = {"items": [{"attribute": c.get("attribute"), "intervals": c.get("intervals")}
                                                 for c in tas]}
        argv = [python, BUILD_SCRIPT, "add-time-attributes", "--model-id", mid, "--spec", TIME_ATTRIBUTES_SPEC]
        merged_steps.setdefault(MERGED_KINDS["add_time_attributes"], []).append(
            {"finding": ", ".join(dict.fromkeys(c["finding"] for c in tas)),
             "title": "Add time attributes", "kind": "add_time_attributes",
             "dry_run": cmd(argv + ["--dry-run"]), "apply": cmd(argv), "manual": None,
             "risks": ["Runs after the phase 2 type change and republish: a number stored as a date takes time "
                       "attributes only once it is a date. Analysts' filters on the number become filters on "
                       "'<date> (month)__id' (same yyyyMM values)."]})
    merged_findings = {k: {c["finding"] for c in v} for k, v in merged.items()}
    retyped_to = {str(c.get("column") or "").casefold(): (str(c.get("type") or "").lower(), c.get("date_format") or "")
                  for c in merged["change_column_type"]}

    # A type change can already restore what a source change would rewrite (leading zeros, letter codes):
    # test the values after the type change, and change the source only if they are still wrong.
    retyped = {str(c.get("column") or "").casefold(): c["finding"] for c in merged["change_column_type"]}
    for c in merged["source_change"]:
        for col in [x.strip() for x in str(c.get("column") or "").split(",") if x.strip()]:
            if col.casefold() in retyped:
                problems.append(f"{c['finding']}: '{col}' has a source change and a type change "
                                f"({retyped[col.casefold()]}); the type change may already restore it. Keep the type "
                                f"change, add a test of the values after phase 2, and change the source only if "
                                f"that test fails")

    # Display columns (descriptions, names, labels) are forms unless a metric filters on them.
    filtered = {str(u).lower() for f in ordered for a in f.get("actions") or []
                if a.get("kind") == "create_conditional_metric"
                for u in [(a.get("args") or {}).get("attribute")] + [c[0] for c in (a.get("args") or {}).get("and") or []
                                                                    if isinstance(c, list) and c]}
    for f in ordered:
        for a in f.get("actions") or []:
            if a.get("kind") != "add_lookup_table":
                continue
            for col in (a.get("args") or {}).get("new_attributes") or []:
                if _DISPLAY_COLUMN.search(str(col)) and str(col).lower() not in filtered:
                    warnings.append(f"{f['id']}: '{col}' looks like a display column; list it under new_forms "
                                    f"unless a metric will filter on it")

    # A derived metric built on a derived attribute is hard to replace later: replacing the attribute (say, with
    # a lookup) deletes every metric that filters on it or counts it.
    formula_attrs = {str((a.get("args") or {}).get("name") or "").lower(): f["id"] for f in ordered
                     for a in f.get("actions") or [] if a.get("kind") == "create_formula_attribute"}
    for f in ordered:
        for a in f.get("actions") or []:
            args = a.get("args") or {}
            if a.get("kind") not in ("create_conditional_metric", "create_level_metric", "create_weighted_average"):
                continue
            used = [args.get(k) for k in ("attribute", "count_attribute", "level_attribute", "entity_attribute",
                                          "value_attribute")] + [c[0] for c in args.get("and") or []
                                                                 if isinstance(c, list) and c]
            for u in used:
                if u and str(u).lower() in formula_attrs:
                    warnings.append(f"{f['id']}: '{args.get('name')}' is built on '{u}', a derived attribute "
                                    f"({formula_attrs[str(u).lower()]}); replacing that attribute later deletes "
                                    f"this metric. Prefer a lookup table for it")
        for a in f.get("actions") or []:
            formula = str((a.get("args") or {}).get("formula") or "") if a.get("kind") == "create_formula_attribute" else ""
            new_fns = sorted({fn for fn in re.findall(r"([A-Za-z_]\w*)\s*\(", formula)
                              if fn.lower() not in VERIFIED_FORMULA_FUNCTIONS})
            gone = [fn for fn in new_fns if fn.lower() in MISSING_FORMULA_FUNCTIONS]
            if gone:   # observed on one tenant, so a warning: another tenant's library may differ
                warnings.append(f"{f['id']}: '{(a.get('args') or {}).get('name')}' uses {', '.join(gone)}, which a "
                                f"tenant has refused as an unknown function; prefer another formula (Case is accepted)")
                new_fns = [fn for fn in new_fns if fn not in gone]
            if new_fns:
                warnings.append(f"{f['id']}: '{(a.get('args') or {}).get('name')}' uses {', '.join(new_fns)}, not yet "
                                f"confirmed live. The dry run checks columns only; the tenant checks the functions "
                                f"when the step is applied (one changeset, discarded if refused). Record the result")
            if a.get("kind") == "create_formula_attribute" and re.search(r"'|\d\.\d", formula):
                if not (a.get("args") or {}).get("unverified_ok"):
                    warnings.append(f"{f['id']}: the formula of '{(a.get('args') or {}).get('name')}' uses a string "
                                    f"literal or a decimal, not yet confirmed live; set args.unverified_ok to try it")

    # patch_fact_metrics may carry its spec inline (args.metrics): plan writes the file into the bundle.
    for f in ordered:
        for i, a in enumerate(f.get("actions") or []):
            args = a.get("args") or {}
            if a.get("kind") == "patch_fact_metrics" and isinstance(args.get("metrics"), list) and not args.get("spec"):
                path = f"specs/{f['id']}_fact_metrics{'_' + str(i + 1) if i else ''}.json"
                files[path] = {k: args[k] for k in ("presets", "metrics") if k in args}
                a["args"] = {**args, "spec": path}

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
    phase_numbers = sorted({_phase_of(f) for f in ordered} | set(merged_steps))
    for n in phase_numbers:
        items = [f for f in ordered if _phase_of(f) == n]
        steps = list(merged_steps.get(n, []))
        noted: set[tuple[str, str]] = set()
        lookups: list[tuple[dict, bool]] = []
        lookup_at, lookup_findings, retypes = None, [], []
        for f in items:
            for action in f.get("actions") or [{"kind": "manual", "steps": f.get("recommendation")}]:
                kind = action.get("kind")
                if kind in MERGED_KINDS:
                    c = action_commands(action, mid, python)
                    problems += [f"{f['id']}: {p}" for p in c["problems"]]
                    target = MERGED_KINDS[kind]
                    if n != target and (f["id"], kind) not in noted:
                        noted.add((f["id"], kind))
                        what = MERGED_NOTES[kind].format(phase=target)
                        steps.append({"finding": f["id"], "title": f.get("title"), "kind": "note", "dry_run": None,
                                      "apply": None, "manual": None, "risks": [],
                                      "note": f"{what}; nothing to do here."})
                    continue
                c = action_commands(action, mid, python)
                problems += [f"{f['id']}: {p}" for p in c["problems"]]
                if kind == "manual":
                    work, tests = split_test_steps(action.get("steps") or (action.get("args") or {}).get("steps"))
                    if tests:
                        steps.append({"finding": f["id"], "title": f.get("title"), "kind": "verify",
                                      "dry_run": None, "apply": None, "manual": None, "risks": [],
                                      "note": "Checked by /verify-changes after this phase: " + " ".join(tests)})
                        if not work:
                            continue
                        c = {**c, "manual": " ".join(f"{i}. {w.rstrip('.')}." for i, w in enumerate(work, 1))}
                retype = None
                if kind == "add_lookup_table":
                    largs = action.get("args") or {}
                    key = str(largs.get("key_column") or largs.get("key_attribute") or "")
                    t = retyped_to.get(key.casefold()) or retyped_to.get(str(largs.get("key_attribute") or "").casefold())
                    if t:   # the fact column changes type in this plan: the lookup's key must match it to join
                        table = os.path.splitext(os.path.basename(str(largs.get("csv") or "")))[0]
                        argv = [python, BUILD_SCRIPT, "set-column-types", "--model-id", mid, "--table", table,
                                "--set", key, t[0]] + ([t[1]] if t[1] else [])
                        retype = {"finding": f["id"], "title": f"Give the {table} key the type of '{key}'",
                                  "kind": "change_column_type", "dry_run": cmd(argv + ["--dry-run"]),
                                  "apply": cmd(argv), "manual": None,
                                  "risks": ["Run after the lookup table is added; the table name is the CSV's, "
                                            "matched loosely."]}
                first = (f["id"], "risks") not in noted          # a finding's risks show on its first step only
                noted.add((f["id"], "risks"))
                if kind == "add_lookup_table":   # all of a phase's lookups become one checklist step
                    if lookup_at is None:
                        lookup_at = len(steps)
                    lookups.append((action.get("args") or {}, bool(retype)))
                    lookup_findings.append(f["id"])
                    if retype:
                        retypes.append(retype)
                    continue
                steps.append({"finding": f["id"], "title": f.get("title"), "kind": action.get("kind"),
                              "dry_run": cmd(c["dry_run"]), "apply": cmd(c["apply"]), "manual": c["manual"],
                              "risks": (f.get("risks") or []) if first else []})
                if retype:
                    steps.append(retype)
        if lookups:
            ids = list(dict.fromkeys(lookup_findings))
            risks = [r for fid in ids for f in items if f["id"] == fid for r in (f.get("risks") or [])]
            step = {"finding": ", ".join(ids), "title": f"Add {len(lookups)} lookup table"
                    f"{'s' if len(lookups) > 1 else ''}", "kind": "add_lookup_table",
                    "dry_run": None, "apply": None, "manual": lookups_checklist(lookups),
                    "risks": list(dict.fromkeys(risks))}
            if any(la.get("extends_table") for la, _ in lookups):
                # a table reloaded by hand can lose attribute relationships: export before and after, stop if so
                b4, af = f"ignore/exports/before_phase{n}_reload.json", f"ignore/exports/after_phase{n}_reload.json"
                step["guard_before"] = [cmd([python, BUILD_SCRIPT, "export-model", "--model-id", mid, "--out", b4])]
                step["guard_after"] = [cmd([python, BUILD_SCRIPT, "export-model", "--model-id", mid, "--out", af]),
                                       cmd([python, ASSESS_SCRIPT, "relationship-diff", "--before", b4, "--after", af])]
            steps[lookup_at:lookup_at] = [step] + retypes
        before = ["Save a backup copy of the model before applying this phase."]
        if n == 3:
            before.append("For every attribute removed or replaced: list the derived metrics that filter on "
                          "it (they are deleted with it) and plan to recreate them.")
        republish = n in REPUBLISH_PHASES or any(f.get("republish") for f in items) or bool(merged_steps.get(n))
        own = [a for f in items for a in f.get("actions") or [{"kind": "manual"}]]
        if not merged_steps.get(n) and own and all(a.get("kind") in MERGED_KINDS and MERGED_KINDS[a["kind"]] != n
                                                   for a in own) and not any(f.get("republish") for f in items):
            republish = False   # its only work moved into another phase
        after = []
        if republish:
            after.append({"what": "Republish the model (tables, attributes or fact metrics changed).",
                          "command": cmd([python, BUILD_SCRIPT, "publish", "--model-id", mid] + PUBLISH_WAIT)})
        after.append({"what": "Re-certify the model (edits drop certification; MCP cannot see an uncertified model).",
                      "command": cmd([python, BUILD_SCRIPT, "certify", "--object-id", mid])})
        tests = sorted({t for f in items for t in f.get("tests") or []})
        for kind, target in MERGED_KINDS.items():
            if n == target and merged[kind]:
                tests = sorted(set(tests) | {t for f in ordered if f["id"] in merged_findings[kind]
                                             for t in f.get("tests") or []})
        if not tests:
            warnings.append(f"phase {n}: no unit tests named in its findings")
        names = [f["id"] for f in items]
        for kind, target in MERGED_KINDS.items():
            if n == target and merged[kind]:
                first = list(dict.fromkeys(c["finding"] for c in merged[kind]))
                names = first + [x for x in names if x not in first]
        phases.append({"phase": n, "name": PHASES[n], "findings": names,
                       "before": before, "steps": steps, "unit_tests": tests,
                       "regression": [cmd([a.replace("{n}", str(n)) for a in run_cmd]),
                                      cmd([a.replace("{n}", str(n)) for a in grade_cmd])],
                       "after": after})
    planned_ids = {f.get("id") for f in chosen}
    skipped = [{"id": f.get("id"), "status": f.get("status")} for f in every if f.get("id") not in planned_ids]
    # Every phase enforces answers: a finding nobody decided (proposed) blocks the phases of its actions, or its
    # own phase when it has none, whether the assessment, verify-changes or a check found it.
    unanswered = unanswered_by_phase(every)
    for ph in phases:
        ph["manual_steps"] = sum(1 for st in ph.get("steps") or [] if st.get("manual"))
        if ph["manual_steps"]:
            warnings.append(f"phase {ph['phase']}: {ph['manual_steps']} manual step(s)")
        if unanswered.get(ph["phase"]):
            ph["blocked_by"] = unanswered[ph["phase"]]
            warnings.append(f"phase {ph['phase']}: no script until these findings are answered: "
                            f"{', '.join(unanswered[ph['phase']])}")
    digest = hashlib.sha256(json.dumps({"findings": [{k: f.get(k) for k in ("id", "status", "actions", "decision_note",
                                                                         "applied_phases")} for f in every],
                                        "model_id": model_id, "statuses": list(statuses)},
                                       sort_keys=True, default=str).encode()).hexdigest()[:12]
    return {"version": 1, "model": findings_doc.get("model"), "model_id": model_id or None,
            "generated": time.strftime("%Y-%m-%d"), "statuses_planned": list(statuses), "plan_id": digest,
            "phases": phases, "files": files, "not_planned": skipped, "problems": problems,
            "warnings": warnings, "unanswered": {str(k): v for k, v in sorted(unanswered.items())}}


_CONSULT = re.compile(r"\b(consult|confirm with|check with|ask (the )?(data )?owner|data owner|agree with|decide|"
                      r"design (it|the|a|this)|review (it )?with|discuss)\b", re.I)


def consult_steps(f: dict) -> list[str]:
    """Manual steps that are a question for someone (a design, a data owner's review) rather than a model
    edit: they belong in a finding of their own that needs an answer, not inside one being approved."""
    out = []
    for a in f.get("actions") or []:
        if a.get("kind") != "manual":
            continue
        steps = a.get("steps") or (a.get("args") or {}).get("steps") or []
        for st in steps if isinstance(steps, list) else [steps]:
            if _CONSULT.search(str(st)):
                out.append(str(st)[:120])
    return out


def unanswered_by_phase(findings: list[dict]) -> dict[int, list[str]]:
    """Phase -> findings still awaiting a decision (status proposed) that bear on it: the phases of their
    actions, else the finding's own phase."""
    out: dict[int, list[str]] = {}
    for f in findings:
        if (f.get("status") or "proposed") != "proposed":
            continue
        phases = {action_phase(f, a) for a in f.get("actions") or []} - {None} or {_phase_of(f)} - {None}
        for n in phases:
            out.setdefault(int(n), []).append(str(f.get("id")))
    return out


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
        for where, text in (("sql", q.get("sql") or ""), ("best answer", (q.get("best_answer") or {}).get("sql") or "")):
            for lint in _sql_lint(text):
                warnings.append(f"{qid}: {where}: {lint}; fix with reason kind sql_error")
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
        ap = f.get("applied_phases")
        if ap is not None and (not isinstance(ap, list) or any(p not in PHASES for p in ap)):
            problems.append(f"{fid}: applied_phases must be a list of phases 1-7")
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
            if not f.get("checks"):
                warnings.append(f"{fid}: no tests named")
        for st in consult_steps(f):
            warnings.append(f"{fid}: a manual step is a question for someone, not a model edit ('{st}'): make it "
                            f"its own finding with needs_answer and no action, so this one holds only steps that run")
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

    sp = sub.add_parser("roles", help="find numeric columns read as the wrong kind of object (codes and counts as "
                                      "measures, entity characteristics summed across rows, measures held as attributes)")
    sp.add_argument("--project", required=True)
    sp.add_argument("--scope", required=True, help="scope.json from the scope command (with profiles)")
    sp.add_argument("--out", required=True)
    sp.add_argument("--entity", default="", help="entity key attribute (default: relationships.json's, else guessed)")
    sp.add_argument("--relationships", help="relationships.json, for its entity")
    sp.add_argument("--where", default="", help="SQL condition that scopes the checks on large models")
    sp.add_argument("--batch", type=int, default=8, help="measures per query (default 8)")
    sp.add_argument("--low-span", type=int, default=ROLE_LOW_SPAN,
                    help=f"integer range treated as a code, band or small count (default {ROLE_LOW_SPAN})")

    sp = sub.add_parser("probe", help="automatic checks on attributes and metrics: rows lost, values only in a "
                                      "lookup, parts that don't add up, one value in every group, latest-only")
    sp.add_argument("--project", required=True)
    sp.add_argument("--scope", required=True, help="scope.json with profiles and an anchor metric")
    sp.add_argument("--out", required=True)
    sp.add_argument("--attributes", help="comma-separated names (default: every attribute with at most --max-values)")
    sp.add_argument("--metrics", help="comma-separated names (default: every metric)")
    sp.add_argument("--changed-since", help="previous scope.json: probe only what changed since (verify-changes)")
    sp.add_argument("--findings", help="findings.json: also probe objects named by actions of --phases")
    sp.add_argument("--phases", default="", help="comma-separated phases applied this round")
    sp.add_argument("--period", default="", help="period attribute: metrics are also checked across periods")
    sp.add_argument("--split", default="", help="partner attribute grouped with each object (default: a 3-30 "
                                                "value attribute)")
    sp.add_argument("--filter", default="", help='"<attribute>=<value>" (default: a latest-period flag, else the '
                                                 'latest period)')
    sp.add_argument("--where", default="")
    sp.add_argument("--max-values", type=int, default=PROBE_MAX_VALUES)
    sp.add_argument("--batch", type=int, default=6)

    sp = sub.add_parser("question-sql", help="rewrite a question's governed SQL, keeping the earlier version")
    sp.add_argument("--bank", required=True)
    sp.add_argument("--id", required=True)
    g2 = sp.add_mutually_exclusive_group(required=True)
    g2.add_argument("--sql")
    g2.add_argument("--sql-file")
    g2.add_argument("--not-answerable", action="store_true")
    sp.add_argument("--reason-kind", required=True, choices=QUESTION_SQL_REASONS)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--path", default="", choices=("",) + PATHS)
    sp.add_argument("--round", type=int, default=None)

    sp = sub.add_parser("lookups", help="recommend lookup tables and write CSV templates for them")
    sp.add_argument("--probes", help="probes.json: attributes listing values with no fact rows point to existing tables")
    sp.add_argument("--model-export", help="the model's export JSON (build_mosaic.py export-model): its lookup tables, "
                                           "definitive; without it they are inferred")
    sp.add_argument("--calendar-lookups", action="store_true",
                    help="calendar parents from lookup CSVs instead of time attributes (when a date column can't be "
                         "converted)")
    sp.add_argument("--project", required=True)
    sp.add_argument("--scope", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--csv-dir", required=True, help="folder for the CSV templates")
    sp.add_argument("--relationships", help="relationships.json, to consider an entity table")
    sp.add_argument("--period", default="", help="period attribute (default: guessed)")
    sp.add_argument("--where", default="")
    sp.add_argument("--max-code-values", type=int, default=200,
                    help="treat text attributes with at most this many values as codes (default 200)")

    sp = sub.add_parser("check-lookup", help="check a filled lookup file is load-ready: the table's load format, its "
                        "header, the existing columns unchanged from the template; with --project, also against the "
                        "reloaded table")
    sp.add_argument("--lookups", required=True, help="lookups.json from the lookups command")
    sp.add_argument("--id", required=True, help="the suggestion's id (X-..., L-...)")
    sp.add_argument("--csv", help="the filled file (default: the template path in lookups.json)")
    sp.add_argument("--project", help="query the model: compare the reloaded table with the file (after a reload)")
    sp.add_argument("--model", help="model name (default: lookups.json)")
    sp.add_argument("--read-as", action="append", default=[], metavar="COLUMN=NAME",
                    help="how the connector answers a column the suggestion doesn't map (a new description form)")
    sp.add_argument("--record", action="store_true", help="save the result in lookups.json (the workbook shows it)")

    sp = sub.add_parser("grade", help="compare results with expected values; summarize by level and path")
    sp.add_argument("--bank", required=True)
    sp.add_argument("--results", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--tolerance", type=float, default=None,
                    help="relative tolerance when a question sets none (default 1e-6)")
    sp.add_argument("--previous", help="an earlier graded.json: report changed verdicts and regressions")

    sp = sub.add_parser("baseline", help="freeze reviewed answers of ungraded questions as baselines")
    sp.add_argument("--bank", required=True, help="question bank to update in place")
    sp.add_argument("--results", required=True, help="results.json holding the reviewed answers")
    sp.add_argument("--ids", required=True, help="comma-separated question ids, reviewed and judged right")
    sp.add_argument("--round", type=int, default=None)
    sp.add_argument("--note", default="", help="what the review checked")
    sp.add_argument("--force", action="store_true", help="also for questions that have an expected value")

    sp = sub.add_parser("best-answer", help="set a question's best answer, or override it with a recorded reason")
    sp.add_argument("--bank", required=True, help="question bank to update in place")
    sp.add_argument("--id", required=True)
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--sql", help="expert SQL over the model's own columns")
    g.add_argument("--sql-file", help="file holding the expert SQL")
    g.add_argument("--not-answerable", help="why no SQL over this model can answer it")
    sp.add_argument("--reason-kind", required=True, choices=BEST_ANSWER_REASONS)
    sp.add_argument("--reason", default="", help="for an override: the decision, the data change or the error")
    sp.add_argument("--rationale", default="", help="what the SQL does and why it is the right definition")
    sp.add_argument("--round", type=int, default=None)

    sp = sub.add_parser("references", help="names saved SQL uses that the model no longer has: compare this "
                        "round's scope with the previous one, rewrite SQL where the replacement is certain")
    sp.add_argument("--old-scope", required=True, help="the previous round's scope (scope_round<N-1>.json)")
    sp.add_argument("--scope", required=True)
    sp.add_argument("--bank", required=True)
    sp.add_argument("--model-export", help="this round's export: the key form a plain name became")
    sp.add_argument("--old-model-export", help="the previous round's export: objects renamed (same id)")
    sp.add_argument("--apply", action="store_true", help="rewrite the SQL (reason kind model_change) in --bank")
    sp.add_argument("--findings", help="findings.json: report which earlier name findings are resolved")
    sp.add_argument("--round", type=int)
    sp.add_argument("--out", required=True)

    sp = sub.add_parser("join-paths", help="does every attribute join each fact table (a cross join otherwise); "
                        "relationships lost since the previous round")
    sp.add_argument("--project", required=True)
    sp.add_argument("--scope", required=True)
    sp.add_argument("--model-export", help="the export: tables each attribute reaches, declared relationships")
    sp.add_argument("--previous", help="the previous round's join_paths.json, to compare relationships")
    sp.add_argument("--findings", help="findings.json: report which earlier join-paths findings are resolved")
    sp.add_argument("--where", default="")
    sp.add_argument("--round", type=int)
    sp.add_argument("--max-attributes", type=int, default=300)
    sp.add_argument("--out", required=True)

    sp = sub.add_parser("relationship-diff", help="relationships one model export has and another lacks (by "
                        "name); exits 1 when any was lost, with the actions that restore them")
    sp.add_argument("--before", required=True)
    sp.add_argument("--after", required=True)

    sp = sub.add_parser("add-findings", help="merge the findings references / join-paths proposed into "
                        "findings.json (existing ones keep their status)")
    sp.add_argument("--findings", required=True)
    sp.add_argument("--from", dest="sources", action="append", required=True, help="references or join-paths output")

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

    if args.cmd in LIVE_COMMANDS or (args.cmd == "check-lookup" and args.project):
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
    if args.cmd == "roles":
        entity = args.entity or ((_load_json(args.relationships) or {}).get("entity") if args.relationships else "")
        out = review_roles(runner(), args.project, _load_json(args.scope), entity=entity or "", where=args.where,
                           batch=args.batch, low_span=args.low_span, log=log)
        out["sources"] = source_info(scope=args.scope)
        _write_json(args.out, out)
        if _pending(runner):
            return 3
        print(json.dumps({"ok": True, "out": args.out, "entity": out["entity"], "measures_checked": out["measures_checked"],
                          "candidates": {r: sum(1 for c in out["candidates"] if c["role"] == r)
                                         for r in sorted({c["role"] for c in out["candidates"]})}}, indent=2))
        return 0
    if args.cmd == "probe":
        scope = _load_json(args.scope)
        attrs = [x.strip() for x in args.attributes.split(",") if x.strip()] if args.attributes else None
        mets = [x.strip() for x in args.metrics.split(",") if x.strip()] if args.metrics else None
        if args.changed_since or args.findings:
            phases = [int(p) for p in args.phases.split(",") if p.strip().isdigit()]
            changed = changed_objects(_load_json(args.changed_since) if args.changed_since else None, scope,
                                      _load_json(args.findings) if args.findings else None, phases or None)
            attrs = changed["attributes"] if attrs is None else attrs
            mets = changed["metrics"] if mets is None else mets
        out = probe_objects(runner(), args.project, scope, attributes=attrs, metrics=mets, period=args.period,
                            split=args.split, filter_spec=args.filter, where=args.where, max_values=args.max_values,
                            batch=args.batch, log=log)
        out["sources"] = source_info(scope=args.scope)
        _write_json(args.out, out)
        if _pending(runner):
            return 3
        print(json.dumps({"ok": True, "out": args.out, "attributes": len(out["attributes"]),
                          "metrics": len(out["metrics"]), "warnings": len(out["warnings"])}, indent=2))
        return 0
    if args.cmd == "check-lookup":
        return _check_lookup_cmd(args, runner)
    if args.cmd == "join-paths":
        scope = _load_json(args.scope)
        out = check_join_paths(runner(), args.project, scope,
                               export=_load_json(args.model_export) if args.model_export else None,
                               previous=_load_json(args.previous) if args.previous else None, where=args.where,
                               round_no=args.round, max_attributes=args.max_attributes, log=log)
        if args.findings:
            out["earlier_findings"] = resolved_findings(_load_json(args.findings), out["findings"])
        out["sources"] = source_info(scope=args.scope, export=args.model_export)
        _write_json(args.out, out)
        print(json.dumps({"out": args.out, "facts": out["facts"], "unjoined": out["unjoined"],
                          "filter_problems": sorted(out["filter_problems"]),
                          "metrics_filtering_flagged": out["metrics_filtering_flagged"],
                          "relationship_changes": out["relationship_changes"],
                          "findings": [f["id"] for f in out["findings"]],
                          **({"earlier_findings": out["earlier_findings"]} if args.findings else {})}, indent=2))
        return _pending(runner)
    if args.cmd == "lookups":
        scope = _load_json(args.scope)
        rel = _load_json(args.relationships) if args.relationships else None
        sugg = lookup_suggestions(scope, period=args.period, max_code_values=args.max_code_values, relationships=rel,
                                  time_attributes=not args.calendar_lookups,
                                  probes=_load_json(args.probes) if args.probes else None,
                                  export=_load_json(args.model_export) if args.model_export else None)
        export = _load_json(args.model_export) if args.model_export else None
        built = build_lookups(runner(), args.project, scope, sugg, args.csv_dir, where=args.where, export=export)
        _write_json(args.out, {"version": 1, "model": scope.get("model"), "lookups": built,
                               "sources": source_info(scope=args.scope, export=args.model_export)})
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


LIVE_COMMANDS = ("scope", "run", "relationships", "roles", "probe", "lookups", "join-paths")   # commands that query the model


def _pending(runner: Callable[[], Runner]) -> bool:
    """True when the runner made for this command is a replay runner with replies still missing."""
    made = getattr(runner, "made", [])
    return any(getattr(r, "pending", None) for r in made)


def _check_lookup_cmd(args: argparse.Namespace, runner: Callable[[], Runner] | None = None) -> int:
    data = _load_json(args.lookups)
    entry = next((x for x in data.get("lookups") or [] if x.get("id") == args.id), None)
    if not entry:
        print(json.dumps({"ok": False, "error": f"no suggestion '{args.id}' in {args.lookups}"}, indent=2))
        return 2
    path = args.csv or entry.get("csv")
    if not path:
        print(json.dumps({"ok": False, "error": f"'{args.id}' has no CSV file; pass --csv"}, indent=2))
        return 2
    extra = dict(x.split("=", 1) for x in args.read_as if "=" in x)
    if extra or entry.get("kind") == "code_lookup":
        entry = {**entry, "read_as": {**({entry["columns"][0]: entry["attribute"]} if entry.get("kind") == "code_lookup"
                                        else {}), **(entry.get("read_as") or {}), **extra}}
    live = None
    if runner is not None:
        cols = [c for c in (entry.get("template") or {}).get("columns") or entry.get("columns") or []
                if (entry.get("read_as") or {}).get(c)]
        live, err = _read_table_rows(runner(), args.project, args.model or data.get("model") or "",
                                     entry.get("read_as") or {}, cols)
        if live is None:
            print(json.dumps({"ok": False, "error": err}, indent=2))
            return 2
    res = check_lookup_csv(entry, path, live_rows=live)
    if args.record:
        for x in data.get("lookups") or []:
            if x.get("id") == args.id:
                x["check"] = {k: res[k] for k in ("ok", "csv", "problems", "warnings") if k in res}
                if live is not None:
                    x["check"]["against_model"] = True
        _write_json(args.lookups, data)
    print(json.dumps(res, indent=2))
    return 0 if res["ok"] else 1


def _offline(args: argparse.Namespace) -> int:
    if args.cmd == "check-lookup":
        return _check_lookup_cmd(args)
    if args.cmd == "references":
        old, new, bank = _load_json(args.old_scope), _load_json(args.scope), _load_json(args.bank)
        changes = reference_changes(old, new, export=_load_json(args.model_export) if args.model_export else None,
                                    old_export=_load_json(args.old_model_export) if args.old_model_export else None)
        usage = apply_reference_changes(bank, changes, round_no=args.round, apply=args.apply)
        if args.apply and usage["rewritten"]:
            _write_json(args.bank, bank)
        out = {"version": 1, "model": new.get("model"), "removed": changes, "usage": usage,
               "findings": reference_findings(changes, usage, args.round)}
        if getattr(args, "findings", None):
            done = resolved_findings(_load_json(args.findings), out["findings"])
            done = {k: [i for i in v if i.startswith("N-")] for k, v in done.items()}
            out["earlier_findings"] = done
        _write_json(args.out, out)
        print(json.dumps({"out": args.out, "names_gone": [c["name"] for c in changes],
                          "rewritten": len(usage["rewritten"]), "applied": args.apply,
                          "unresolved": usage["unresolved"], "findings": [f["id"] for f in out["findings"]]},
                         indent=2))
        return 0
    if args.cmd == "relationship-diff":
        res = relationship_diff(_load_json(args.before), _load_json(args.after))
        print(json.dumps(res, indent=2))
        if res["lost"]:
            print(f"STOP: {len(res['lost'])} relationship(s) lost: "
                  + "; ".join(f"{r['parent']} -> {r['child']}" for r in res["lost"])
                  + ". Run /verify-changes: it proposes restoring them. Don't continue this phase.", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "add-findings":
        doc = _load_json(args.findings)
        new = [f for src in args.sources for f in _load_json(src).get("findings") or []]
        res = merge_findings(doc, new)
        _write_json(args.findings, doc)
        print(json.dumps(res, indent=2))
        return 0
    if args.cmd == "grade":
        bank = _load_json(args.bank)
        out = grade_bank(bank, _load_json(args.results), bank_dir=os.path.dirname(os.path.abspath(args.bank)),
                         tolerance=args.tolerance, previous=_load_json(args.previous) if args.previous else None)
        _write_json(args.out, out)
        report = {"ok": True, "out": args.out, "by_verdict": out["summary"]["by_verdict"]}
        if "regressions" in out:
            report["regressions"] = out["regressions"]
        if out["baseline_changes"]:
            report["baseline_changes"] = out["baseline_changes"]   # review: an intended fix or a regression
        print(json.dumps(report, indent=2))
        return 0
    if args.cmd == "best-answer":
        bank = _load_json(args.bank)
        sql = args.sql or ""
        if args.sql_file:
            with open(args.sql_file, encoding="utf-8") as f:
                sql = f.read()
        out = set_best_answer(bank, args.id, sql=sql, not_answerable=args.not_answerable or "",
                              reason_kind=args.reason_kind, reason=args.reason, rationale=args.rationale,
                              round_no=args.round)
        if out["ok"]:
            _write_json(args.bank, bank)
        print(json.dumps(out, indent=2))
        return 0 if out["ok"] else 2
    if args.cmd == "question-sql":
        bank = _load_json(args.bank)
        sql = args.sql or ""
        if args.sql_file:
            with open(args.sql_file, encoding="utf-8") as f:
                sql = f.read()
        out = set_question_sql(bank, args.id, sql=sql, not_answerable=bool(args.not_answerable),
                               reason_kind=args.reason_kind, reason=args.reason, round_no=args.round, path=args.path)
        if out["ok"]:
            _write_json(args.bank, bank)
        print(json.dumps(out, indent=2))
        return 0 if out["ok"] else 2
    if args.cmd == "baseline":
        bank = _load_json(args.bank)
        out = freeze_baselines(bank, _load_json(args.results), [i.strip() for i in args.ids.split(",") if i.strip()],
                               round_no=args.round, note=args.note, force=args.force)
        if out["frozen"]:
            _write_json(args.bank, bank)
        print(json.dumps({"ok": not out["unknown"], **out}, indent=2))
        return 0 if not out["unknown"] else 2
    if args.cmd == "plan":
        statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
        bad = [s for s in statuses if s not in STATUSES]
        if bad:
            print(json.dumps({"ok": False, "problems": [f"unknown status {bad}"]}, indent=2))
            return 2
        out = build_plan(_load_json(args.findings), model_id=args.model_id, statuses=statuses,
                         python=args.python, quoting=args.quoting, project=args.project, bank=args.bank)
        _write_json(args.out, out)
        base = os.path.dirname(os.path.abspath(args.out))
        for rel, content in (out.get("files") or {}).items():   # spec files the commands read (relative paths)
            dest = os.path.normpath(os.path.join(base, rel))
            if not dest.startswith(base + os.sep):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _write_json(dest, content)
        print(json.dumps({"ok": not out["problems"], "out": args.out, "phases": [p["phase"] for p in out["phases"]],
                          "steps": sum(len(p["steps"]) for p in out["phases"]), "files": sorted(out.get("files") or {}),
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
