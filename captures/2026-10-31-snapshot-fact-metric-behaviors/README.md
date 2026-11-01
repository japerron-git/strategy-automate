# 2026-10-30/31 — Metric behaviors on a periodic-snapshot fact (in-memory Mosaic model)

Question: on a fact table with one row per entity per period, where rows keep repeating after the entity
exits (frozen status and exit code), which metric shapes give the right answer at every grouping, and which
silently do not? Durable answers: `memory/reference_mosaic_derived_metrics.md` §0d,
`memory/reference_mosaic_fact_metric_aggregation.md` ("Semi-additive rules on periodic-snapshot facts"),
`memory/feedback_mosaic_gotchas.md` ("Model edits with silent side effects"), `memory/reference_mcp_tools.md`
("SQL shapes that change metric answers"). This file is the dated test log.

Stand-ins: **SnapshotCo** = an in-memory Mosaic model built from one uploaded file — a synthetic sample in a
public credit-risk-transfer loan-level layout: 62 entities in 5 pools, 2,000 entity-period rows, 38 monthly
periods, an exit code (prepaid / three credit-event codes), a monthly status code, balance and loss columns.
Mapping tables were added later (pool → group, exit code → exit type + event flag, status → bucket + 60+ flag,
period → activity month + latest-period flag). Tenant: a Strategy ONE Cloud tenant. All object IDs, hostnames
and user names redacted. Every expected value was recomputed independently from the source file (pandas) and
compared to MCP `query` results.

## Write paths used (all verified)

- Derived metrics: `POST /api/model/dataModels/{mid}/metrics?showAdvancedProperties=true` with tokens
  (level / nested / count shapes) or `expression.tree` + `dimty: null` (compounds), one changeset per metric.
- Conditional metrics: create → `POST …/metrics/{id}/embeddedObjects` → `PUT …/metrics/{id}` (bind), one
  changeset (`reference_mosaic_derived_metrics.md` §0c), with `removeElements: false`, 1–3 ANDed element lists.
- Compound update: `PUT …/metrics/{id}?showAdvancedProperties=true` with the full tree body + `formulaJoinType`.
- Derived metric delete: `DELETE …/metrics/{id}` in a changeset.
- Formula attribute: `POST …/attributes` (tokens, form `dataType`); type fix: `PATCH …/attributes/{id}` `{forms:[…]}`.
- Fact metric level rule: `PATCH …/factMetrics/{id}` `{dimty:{dimtyUnits:[report_base_level, {attribute: Period,
  aggregation: first_in_fact|last_in_fact}]}}` — needed a republish to reach the data.

## Results

| # | Shape | Expected (source) | Observed | Verdict |
|---|---|---|---|---|
| 1 | Fact metric, `first_in_fact` on Period, original amount, grouped by an entity characteristic | 13,485,000 for the largest cell | 2,382,000 | wrong: per-group earliest period only |
| 2 | `Sum(Max([M]){~+,[Entity]+}){~+}` same grouping, no grouping, by pool, by period | all cells match | all cells match | right |
| 3 | `Max([M]){~+,[Entity]+}` with Entity in dimty, by pool | pool totals | largest single entity per pool | not a total |
| 4 | Any metric in a query that also has `COUNT(DISTINCT CASE …)` | per-pool removal amount | 10–30× inflated, nested metric too | wrong |
| 5 | `Count<Distinct=True>([Entity])` ± embedded filter (exit codes, flag = Y) | counts per pool / characteristic / code / period | match | right |
| 6 | Conditional `Sum([M])` over a `last_in_fact` fact metric (exit amount, event codes) | 268,827.70 for one pool | 1,075,310.80 (4×; 8× elsewhere) | wrong: rule dropped |
| 7 | Rule 6 + filter on the nested form `Sum(Max([M]){Entity})` | event-only total 1,308,550.39 | 6,231,490.45 (all exits) | wrong: filter ignored |
| 8 | Nested over a conditional `Avg` | sum over entities | the conditional metric's own report-level value | wrong |
| 9 | Conditional `Sum([M])` with its own `last_in_fact` Period unit | 268,827.70 / 1,308,550.39 / per-characteristic | match, no republish | right |
| 10 | Tree compound `A − B`, default join, query without a plain metric | a pool with no events shows 0 | pool row missing | rows drop |
| 11 | Same with `formulaJoinType: "outer"` | row kept | row kept; blank operand = 0 | right |
| 12 | `Count([Entity])` (distinct or not) + `last_in_fact` Period unit, no period on query | 8 (latest period) | 15 (ever) / 165 (all rows) | rule ignored |
| 13 | `Count([fact metric])` + Period unit, no period | 8 | 8 | right |
| 14 | Status metric with `last_in_fact` or `last_in_relationship`, by pool, no period on query (also after the Period attribute got a separate lookup table, auto-detect off) | 0 for a pool with no current status rows | that pool's last status month value | per-group "latest" |
| 15 | Same, filtered `latest-period flag = Y` (flag on the period table) | 0 / match | match | right |
| 16 | AND filter, 2 and 3 element lists | counts | match | right |
| 17 | Weighted average from attributes (`Max(attr)` × `Max(weight)` per entity; `A/A` denominator) | 5 averages × 5 pools × 3 characteristics + totals | all match; entities with no value excluded | right |
| 18 | `Avg(attr)` filtered `event flag = Y AND latest flag = Y` | 16.4 average, per pool 18/14/18/–/16 | match | right |
| 19 | Formula attribute `Year([date])`, no dataType | 2017 | `1905-07-09` (stored as date) | fix with form dataType |
| 20 | Replace a code attribute with a lookup-backed one | — | 16 derived metrics that filtered on it (and dependents) deleted silently | back up + list dependents |

## Blind discoverability check

A fresh MCP client with no context (connector only) answered 22 analyst questions against the finished model:
22 correct; 19 with governed metrics or simple SQL; 3 needed expert SQL (cross-month entity comparisons and
a prior-period denominator, which no metric expresses). Before the changes, a 20-question run scored 7 simple,
10 expert-SQL, 3 wrong.
