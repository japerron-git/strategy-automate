---
name: Mosaic fact-metric aggregation + format — discovery, PATCH, one-changeset bulk fix
description: Where base fact metrics live, the PATCH shape for aggregation function and number format, the semi-additive (first/last) rule, and the one-changeset bulk protocol implemented by `build_mosaic.py patch-fact-metrics`. Synthesized 2026-10-05 from a May 2026 branch that never merged, reconciled with the verified September format enum and the identity-token-off rule.
type: reference
tags: [mosaic, fact-metric, aggregation, format, changeset]
---

## Where fact metrics live

- `GET /api/model/dataModels/{mid}/factMetrics` — every base fact metric on the model (`information.{objectId,name}`). This is the discovery call.
- `GET /api/model/dataModels/{mid}/metrics` — **derived / level / compound** metrics only. A pure-fact model returns 0–2 here.
- `GET /api/model/dataModels/{mid}/factMetrics/{fmid}` — full payload with `function`, `format`, `dimty`. Read it before a PATCH to build the diff.
- Avoid `GET /api/searches/results?rootFolder=<schema folder>&type=4`: the schema folder is shared by every model in the project, so the result mixes in unrelated metrics.

## PATCH

```
PATCH /api/model/dataModels/{mid}/factMetrics/{fmid}
Headers: X-MSTR-AuthToken, X-MSTR-ProjectID, X-MSTR-MS-Changeset
Body (partial; the server keeps every field you omit):
{"function": "avg", "format": {"header": [], "values": [<typed tokens>]}}
```

- The field is literally `function`. `aggregation` or `defaultAggregation` returns 200 and changes nothing.
- Format tokens and the `number_category` enum: use the verified table in `feedback_mosaic_ship_bar.md` ("Metric formats"): 0=Fixed, 1=Currency, 2=Date, 3=Time, 4=Percentage, 5=Fraction, 6=Scientific, 7=Custom, 8=Special, 9=General. The May 2026 version of this note listed 2=Currency, 3=Percent and 4=Scientific. That table is wrong and renders currency as dates.
- A column stored as 0–100 must not use category 4, which multiplies by 100 (94.44 → "9444%"). Use Fixed with a literal suffix: `#,##0.0"%"` (preset `percent_0_100`).
- `metricFormatType: "reserved"` was sent in May 2026 runs because a format PATCH seemed to no-op without it. The September 2026 runs patched formats without it and the change rendered. Add it only if a format PATCH reads back unchanged.
- Function and format edits apply at query time. No republish is needed (`reference_mosaic_publish_path.md`).

## Picking `function`

| `function` | Use for |
|---|---|
| `sum` | Additive measures: revenue, units, counts. The build default. |
| `avg` | Per-unit rates stored at row grain: unit price, lead time, utilization %. |
| `min` / `max` | Worst/best-case sizing. |
| `first` / `last` | Semi-additive snapshots across time: inventory on hand, balances, backlog. |
| `median`, `stdev`, `var`, … | Statistical; rare in operational models. |

**Semi-additive trap.** `last` is correct across time but wrong at an all-products rollup: it picks one cell's value instead of summing. If consumers slice freely, keep the base at `sum` and add a level-metric sibling `Sum(<measure>) {~, Last(<snapshot date>)}` (`reference_mosaic_derived_metrics.md`). Use base `last` only when every report slices by the natural grain.

## Semi-additive rules on periodic-snapshot facts (verified 2026-10-30/31)

The `function` above is one control; the other is a `dimty` attribute unit on the period attribute with
`aggregation: first_in_fact | last_in_fact | first_in_relationship | last_in_relationship` (the only values a
fact-metric attribute unit accepts — `reference_strategy_error_codes.md`, `8004d716`). On a snapshot fact
(one row per entity per period, rows repeated after the entity exits):

- **Republish needed.** A dimty change on a fact metric reached the data only after a republish; function
  and format changes did not need one.
- **"First"/"last" resolve per report group, among the rows that qualify** — not per entity, and not over
  the whole calendar. Taking an entity-level constant (an original amount) with `first_in_fact` is right by
  pool or by period, but wrong for any grouping that mixes pools starting in different periods: only the
  entities present in the group's earliest period are counted (one production model understated a segment's
  original balance by about half). Use the once-per-entity derived metric
  `Sum(Max([M]){~+,[Entity]+}){~+}` instead (`reference_mosaic_derived_metrics.md` §0d rule 1), and keep the
  fact metric as a renamed `(raw)` input with a description that says where it is valid.
- **`last_in_fact` works for amounts that persist** after the event (exit balances, loss components) because
  every entity still has a row in the last period. It is not a "latest period overall" rule; on in-memory models
  `last_in_relationship` behaved the same even with a separate period lookup table (§0d rule 9).
- **Embedded filters drop these rules** when the fact metric is used inside a conditional metric — put the
  unit on the conditional metric (§0d rule 4).

## One-changeset bulk protocol

Patching one metric per changeset costs a commit round-trip per metric and opens an interactive session per changeset. That exhausts the iServer session cap (`8004cb0a`) within minutes (`feedback_build_mosaic_session_leak.md`). Instead, use one login, one list, K reads, one changeset with all PATCHes, one commit, then a read-back.

`build_mosaic.py patch-fact-metrics --model-id M --spec spec.json [--dry-run]` implements this:

```json
{"presets": {"hours_1dp": [{"type":"number_category","value":"0"},
                           {"type":"number_decimal_places","value":"1"},
                           {"type":"number_format","value":"#,##0.0\" hrs\""}]},
 "metrics": [{"name": "Unit Price",  "function": "avg",  "format": "currency"},
             {"name": "On Hand Qty", "function": "last", "format": "integer"},
             {"name": "Lead Time",   "format": "hours_1dp"}]}
```

Built-in presets are `currency`, `percent` (0–1 column), `percent_0_100`, `integer`, `fixed2` and `scientific`. A preset can also be a raw token list. The command PATCHes only metrics whose function or format differs, discards the changeset on any failure, and exits non-zero if the read-back disagrees. Keep model-specific specs under `captures/`.

`build` now also sets a format when it creates each fact metric:
- Names containing revenue, cost, price, amount and similar words get currency.
- Integer `sum`/`count`/`min`/`max` get `#,##0`.
- Everything else gets two-decimal Fixed.
- The dictionary `metrics.<TABLE>.<COL>.format` key overrides the default with a preset name or token list.

Percent is never guessed, because the name can't tell a 0–1 column from a 0–100 one. If a create call rejects the format, `build` retries without it rather than losing the metric.

## Identity token

Default: **off**. Use the standard auth token plus `X-MSTR-ProjectID` (`feedback_mosaic_identity_token_privilege_downgrade.md`); `patch-fact-metrics` logs in this way. If a tenant does need the token for writes:

- Mint it **after** `X-MSTR-ProjectID` is set. A token minted before then is project-less, and writes 403 with `8004cb09`, blaming a privilege the user actually holds.
- Attach it only for the changeset and pop it after commit/discard. Held session-wide, it makes Modeling GETs fail with `8004c768 "Wrong projectId"`.

Two different causes produce `8004cb09`: a project-less token, or project-only privilege grants combined with any identity token. Dropping the token fixes both.

## Failure modes

- `8004cb0a` mid-run: too many changesets opened in a burst. Use one changeset per spec.
- PATCH 200 but `function` unchanged: wrong key (see above).
- `semanticRole` changes after a format PATCH: Mosaic re-derives it from the format category. This is cosmetic.
- `last` gives the wrong total at the All-X level: see the semi-additive trap.
