#!/usr/bin/env python3
"""Compare Strategy model results against a trusted reference result set.

This is the first pluggable data-correctness runner for the strategy-validation
skill. It intentionally starts with file/result-set adapters so Mosaic, classic
reports, warehouse SQL, and external systems can all feed the same diff engine.
Live source adapters should reduce to the same row-list shape.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import getpass
import urllib.parse
import json
import math
import os
import re
import sys
import time
from typing import Any

try:
    import requests  # type: ignore
except ImportError:
    requests = None  # type: ignore

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_PLATFORM = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, "strategy-platform", "scripts"))
sys.path.insert(0, _PLATFORM)   # shared core: sign-in, API client, MCP
from _client import add_auth_args  # noqa: E402  (tolerates absent requests)


NULL_SENTINEL = "<NULL>"


# ── Live Trino adapter (for Mosaic model reads) ──────────────────────────────
#
# Strategy ONE Cloud tenants expose a Trino endpoint on the same host as the
# Library API (port 443, HTTPS, basic auth with the MSTR username/password).
# Each Mosaic model becomes a Trino table whose name is the model name
# lowercased and quoted. Column naming follows
# `"<attribute name lowercase> (<form name lowercase>)"` for attributes and
# `"<metric name lowercase>"` for metrics. See
# memory/reference_strategy_data_validation.md for the conventions.
#
# We implement the Trino query protocol directly against /v1/statement so this
# script stays `requests`-only (no trino-python-client dependency).

def _trino_host_from_base(base: str) -> str:
    """Derive the Trino host from a Library base URL. E.g.
    `https://foo.strategy.com/MicroStrategyLibrary` → `foo.strategy.com`."""
    from urllib.parse import urlparse
    parsed = urlparse(base)
    return parsed.netloc or parsed.path


def _trino_query(host: str, username: str, password: str,
                 schema: str, sql: str,
                 catalog: str = "sql", timeout: int = 120) -> list[dict[str, Any]]:
    """Run `sql` against a Strategy Trino endpoint. Returns rows as list[dict].

    Follows the Trino HTTP protocol: POST /v1/statement → poll nextUri until
    the response has no `nextUri` field. Each response page may carry
    `columns` (once) and `data` (0+ times). We assemble rows as dicts keyed
    by the column names from the first `columns` block we see.
    """
    if requests is None:
        raise SystemExit("requests is required for the live Trino adapter "
                         "(pip install requests).")
    url = f"https://{host}/v1/statement"
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "X-Trino-User": username,
        "X-Trino-Catalog": catalog,
        "X-Trino-Schema": schema,
        "Accept": "application/json",
    }
    http: Any
    try:
        import strategy_auth   # default timeouts + Trino-friendly retries of 502/503/504 page GETs
        http = strategy_auth.SafeSession()
    except ImportError:
        http = requests.Session()
    http.auth = (username, password)   # one connection for the POST and every page
    http.headers.update(headers)
    with http:
        return _trino_follow(http, url, sql, timeout)


def _trino_follow(http: Any, url: str, sql: str, timeout: int) -> list[dict[str, Any]]:
    """POST the statement and follow nextUri; a query we stop following (error, timeout,
    Ctrl-C, page cap) is cancelled so it doesn't keep running on the warehouse."""
    resp = http.post(url, data=sql.encode("utf-8"), timeout=timeout)
    if not resp.ok:
        raise SystemExit(f"Trino POST /v1/statement → {resp.status_code}: {resp.text[:400]}")
    cursor: dict[str, Any] = {"next": None}
    try:
        return _trino_pages(http, url, resp.json(), timeout, cursor)
    except BaseException:
        nxt = cursor["next"]
        if nxt and _same_trino_origin(nxt, url):   # the password rides along: never elsewhere
            with contextlib.suppress(Exception):
                http.delete(nxt, timeout=10)
        raise


def _same_trino_origin(nxt: str, url: str) -> bool:
    parts, home = urllib.parse.urlsplit(nxt), urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.hostname == home.hostname and parts.port == home.port


def _trino_pages(http: Any, url: str, payload: dict, timeout: int, cursor: dict) -> list[dict[str, Any]]:
    columns: list[str] | None = None
    rows: list[dict[str, Any]] = []
    safety = 1000  # hard cap on follow-next redirects; Trino pages are typically <50
    while True:
        if columns is None and isinstance(payload.get("columns"), list):
            columns = [str(c.get("name")) for c in payload["columns"]]
        data = payload.get("data")
        if isinstance(data, list) and columns:
            for row in data:
                rows.append({columns[i]: row[i] for i in range(min(len(columns), len(row)))})
        err = payload.get("error")
        if err:
            raise SystemExit(f"Trino query error: {err.get('message') or err}")
        nxt = cursor["next"] = payload.get("nextUri")
        if not nxt:
            return rows
        safety -= 1
        if safety <= 0:
            raise SystemExit("Trino query: exceeded nextUri follow cap (1000)")   # the caller cancels it
        # The password rides on every page request: follow nextUri only over https to the
        # host we posted to (a proxy may hand back http:// or another host).
        if not _same_trino_origin(nxt, url):
            parts = urllib.parse.urlsplit(nxt)
            raise SystemExit(f"Trino nextUri points at {parts.scheme}://{parts.netloc}; refusing to send "
                             "credentials there")
        fr = http.get(nxt, timeout=timeout)
        if not fr.ok:
            raise SystemExit(f"Trino GET {nxt} → {fr.status_code}: {fr.text[:300]}")
        payload = fr.json()


def _resolve_trino_creds(args: argparse.Namespace) -> tuple[str, str, str, str]:
    """Return (host, username, password, schema) from args + env, prompting for
    the password only if not found. host defaults to the Library host; schema
    defaults to the project name, lowercased."""
    base = args.base or os.environ.get("MSTR_BASE", "")
    host = args.trino_host or _trino_host_from_base(base)
    if not host:
        raise SystemExit("--trino-host (or --base / MSTR_BASE) is required to locate the Trino endpoint.")
    user = args.user or os.environ.get("MSTR_USER", "")
    if not user:
        raise SystemExit("--user or MSTR_USER is required for Trino basic auth (a Strategy password). "
                         "Single-sign-on accounts without one: run the same SQL through the Mosaic MCP "
                         "server instead — skills/strategy-platform/scripts/strategy_mcp.py query.")
    password = os.environ.get("MSTR_PASSWORD", "")
    if not password:
        password = getpass.getpass(f"MSTR password for {user}@{host}: ")
    schema = args.trino_schema
    if not schema:
        project = args.project_name or os.environ.get("MSTR_PROJECT_NAME", "")
        if not project:
            raise SystemExit("--trino-schema or --project-name / MSTR_PROJECT_NAME is required "
                             "(Trino schema is the project name lowercased).")
        schema = project.lower()
    return host, user, password, schema


def _quote_model_name(name: str) -> str:
    """Trino needs the model-as-table name double-quoted when it contains spaces
    or mixed case. Always quote so we don't trip on either."""
    return '"' + name.replace('"', '""') + '"'


def load_rows(path: str) -> list[dict[str, Any]]:
    if path.lower().endswith(".csv"):
        try:
            with open(path, encoding="utf-8-sig", newline="") as f:
                return [dict(row) for row in csv.DictReader(f)]
        except OSError as exc:
            raise SystemExit(f"{path}: {exc}") from exc
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except OSError as exc:
        raise SystemExit(f"{path}: {exc}") from exc
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "items", "result", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    raise SystemExit(f"{path}: expected CSV, JSON list, or JSON object with rows/data/items/results")


def split_cols(value: str) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


_GROUPED_NUMBER = re.compile(r"-?\d{1,3}(,\d{3})+(\.\d+)?")


def _key_text(value: Any) -> str:
    """A key as text. The Mosaic connector shows numbers with thousands separators ("2,015"), a reference
    file doesn't: a number written that way loses them, so the two match."""
    text = str(value)
    return text.replace(",", "") if _GROUPED_NUMBER.fullmatch(text.strip()) else text


def key_for(row: dict[str, Any], key_cols: list[str]) -> tuple:
    if not key_cols:
        return ()
    return tuple(NULL_SENTINEL if row.get(col) in (None, "") else _key_text(row.get(col)) for col in key_cols)


def number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def infer_measures(model_rows: list[dict[str, Any]], reference_rows: list[dict[str, Any]], key_cols: list[str]) -> list[str]:
    names = sorted((set().union(*(r.keys() for r in model_rows or [{}])) &
                    set().union(*(r.keys() for r in reference_rows or [{}]))) - set(key_cols))
    measures = []
    for name in names:
        values = [number(row.get(name)) for row in (model_rows[:20] + reference_rows[:20])]
        if any(value is not None for value in values):
            measures.append(name)
    return measures


def index_rows(rows: list[dict[str, Any]], key_cols: list[str], label: str) -> tuple[dict[tuple, dict], list[tuple]]:
    indexed: dict[tuple, dict] = {}
    duplicates: list[tuple] = []
    for idx, row in enumerate(rows):
        key = key_for(row, key_cols) if key_cols else (idx if len(rows) > 1 else 0,)
        if key in indexed:
            duplicates.append(key)
            continue
        indexed[key] = row
    if duplicates:
        sample = ", ".join(str(k) for k in duplicates[:5])
        raise SystemExit(f"{label}: duplicate keys found: {sample}")
    return indexed, duplicates


def compare_rows(model_rows: list[dict[str, Any]], reference_rows: list[dict[str, Any]],
                 key_cols: list[str], measure_cols: list[str], tolerance: float,
                 query_name: str, allow_empty: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    if not model_rows and not reference_rows and not allow_empty:
        # Nothing on either side proves nothing: a wrong filter, schema or model name empties
        # both queries just as well as a correct one. Never a pass unless asked for.
        return {"query_name": query_name, "status": "empty", "row_count_model": 0, "row_count_reference": 0,
                "note": "both sides returned no rows; check the query, or pass --allow-empty if expected",
                "elapsed_model_ms": None, "elapsed_reference_ms": None, "elapsed_compare_ms": 0}
    if not measure_cols:
        measure_cols = infer_measures(model_rows, reference_rows, key_cols)
    if not measure_cols:
        raise SystemExit("No measure columns supplied or inferred")

    model_idx, _ = index_rows(model_rows, key_cols, "model")
    ref_idx, _ = index_rows(reference_rows, key_cols, "reference")
    model_keys = set(model_idx)
    ref_keys = set(ref_idx)
    common = sorted(model_keys & ref_keys)

    deltas = []
    worst = 0.0
    for key in common:
        mrow = model_idx[key]
        rrow = ref_idx[key]
        for col in measure_cols:
            mv = number(mrow.get(col))
            rv = number(rrow.get(col))
            if mv is None and rv is None:
                continue
            if mv is None or rv is None:
                rel = 1.0
            else:
                rel = abs(mv - rv) / max(abs(mv), abs(rv), 1.0)
            worst = max(worst, rel)
            if rel > tolerance:
                deltas.append({
                    "key": list(key) if isinstance(key, tuple) else key,
                    "metric": col,
                    "model": mv,
                    "reference": rv,
                    "delta_pct": rel,
                })

    reference_only = sorted(ref_keys - model_keys)
    model_only = sorted(model_keys - ref_keys)
    status = "ok" if not (reference_only or model_only or deltas) else "mismatch"
    return {
        "query_name": query_name,
        "status": status,
        "row_count_model": len(model_rows),
        "row_count_reference": len(reference_rows),
        "matched_rows": len(common) - len({tuple(d["key"]) if isinstance(d["key"], list) else d["key"] for d in deltas}),
        "reference_only_rows": [list(k) if isinstance(k, tuple) else k for k in reference_only[:50]],
        "model_only_rows": [list(k) if isinstance(k, tuple) else k for k in model_only[:50]],
        "delta_rows": deltas[:100],
        "worst_delta_pct": worst,
        "metric_columns": measure_cols,
        "elapsed_model_ms": None,
        "elapsed_reference_ms": None,
        "elapsed_compare_ms": int((time.monotonic() - started) * 1000),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-file", "--model-result-file", dest="model_file",
                        help="CSV/JSON rows produced from the model under test (file adapter)")
    parser.add_argument("--reference-file", "--reference-result-file", dest="reference_file",
                        help="CSV/JSON rows from the trusted comparator (file adapter)")
    parser.add_argument("--key", default="", help="comma-separated key/dimension columns; omit only for a single-row total")
    parser.add_argument("--measures", default="", help="comma-separated numeric measure columns; inferred when omitted")
    parser.add_argument("--query-name", default="model_comparison")
    parser.add_argument("--tolerance", type=float, default=1e-6)
    parser.add_argument("--allow-empty", action="store_true",
                        help="treat 'both sides returned no rows' as a pass (default: exit 2, it proves nothing)")
    parser.add_argument("--out", help="write structured JSON result")

    # Live Mosaic adapter (Trino). Use these together: --model + --reference-mosaic + --query.
    parser.add_argument("--model",
                        help="Mosaic model name under test (live adapter via Trino)")
    parser.add_argument("--reference-mosaic",
                        help="Mosaic model name to compare against (live adapter via Trino)")
    parser.add_argument("--query",
                        help="SQL run against BOTH models. Use {{MODEL}} (or a bare %%s) as the "
                             "model-as-table placeholder. The placeholder gets replaced "
                             "with the correctly-quoted model name per side; LIKE '%%x%%' patterns are left alone.")
    parser.add_argument("--model-query",
                        help="Override SQL for the --model side only (otherwise --query is used).")
    parser.add_argument("--reference-query",
                        help="Override SQL for the --reference-mosaic side only.")
    parser.add_argument("--trino-host",
                        help="Trino host (defaults to the host component of --base / MSTR_BASE).")
    parser.add_argument("--trino-schema",
                        help="Trino schema (defaults to --project-name / MSTR_PROJECT_NAME lowercased).")
    add_auth_args(parser, password=False, login_mode=False, help_text={
        "base": "MSTR Library base URL (used to derive Trino host).",
        "user": "MSTR username (reused for Trino basic auth).",
        "project-name": "MSTR project name (lowercased → Trino schema).",
    })

    # Still-pending adapters.
    parser.add_argument("--reference-sql-file")
    parser.add_argument("--reference-conn")
    parser.add_argument("--reference-classic-report")
    parser.add_argument("--reference-rest-file")
    parser.add_argument("--query-suite")
    return parser.parse_args()


def inject_model(sql: str, model_name: str) -> str:
    """Put the quoted model name where the query says: {{MODEL}} (preferred), else a bare %s.
    A %s inside a LIKE pattern such as '%south%' is not a placeholder and is left alone."""
    quoted = _quote_model_name(model_name.lower())
    if "{{MODEL}}" in sql:
        return sql.replace("{{MODEL}}", quoted)
    # no placeholder at all: the caller embedded the model name already
    return re.sub(r"(?<![\w'%])%s(?![\w%'])", lambda _m: quoted, sql)


def _run_mosaic_adapter(args: argparse.Namespace) -> dict[str, Any]:
    """Live Mosaic-to-Mosaic comparison via Trino. Runs one SQL against each
    side, quoting the model name as the table reference, then passes the rows
    through the existing diff engine."""
    if not (args.model and args.reference_mosaic):
        raise SystemExit("--model and --reference-mosaic must both be set for the live Mosaic adapter.")
    model_sql = args.model_query or args.query
    ref_sql = args.reference_query or args.query
    if not model_sql or not ref_sql:
        raise SystemExit("Provide --query (shared) or both --model-query and --reference-query.")

    host, user, password, schema = _resolve_trino_creds(args)

    def _inject(sql: str, model_name: str) -> str:
        return inject_model(sql, model_name)

    t0 = time.monotonic()
    model_rows = _trino_query(host, user, password, schema,
                              _inject(model_sql, args.model))
    model_ms = int((time.monotonic() - t0) * 1000)

    t0 = time.monotonic()
    ref_rows = _trino_query(host, user, password, schema,
                            _inject(ref_sql, args.reference_mosaic))
    ref_ms = int((time.monotonic() - t0) * 1000)

    result = compare_rows(
        model_rows, ref_rows,
        split_cols(args.key),
        split_cols(args.measures),
        args.tolerance,
        args.query_name,
        allow_empty=args.allow_empty,
    )
    result["elapsed_model_ms"] = model_ms
    result["elapsed_reference_ms"] = ref_ms
    result["source"] = {
        "adapter": "mosaic_trino",
        "model": args.model,
        "reference": args.reference_mosaic,
        "trino_host": host,
        "trino_schema": schema,
    }
    return result


def main() -> int:
    args = parse_args()

    # Still-unimplemented adapters → honest error with workaround.
    unimplemented = [flag for flag, val in (
        ("--reference-sql-file", args.reference_sql_file),
        ("--reference-conn", args.reference_conn),
        ("--reference-classic-report", args.reference_classic_report),
        ("--reference-rest-file", args.reference_rest_file),
        ("--query-suite", args.query_suite),
    ) if val]
    if unimplemented:
        raise SystemExit(
            f"Adapter flag(s) {', '.join(unimplemented)} are not yet implemented. "
            f"Workaround: run the appropriate source adapter (classic report instance, "
            f"warehouse driver, REST fixture read) externally, dump rows to JSON/CSV, then "
            f"compare with --model-file + --reference-file. "
            f"The live Mosaic-to-Mosaic adapter IS implemented — use "
            f"--model + --reference-mosaic + --query. "
            f"See memory/reference_strategy_data_validation.md."
        )

    # Dispatch: live Mosaic adapter if model+reference-mosaic are set, else file adapter.
    if args.model or args.reference_mosaic:
        result = _run_mosaic_adapter(args)
    else:
        if not (args.model_file and args.reference_file):
            raise SystemExit(
                "Provide --model-file and --reference-file for the file adapter, "
                "OR --model + --reference-mosaic + --query for the live Mosaic adapter."
            )
        result = compare_rows(
            load_rows(args.model_file),
            load_rows(args.reference_file),
            split_cols(args.key),
            split_cols(args.measures),
            args.tolerance,
            args.query_name,
            allow_empty=args.allow_empty,
        )

    text = json.dumps(result, indent=2)
    if args.out:
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # rows can be business data
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return {"ok": 0, "mismatch": 1}.get(result["status"], 2)   # empty or anything else: 2


if __name__ == "__main__":
    raise SystemExit(main())
