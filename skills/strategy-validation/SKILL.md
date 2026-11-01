---
name: strategy-validation
description: Validate a Strategy Mosaic (or any Strategy) model against a trusted reference — another Mosaic model, a classic/legacy semantic-layer query, a flat file (CSV/Parquet/JSON), a direct warehouse SQL query, or a REST report output. Run a suite of paired queries, compare aggregates row-by-row with numeric tolerance, report matches/mismatches/missing rows. Also owns Strategy Test Center baseline and comparison tests. Use it for "validate the model", "do the numbers match", "compare with the old report", "Test Center", "baseline test", "regression test" or "compare before and after a migration".
---

# Strategy Data Validation

Use this skill whenever a model (especially a freshly built or modified Mosaic model) needs to be proven data-correct before it's marked shippable. **Every build should close with validation or an explicit validation-pending note.** The reference source can be whatever trusted artifact is closest to ground truth — this skill is not Mosaic-only.

Validation is always comparative. There is no universal "model is correct" check without first selecting what it should match: an existing Mosaic model, a classic/legacy semantic-layer report or model, a raw warehouse query, a flat file, an external system/API, or a saved REST fixture. If no trusted comparator is available yet, say that clearly and mark the build **not shippable pending validation**.

## Scope

Owns the paired-query validator (`skills/build-mosaic-model/scripts/strategy_validate_models.py`) and the Test Center API areas — Baseline Test, Comparison Test, Test Center Settings, Storage Sync File, and the internal Test Center file store and baseline-result files (`python3 skills/strategy-platform/scripts/strategy_api.py ops --tag strategy-validation --internal`). Structural checks are `build_mosaic.py validate-model`; fixing a model is `build-mosaic-model`. Question banks written by `skills/strategy-model-assessment/SKILL.md` double as regression suites: until `strategy_validate_models.py --query-suite` reads them, re-run one with `assess_model.py run` + `grade --previous <earlier graded.json>`.

## When to invoke

- Right after a new Mosaic build completes (before reporting "done").
- After a schema change (added/removed tables, attribute re-mapping, metric override, relationship edit).
- Before a production publish or refresh.
- During a legacy → Mosaic migration, to prove the Mosaic model reproduces classic report answers.
- On demand when a user doubts a number they saw in a dashboard.

## Reference sources (pluggable)

Pick the reference based on what's available and trusted. Record the selected reference type and reference object/source in the validation report.

1. **Another Mosaic model** — clone-reference pattern. Query through the Mosaic MCP `query` tool with `project="<project name from get_projects>"` (the model must be certified to be visible), or direct Trino with schema = project name lowercased. Best when validating a clone-and-remap or an alternate live/in-memory variant of the same source.
2. **Classic/legacy semantic-layer report** — run the classic report via `/api/reports/{id}/instances` + JSON Data API, or query the classic project attributes/metrics through the Modeling Service. Best during legacy-to-Mosaic migrations; see `memory/reference_strategy_legacy_to_mosaic_mining.md`.
3. **Flat file** (CSV / Parquet / JSON) — a snapshot export, hand-curated gold set, or auditor-supplied file. Load locally with `csv` / `json` / DuckDB / Pandas. Best when the warehouse is read-once (compliance exports, audit reconciliation).
4. **Direct warehouse SQL** — bypass the semantic layer entirely and query Snowflake / BigQuery / Oracle / etc. with raw SQL. Best for "does the semantic-layer math match the raw warehouse?" checks.
5. **External system / REST output** — saved output from a prior run (canary fixture), an externally-hosted reference API, a source application, or a previous inventory snapshot. Best for regression: "does today's build still match yesterday's numbers?"

Every source adapter reduces to `run_query(q) -> list[dict]`. The skill diffs structured rows regardless of source.

## Minimum validation suite (5 paired queries)

A shippable validation covers these 5 shapes at minimum. Expand to 10+ for critical / production-bound models.

1. **Totals + cardinality** — `SELECT SUM(<primary measure>), COUNT(DISTINCT <key attribute>) FROM <model>`; on big models scope it with a `WHERE` — a whole-model `COUNT(*)` can hit the job time limit (`memory/reference_mcp_tools.md`). Catches empty models, wrong fact table, fact-table double-joins.
2. **One-dim breakdown** — group by a high-cardinality descriptor (market segment, brand, ship mode). Catches attribute→fact join breaks.
3. **Two-dim rollup across the relationship chain** — e.g., Region × Nation revenue with hierarchical joins. Catches missing or mis-directed relationships (this is how the TPC-H Line-Number-overwritten bug surfaces).
4. **Filtered subset** — group by a descriptor inside a narrow filter (e.g., WHERE region = 'EUROPE'). Catches filter semantics + security-filter bleed-through.
5. **Time-based split** — YEAR() × a descriptor. Catches date-type coercions, timezone shifts, and the common "quarter truncation" bug.

Pick random / domain-appropriate variants; do not always run the same 5 — rotate queries so regressions in untested corners of the model surface.

## Comparison semantics

- **Numeric tolerance:** default `|a - b| / max(|a|, |b|, 1) <= 1e-6`. Decimal/currency columns should match to cents; rates to 4 decimal places. Raise tolerance only when a known reason exists (e.g., reference file was truncated to 2dp).
- **Row-set matching:** join both result sets on the dimension tuple (group-by columns). Report: `matched`, `reference_only` (rows in reference but missing from model), `model_only` (rows in model not in reference), `deltas` (rows present both sides but with differing metric values).
- **Ordering insensitivity:** always sort both sides by the dim tuple before compare; don't rely on the query's `ORDER BY` matching.
- **Null handling:** `NULL == NULL` for compare purposes (do not let Snowflake-style `NULL != NULL` silently fail rows); treat missing dimension values as `<NULL>` sentinel in the key tuple.

## Result format

The validator returns a structured result for each paired query:

```json
{
  "query_name": "region_nation_revenue",
  "status": "ok" | "mismatch" | "error",
  "row_count_model": 25,
  "row_count_reference": 25,
  "matched_rows": 25,
  "reference_only_rows": [],
  "model_only_rows": [],
  "delta_rows": [],
  "worst_delta_pct": 0.0,
  "metric_columns": ["revenue", "units"],
  "elapsed_model_ms": 812,
  "elapsed_reference_ms": 745
}
```

Final report: `N/N passed` plus a table of any failures with the smallest reproduction query on the left and the delta on the right.

## Helper script

`skills/build-mosaic-model/scripts/strategy_validate_models.py` is the pluggable runner.

### File adapter (works for any comparator — dump rows first, then diff)

```bash
python3 skills/build-mosaic-model/scripts/strategy_validate_models.py \
  --model-file /tmp/model_rows.csv \
  --reference-file /tmp/reference_rows.csv \
  --key region,nation \
  --measures revenue,orders \
  --out /tmp/validation.json
```

### Live Mosaic-to-Mosaic adapter (Trino)

Implemented. Runs the same SQL against two Mosaic models through the Strategy Trino endpoint (host derived from `MSTR_BASE`, schema from `MSTR_PROJECT_NAME`, basic auth with `MSTR_USER` / `MSTR_PASSWORD`). Single-sign-on accounts without a Strategy password run the same SQL through the Mosaic MCP server (`python3 skills/strategy-platform/scripts/strategy_mcp.py query --project "<project>" --sql '<SQL>'`), save both row sets and use the file adapter. Model name becomes the table name (lowercased, double-quoted). Use `%s` or `{{MODEL}}` as the model placeholder in the SQL:

```bash
python3 skills/build-mosaic-model/scripts/strategy_validate_models.py \
  --model "retail_model_v2" \
  --reference-mosaic "retail_model_v1" \
  --query 'SELECT "region (region name)", SUM("revenue") FROM %s GROUP BY 1' \
  --key 'region (region name)' \
  --measures revenue \
  --out /tmp/validation.json
```

Trino column naming: attributes are `"<attribute name lowercase> (<form name lowercase>)"`; metrics are `"<metric name lowercase>"`. See `memory/reference_strategy_data_validation.md` for the conventions.

### Still-pending adapters

Warehouse-SQL, classic-report, and REST-fixture adapters are not yet wrapped. The honest error message from the script points at the file-adapter workaround:

```bash
# Not yet implemented — the script will tell you to dump both sides to files and use --model-file + --reference-file.
--reference-sql-file / --reference-conn
--reference-classic-report
--reference-rest-file
--query-suite
```

Until those ship, the equivalent ad-hoc flow is: choose the comparator, run each paired query once against the model and once against the reference source, save both row sets, run the file adapter, and report the comparator used.

## Test Center: baseline and comparison tests

Test Center captures a baseline of reports, documents, dashboards and cubes (data, SQL, PDF) and compares two baselines — the before/after gate for migrations and model changes. Mechanics as in every domain skill: `describe` before each call, dry run, then `--yes` (`skills/strategy-platform/SKILL.md`).

```bash
API="python3 skills/strategy-platform/scripts/strategy_api.py"
$API call getAllIntegrityTests
$API call createIntegrityTest --body @test.json --yes          # name, settings.analyzer (what to capture), testObjects
$API call createIntegrityTestBaseline -p integrityTestId=<T> --body '{}' --yes        # runs the test objects
$API call queryIntegrityBaselineStatus -p integrityTestId=<T> -p baselineId=<B>       # poll
$API call createIntegrityComparison --body @cmp.json --yes     # name, source / target {type, testId, baselineId}, settings.compareContent
$API call createIntegrityComparison_1 -p integrityComparisonId=<C> --body '{}' --yes  # run the comparison
$API call queryIntegrityComparisonStatus -p integrityComparisonId=<C> -p comparisonId=<R>
$API call getIntegrityComparisonSummary -p integrityComparisonId=<C> -p comparisonId=<R> -p includeDetails=true
```

- `settings.compareContent` takes `SQL`, `DATA` and `PDF`; take the other body fields from `describe createIntegrityTest` / `describe createIntegrityComparison`.
- Creating a baseline runs the test objects on the environment ("Trigger a Baseline Test execution" in the spec): schedule it when that load is acceptable.
- Delete the tests, baselines and comparisons you created (`deleteIntegrityTestById`, `deleteIntegrityTestBaseline`, `deleteIntegrityComparisonById`) after confirming the ids.

## Integration with build workflows

Every build-mosaic-model invocation should append a validation decision:

1. Build completes with `model_id` + canonical name.
2. Select a reference: (a) if the user supplied one, use it; (b) else if a sibling model/report/source-system extract is trusted and accessible, use it; (c) else mark validation as pending and ask for the comparator.
3. If a reference is available, run the 5-query minimum suite.
4. Report aligns with the build report: `model built; validation_status=passed; 5/5 queries match reference <type>:<name>`.
5. If any query fails: mark the build as **incomplete**, surface the delta rows prominently, and do not tell the user "done" until failures are explained or fixed.
6. If no comparator exists yet: mark `validation_status=not_run` / `reference_required=true`. That is honest progress, not a pass.

The Mosaic ship-bar checklist (`memory/feedback_mosaic_ship_bar.md`) item 8 requires validation — treat this skill as part of the ship bar.

## When to use this skill vs `build_mosaic.py validate-model`

These two validators check different things and should both run:

- **`build_mosaic.py validate-model`** is a **structural** check — every attribute has a non-empty form name, every metric has a format token, every fact table has ≥1 attribute/metric, no orphan attributes, no double-joined facts. Enforces `feedback_mosaic_build_quality.md`. Run IMMEDIATELY after build, before publish.
- **This skill (`strategy-validation`)** is a **numeric-correctness** check — does the model's output match a trusted comparator's output within tolerance? Run AFTER structural validation passes, before declaring shippable.

A structurally valid model can still be numerically wrong (broken conformance, wrong aggregation function, missed SCD transition). A numerically correct model can still be structurally ugly (blank form names → UI unusable). Both gates are required.

## Non-goals

- This is not a performance benchmark; latency/throughput is out of scope.
- This is not a schema validator. For "does the shape look right" (attribute count, form categories, relationship graph) use `build_mosaic.py validate-model` or `skills/build-mosaic-model/scripts/strategy_mosaic_inventory.py`.
- This is not a SQL linter or a metric-lineage tool — it checks numeric correctness only.
- Security-filter effectiveness is a separate validation (confirm the right rows are *hidden*, not just that aggregates match); route that through `memory/reference_strategy_validation_workflows.md`.

## Related

- `memory/reference_strategy_data_validation.md` — design-time 10-check suite + runnable 5-query suite, reference-source decision matrix, tolerance rules, failure triage mapped to Kimball root causes.
- `memory/reference_mosaic_build_validation.md` — the structural checklist invoked by `build_mosaic.py validate-model`.
- `memory/feedback_mosaic_ship_bar.md` checklist item 8 — validation is a ship-bar requirement.

## Status

- **Implemented, hermetic tests:** the file adapter and the Mosaic-to-Mosaic Trino adapter of `strategy_validate_models.py` (`tests/test_strategy_validate_models.py`).
- **Spec-verified only:** the Test Center workflow — its operation IDs, parameters and required body fields pass `strategy_api.py`'s offline validation; no recorded live run. Record the first one in `memory/reference_strategy_validation_workflows.md`.
