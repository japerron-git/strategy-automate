#!/usr/bin/env python3
"""Assess a Mosaic model with business questions: scope it, run a question bank through the
Mosaic MCP connector, grade the answers, and turn approved findings into an ordered change plan.

  assess_model.py scope --project P --model M --out scope.json        # inventory + bounded profiling
  assess_model.py run   --project P --bank question_bank.json --out results.json
  assess_model.py grade --bank question_bank.json --results results.json --out graded.json
  assess_model.py plan  --findings findings.json --model-id <id> --out change_plan.json
  assess_model.py check --bank question_bank.json --findings findings.json --domain domain_notes.json

scope and run sign in to the tenant's Mosaic MCP server the way the MCP connector does
(skills/strategy-platform/scripts/strategy_mcp.py: browser single sign-on, tokens in the OS secret
store). grade, plan and check work offline on the JSON files. File formats, levels and phases:
memory/reference_model_assessment.md. Workflow: skills/strategy-model-assessment/SKILL.md.

Output files hold tenant data (rows, names); they are written owner-only and belong outside the
repo. Nothing here writes to the model: plan only prints build_mosaic.py commands for review.
"""
from __future__ import annotations

import argparse
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
                "patch_model_object", "patch_fact_metrics", "publish", "certify", "manual")


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
    for ln in lines[start + 1:]:
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


# ── scope ────────────────────────────────────────────────────────────────────────────────────

def profile_sql(model: str, attribute: str, where: str = "") -> str:
    col = quote_ident(attribute)
    clause = f" WHERE {where}" if where else ""
    return (f"SELECT COUNT(DISTINCT {col}) AS distinct_values, MIN({col}) AS min_value, "  # nosec B608  # identifiers are quoted; the connector is read-only
            f"MAX({col}) AS max_value FROM {quote_model(model)}{clause}")


def sample_sql(model: str, attribute: str, limit: int, where: str = "") -> str:
    col = quote_ident(attribute)
    clause = f" WHERE {where}" if where else ""
    return f"SELECT DISTINCT {col} AS value FROM {quote_model(model)}{clause} ORDER BY 1 LIMIT {int(limit)}"  # nosec B608  # identifiers are quoted; the connector is read-only


def scope_model(runner: Runner, project: str, model: str, *, profile: bool = True, where: str = "",
                sample_max: int = 30, max_attributes: int = 200,
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
        return out
    for i, attr in enumerate(semantics["attributes"][:max_attributes]):
        log(f"profile {i + 1}/{min(len(semantics['attributes']), max_attributes)}: {attr['name']}")
        parsed = parse_query_result(runner.call("query", {"project": project,
                                                          "query": profile_sql(model, attr["name"], where)}))
        if not parsed["ok"] or not parsed["rows"]:
            attr["profile"] = {"error": parsed["error"] or "no rows"}
            continue
        row = parsed["rows"][0]
        prof: dict[str, Any] = {"distinct_values": svm.number(row.get("distinct_values")),
                                "min": row.get("min_value"), "max": row.get("max_value")}
        distinct = prof["distinct_values"]
        if distinct is not None and 0 < distinct <= sample_max:
            sample = parse_query_result(runner.call("query", {"project": project,
                                                              "query": sample_sql(model, attr["name"], sample_max,
                                                                                  where)}))
            prof["values"] = [r.get("value") for r in sample["rows"]] if sample["ok"] else None
        attr["profile"] = prof
    skipped = len(semantics["attributes"]) - max_attributes
    if skipped > 0:
        out["notes"]["unprofiled_attributes"] = skipped
    return out


# ── run ──────────────────────────────────────────────────────────────────────────────────────

def _select(questions: list[dict], only: str = "", levels: str = "") -> list[dict]:
    ids = {s.strip() for s in only.split(",") if s.strip()}
    lv = {int(s) for s in levels.split(",") if s.strip()}
    return [q for q in questions if (not ids or q.get("id") in ids) and (not lv or q.get("level") in lv)]


def _execute(runner: Runner, project: str, sql: str, max_rows: int) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        parsed = parse_query_result(runner.call("query", {"project": project, "query": sql}))
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

    run_cmd = [python, ASSESS_SCRIPT, "run", "--project", project or "<PROJECT>", "--bank", bank,
               "--out", "results_after_phase_{n}.json"]
    grade_cmd = [python, ASSESS_SCRIPT, "grade", "--bank", bank, "--results", "results_after_phase_{n}.json",
                 "--previous", "graded_before.json", "--out", "graded_after_phase_{n}.json"]
    phases = []
    for n in sorted({_phase_of(f) for f in ordered}):
        items = [f for f in ordered if _phase_of(f) == n]
        steps = []
        for f in items:
            for action in f.get("actions") or [{"kind": "manual", "steps": f.get("recommendation")}]:
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
        after = []
        if republish:
            after.append({"what": "Republish the model (tables, attributes or fact metrics changed).",
                          "command": cmd([python, BUILD_SCRIPT, "publish", "--model-id", mid])})
        after.append({"what": "Re-certify the model (edits drop certification; MCP cannot see an uncertified model).",
                      "command": cmd([python, BUILD_SCRIPT, "certify", "--object-id", mid])})
        tests = sorted({t for f in items for t in f.get("tests") or []})
        if not tests:
            warnings.append(f"phase {n}: no unit tests named in its findings")
        phases.append({"phase": n, "name": PHASES[n], "findings": [f["id"] for f in items],
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

    sp = sub.add_parser("run", help="run a question bank through the MCP query tool")
    sp.add_argument("--project", required=True)
    sp.add_argument("--bank", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--only", default="", help="comma-separated question ids")
    sp.add_argument("--levels", default="", help="comma-separated levels, e.g. 1,2")
    sp.add_argument("--max-rows", type=int, default=1000, help="rows kept per question (default 1000)")

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

    def runner() -> Runner:
        if runner_factory is not None:
            return runner_factory(args)
        return McpRunner(args.base, args.mcp_url, args.server)

    if args.cmd in ("scope", "run"):
        try:
            return _live(args, runner, log)
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
                          sample_max=args.sample_max, max_attributes=args.max_attributes, log=log)
        _write_json(args.out, out)
        print(json.dumps({"ok": True, "out": args.out, "attributes": len(out["attributes"]),
                          "metrics": len(out["metrics"]), **out["notes"]}, indent=2))
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
        print(json.dumps({"ok": True, "out": args.out, **out["summary"]}, indent=2))
        return 0
    return 2


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
