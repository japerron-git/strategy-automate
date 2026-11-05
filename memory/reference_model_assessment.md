---
name: Model assessment — maturity, relationships, lookups, question levels, grading, defect checklist, change phases, file formats
description: Reference for skills/strategy-model-assessment — maturity levels, relationship review, lookup tables, replay mode, the 1-5 question levels, the grading rubric (correctness, answer path, discoverability), a defect checklist distilled from field assessments, the seven change phases, and the JSON formats for domain notes, question banks, results, findings and change plans.
type: reference
---

Companion to `skills/strategy-model-assessment/SKILL.md` and its script
`skills/strategy-model-assessment/scripts/assess_model.py`. Domain-neutral: industry knowledge comes
from sourced research per assessment, never from this file.

## Maturity levels

`assess_model.py scope` estimates the level from names, descriptions, formulas and profiles; confirm it.

| Level | Typical signals | Emphasis |
|---|---|---|
| M0 Raw build | default names (`Total X (Table)`), every numeric summed, IDs or codes as metrics, no descriptions, no derived metrics | grain, aggregation, naming, lookups, relationships; group repeated defects into one finding per pattern; mostly level 1-2 questions |
| M1 Cleaned | business names, formats and aggregations fixed, some descriptions | snapshot semantics, codes, counts |
| M2 Decoded | lookups or forms decode codes; a few derived metrics | governed core measures, conditional metrics |
| M3 Governed | derived and conditional metrics for the main measures | edge semantics (per-group "latest", dropped rows, filters dropping level rules), discoverability |
| M4 Analyst-ready | helpers labeled, latest-period flag, descriptions throughout, certified | level 4-5 questions, cross-period analysis, performance |

## Relationship review

`assess_model.py relationships` tests functional dependencies in the data: child → parent holds when
`COUNT(DISTINCT child) = COUNT(DISTINCT child || parent)` (NULL kept as a value).

| Result | Meaning | Typical recommendation |
|---|---|---|
| `many_to_one` | each child value has one parent | parent-child relationship / hierarchy if it makes business sense |
| `one_to_one` | the two attributes identify each other | make them forms of one attribute (code + description) or drop one |
| `entity_level` | fixed per entity | count or sum once per entity; entity-table candidate |
| `varies_within_entity` | changes across rows of one entity (periods) | belongs to the snapshot; needs period rules |
| `…_with_exceptions` | holds except ≤ 1% of values (50+ child values) | data-quality finding, or a relationship with dirty data |

Evidence is `weak` below 30 child values, `moderate` from 30 and `strong` from 100 — coincidental
dependencies are common in small samples. The entity tests run first; candidate pairs are then
tested only between attributes on the same side (both fixed per entity, or both varying), ranked
by name affinity and hierarchy families (geography, calendar, organization).
Relationships matter for drill paths, rollups across attributes, level metrics (the level attribute
must be an ancestor of the report rows, `reference_mosaic_derived_metrics.md` §0b) and lookup tables.

## Lookup tables

`assess_model.py lookups` recommends: **code lookups** (coded text attributes with no description
form or attribute), a **period lookup** (calendar parents + latest-period flag, when no flag exists)
and, optionally, an **entity table** (many attributes fixed per entity). It writes CSV templates with
the key values from the data; description columns are filled only from cited sources (user documents,
provider dictionary), with the source named per row, or left for the user. Add a lookup by mapping its
key column to the **existing** attribute — replacing the attribute deletes the derived metrics that
filter on it (`feedback_mosaic_gotchas.md`). Then check the attribute's lookup table, republish and
re-certify.

## Replay mode

Where the script cannot sign in (a cloud session with the Mosaic connector), pass `--replies <folder>`:
missing calls are listed in `<folder>/pending.json` (`tool`, `arguments`, `file`); the agent calls each
tool, saves the reply text to the file (`ERROR:` prefix for a failed call), and re-runs the command until
it exits 0 (3 = replies still needed). Query files are named by a hash of the SQL, so a reply can be
reused across runs. Save multi-block replies as one file (the connector can split a table across text
blocks; the parser joins them).

## Connector behaviors seen in assessments (verified 2026-11-01, Mosaic MCP `query`)

- Numbers come back formatted with thousands separators (`202,003`, `9,999`). Values that will be reused
  as keys (codes, YYYYMM periods) are read with `CAST(<col> AS VARCHAR)`, which returns plain digits.
- A NULL in a numeric column came back as `0` from `CAST(... AS VARCHAR)` in `SELECT DISTINCT`, while
  `MIN` and `COUNT(DISTINCT)` ignored it and `= 0` matched nothing — test with `IS NULL`. The lookup
  command drops such below-minimum values from numeric code CSVs and says so.
- `COUNT(DISTINCT …)` over no matching rows returned a blank cell, not 0.
- `UNION ALL` of `SELECT DISTINCT` branches works; the scripts batch value samples that way (and
  profiles as several `COUNT(DISTINCT)` / `MIN` / `MAX` columns per query), cutting connector calls about
  tenfold for agent-driven runs.
- Sentinels ("not available" written as a number, such as -1, 99 or a run of 9s) inside scores and ratios inflate averages; check the
  profile `max` against the documented range.

## Question levels

| Level | What it asks | Typical SQL | Default share |
|---|---|---|---|
| 1 | One governed metric: a total, or by one attribute | one metric, ≤1 `GROUP BY` | 20% |
| 2 | Two attributes, a filter, or a time split | metric + filter / two dims / `year(date)` | 25% |
| 3 | Ratios, shares, top-N, comparison inside one query | ratio metrics, `ORDER BY … LIMIT`, `CASE` in `GROUP BY` | 25% |
| 4 | Snapshot semantics: latest period, "ever in state", once-per-entity amounts, weighted averages | point-in-time metrics + latest-period filter, cohort by start period | 20% |
| 5 | Multi-step: prior-period denominators, transitions between states, cohorts over time, cross-entity comparisons | CTEs or self-joins over periods | 10% |

Write questions in the user's business language. Spread them over the use cases (weighted by
priority, at least one each) and business areas found in the domain research, and include some the
model probably cannot answer.

## Grading rubric (per question)

- **Verdict:** `pass` (matches the expected value within tolerance), `fail`, `error` (query
  failed), `empty` (no rows on either side), `ungraded` (no expected value).
- **Expected-value strength:** `reference` (trusted source) > `recomputed` (independent extract) >
  `same_model` (raw columns of the same model — consistency only).
- **Answer path:** `governed_metric` (a metric answers it directly), `simple_sql` (metrics plus
  ordinary filters / grouping), `expert_sql` (needs CTEs, window logic, or knowledge the model does
  not expose), `not_answerable` (data or metric missing). Record the path expected when writing the
  question and the path actually needed after running it.
- **Discoverability:** `clear` (name and description lead to the right metric), `ambiguous` (two
  plausible metrics), `misleading` (the obvious choice gives a wrong answer).
- **Performance:** seconds per query; note time-outs.

Headline score: verdicts by level, and answer paths overall. A model is in good shape when level 1-3
questions pass through governed metrics and level 4 questions pass through governed metrics or
simple SQL.

## Defect checklist

Walk this list during step 5 even when questions passed. Details and fixes in the linked notes.

**Wrong numbers**
- Snapshot facts summed over time: balances or statuses repeated each period and added across
  periods or across repeated post-exit rows (`reference_mosaic_fact_metric_aggregation.md`).
- Once-per-entity values (amounts recorded on every snapshot row) summed per row instead of per entity
  (`reference_mosaic_derived_metrics.md` §0d rule 1).
- A filtered metric over a fact metric with a level rule: the rule is dropped (§0d rule 4).
- Distinct counts of an attribute ignoring a period level rule (§0d rule 3).
- "Latest period" resolving per group, not overall (§0d rule 9) — compare a status metric grouped
  without a period against the same metric filtered to the latest period.
- Nested level metrics that ignore filters (§0d rule 5).
- Ratios at total grain computed as ratio of totals when the business means average of ratios (or
  the reverse) — check the definition in the domain notes.

**Missing or dropped rows**
- Compound metrics whose groups vanish when an operand is blank (`formulaJoinType`, §0d rule 7).
- Groups with no rows for any metric on the query drop out — needs a base count metric on the query.

**Types, lookups, attributes**
- Formula attributes stored as dates (year, month counts) — fix the form `dataType`.
- Codes with no description lookup; descriptions that don't match the provider's dictionary.
- Period attributes with no lookup table or no latest-period flag.
- Attributes keyed on the wrong table; isolated attributes (`build_mosaic.py validate-topology`).

**Coverage and discoverability**
- Common questions that need expert SQL where a governed metric could exist.
- Metrics with no description, or descriptions that don't say the grain, the period rule or the
  correct filter.
- Helper metrics that look like answers (name them as helpers in the description).
- Raw versions of metrics left visible next to the correct ones without a warning.

**Change side effects** (`feedback_mosaic_gotchas.md` "Model edits with silent side effects")
- Replacing or removing an attribute deletes the derived metrics that filter on it.
- Edits drop certification; MCP cannot see the model until it is re-certified.
- Lookup-table auto-detection may keep the fact table as the lookup.

**Query patterns that change answers** (`reference_mcp_tools.md`): `COUNT(DISTINCT CASE …)` next to
metrics, filters inside subqueries, `COUNT(*)`. If analysts will write these, the model needs a
governed metric so they don't have to.

## Change phases

Apply approved findings in this order; within a phase, follow `depends_on`.

| Phase | Name | Typical categories | After the phase |
|---|---|---|---|
| 1 | Source data and tables | `source_data`, new mapping or lookup tables | republish, re-certify |
| 2 | Types and lookups | `data_type`, `lookup` | republish, re-certify |
| 3 | Attributes and relationships | `attribute`, `relationship` | republish, re-certify |
| 4 | Fixes to existing objects | `metric_defect` | re-certify |
| 5 | New objects | `missing_metric` | re-certify |
| 6 | Names, descriptions, formats | `description`, `format`, `discoverability` | re-certify |
| 7 | Publish, certify, permissions | `governance`, `performance` | — |

Before every phase: a backup copy of the model; before phase 3, list the derived metrics that
filter on any attribute being removed or replaced. After every phase: the phase's unit tests, then
the whole bank as regression.

## File formats (JSON)

`assess_model.py check` validates all of these.

**domain_notes.json**
```json
{"version": 1, "domain": "<industry / process>",
 "claims": [{"id": "D1", "claim": "<definition or convention>",
             "sources": [{"title": "<document>", "type": "user_document | provider | regulator | standard | industry | other",
                          "url": "<link, when public>", "location": "<section or page>"}],
             "applies_to": ["<attribute or metric>"],
             "verified_against_data": "yes | no | contradicted | not_applicable",
             "check": "<query or note>"}],
 "use_cases": [{"id": "U1", "description": "<decision or report the model must support>",
                "audience": "<who>", "priority": "high | medium | low",
                "source": "<user document and section, user statement, or claim id>"}],
 "assumptions": ["<unsourced belief, stated as such>"]}
```
Source order: user-supplied documents (the organization's own definitions and use cases) →
provider dictionary → regulator / standard → industry reference. A claim from a user document needs
a title and a location, not a URL; keep the documents themselves out of the repo.

**question_bank.json**
```json
{"version": 1, "project": "<project>", "model": "<model>",
 "target_mix": {"1": 20, "2": 25, "3": 25, "4": 20, "5": 10},
 "questions": [{"id": "Q001", "level": 1, "area": "<business area>",
                "question": "<plain-language question>",
                "sql": "SELECT … FROM {{MODEL}} … LIMIT 100",
                "expected_path": "governed_metric | simple_sql | expert_sql | not_answerable",
                "metrics": ["<metric names used>"], "key": ["<result columns that identify a row>"],
                "measures": ["<numeric result columns to compare>"],
                "expected": {"source": "inline | reference_file | same_model_sql | none",
                             "strength": "reference | recomputed | same_model",
                             "rows": [], "file": "<path>", "sql": "<same-model SQL>",
                             "tolerance": 1e-6},
                "use_case": "U1", "domain_claims": ["D1"]}]}
```
Result column names in `key` / `measures` must match the SQL aliases.

**results.json** (written by `run`): per question `id`, `sql`, `ok`, `error`, `columns`, `rows`,
`row_count`, `seconds`.

**graded.json** (written by `grade`): per question `id`, `level`, `use_case`, `verdict`, `comparison`
(the validator's result), plus a `summary` by level, expected path, expected-value strength and use case. Add `path_needed`,
`discoverability` and `notes` during review.

**findings.json**
```json
{"version": 1, "model": "<model>",
 "findings": [{"id": "F001", "title": "<short>", "category": "metric_defect",
               "phase": 4, "impact": "high | medium | low", "effort": "small | medium | large",
               "evidence": {"questions": ["Q012"], "observed": "<…>", "expected": "<…>"},
               "recommendation": "<what to change and why>",
               "actions": [{"kind": "create_conditional_metric", "args": {…}}],
               "risks": ["<side effects>"], "depends_on": [], "republish": false,
               "tests": ["Q012"],
               "status": "proposed | approved | modified | declined | applied | verified | failed",
               "decision_note": ""}]}
```
`phase` defaults from `category` (table above). Action kinds `plan` turns into commands:
`create_conditional_metric`, `create_compound_metric`, `create_level_metric`,
`create_weighted_average`, `delete_derived_metric`, `patch_model_object`, `patch_fact_metrics`, `publish`,
`certify`, `change_column_type`, `source_change` and `wire_relationships` (below), `add_lookup_table` (manual
steps: add the CSV, map it to the existing attribute, give its key column the attribute's type, create
`new_attributes` as attributes of their own and `new_forms` as forms, leave out the CSV's `source` column,
check the lookup); anything else is `manual` with `steps`. Lookup suggestions say which columns are forms
(descriptions) and which are attributes (flags, calendar parents); `plan` warns when a description, name or
label column is listed as an attribute that no metric filters on.

- **Fix it in the model first; change the source last.** Before proposing a `source_change`, check
  whether the model can make the fix itself: a column type or date format is `change_column_type`.
  Only a change the model cannot express (new data, values that must be rewritten) needs the source
  changed and reloaded.
- **Where a new column comes from, in order of preference:**
  1. **A lookup table** for anything that depends on one attribute's value: descriptions, groups and
     flags of a code (`code_lookup`), calendar parents of a date (`date_lookup`: year and quarter; the
     reporting period's `period_lookup` adds the latest-period flag). A lookup is fast at run time, and it
     can be replaced later without deleting the metrics built on it.
  2. **A derived metric** for any calculation over measures. Create these freely.
  3. **A derived (formula) attribute** (`create_formula_attribute`) only when the user asks for one:
     it costs at run time, and a derived metric that filters on it or counts it is deleted if the
     attribute is ever replaced. `plan` refuses one without `args.requested_by_user: true`, and warns
     when a derived metric in the plan is built on one.
  4. **A source change** when none of these can produce it, for example a row-level state that depends
     on a blank (a blank key matches no lookup row).
- **Change the type first, test, then decide on the source.** A column read with the wrong type often
  comes back right once the type changes (leading zeros, letter codes), because the source still holds the
  original text. `plan` reports a column that has both a `change_column_type` and a `source_change`: keep
  the type change, add a test of the values, and plan the source change only if that test fails.
- **An empty column is a finding, not something to fill.** Don't write values calculated from other
  columns into a provider field. Give it the right type so it is ready for full data, say in its
  description that it is empty in this data, and compute anything derived (a running total) in a metric.

- **Anything a metric filters on must be an attribute.** An embedded filter targets an attribute, not a
  form, so lookup columns such as a latest-period flag or an active flag go under `new_attributes`.
  `plan` rejects a conditional metric that filters on a column the plan adds under `new_forms`.
- **`change_column_type`** (`column`, `type`: text | integer | bigint | double | date | timestamp,
  `date_format` for dates, e.g. `yyyyMM`, optional `table`) changes a column's type inside the model, with
  no reload: `build_mosaic.py set-column-types` sets the type in the table and its pipeline, then lines up
  the attribute forms and fact metrics that read it. Use it for codes stored as numbers (lost leading
  zeros, lost letter codes), numbers stored as text, and integer dates. `plan` merges every
  `change_column_type` into **one** command per table at the start of phase 2; the data changes at that
  phase's republish, so give each finding a test of the values afterwards. `column` may be the attribute or
  fact metric name the connector shows: the command resolves it to the column it reads. Leave `table` out
  on a one-table model. After the type phase, questions that compare the column with numbers (`= 1`,
  `/ 100`) need text or date comparisons.
- **`source_change`** (`table`, `column`, `change`) is a change the model cannot make: a new column from
  outside the model, sentinels to NULL, values rewritten (padding codes that the source stores without
  their zeros). `plan` gathers every `source_change` into **one** rebuild step at the start of phase 1,
  followed by one republish and re-certify. Each reload costs a re-map of attributes and fact metrics, so
  never plan several. `plan` reports a `source_change` whose text describes a type change.
- **`create_formula_attribute`** (`name`, `formula` in Strategy syntax with `[COLUMNS]`, `description`,
  `display_format` text | number | date, `data_type`, `table`, `update`, `requested_by_user`,
  `unverified_ok`): verified live for functions, columns and integers (`AddMonths([PERIOD], -1)`,
  `Year([date])` typed integer, `MonthsBetween`). String literals and decimals are unconfirmed and need
  `unverified_ok`; `plan` warns on any other function. The dry run checks the columns only: the tenant
  checks the functions when the step is applied, inside a changeset that is discarded if refused.
  Republish afterwards.
- **`wire_relationships`** (`relationships`: a list of `{parent_attribute, child_attribute,
  relationship_table, type}`, type one_to_many | many_to_many | one_to_one; `relationship_table` optional
  on a one-table model, matched loosely otherwise) declares parent-child
  relationships. `plan` merges them into one `build_mosaic.py wire-relationships` command at the start of
  phase 3 and ships its hints file as `specs/relationships.json`.
- **`rename_describe`** (`items`: `{object_type: attribute | fact_metric, current_name, new_name,
  description}`, descriptions at most 250 characters): `plan` merges every item into one
  `build_mosaic.py rename-describe` command in phase 6 and ships its spec as `specs/rename_describe.json`.
  Takes effect at once, no republish; derived metrics take their description when they are created.
  Use it for every rename and description change instead of a manual step.
- **Tests are not manual steps.** A manual step that only checks a result ("Test after republish:
  ...") becomes a `verify` note that `/verify-changes` handles; it doesn't stop the apply script. Write
  such checks as questions in the bank, linked through the finding's `tests`.
- **One lookup checklist per phase.** `plan` gathers a phase's `add_lookup_table` actions into one
  manual step (shared instructions once, one line per table). Adding a table from a file has no verified
  API yet, so this stays manual.
- **A lookup keyed on a retyped attribute gets a retype step.** When the plan changes a column's type
  and also adds a lookup keyed on it, `plan` follows the lookup step with `set-column-types --table <the
  CSV's name> --set <key> <type>`, so the lookup's key joins the retyped column.
- **Manual steps** may be a list; `plan` joins them into one numbered paragraph.
- **Spec files travel with the plan.** `change_plan.json` has a `files` map (relative path → JSON) that
  `plan` also writes next to it: the relationship hints, and the spec of any `patch_fact_metrics` action
  given inline as `args.metrics` (with optional `args.presets`) instead of `args.spec`.
- **Once-per-entity amounts and weighted averages** on a periodic-snapshot fact (one row per entity per
  period) are `create_level_metric` and `create_weighted_average`, not hand-written formulas: summing the
  raw fact counts an entity once per period, and a plain average weights by row count.

`args` use the CLI option names without dashes (`source_metric`, `count_attribute`,
`attribute`, `elements`, `and` as a list of lists, `level_rule`, `level_attribute`, `formula`,
`join`, `format`, `description`, `replace`, `kind`, `object_id`, `json_file`, `spec`; for
`add_lookup_table`: `csv`, `key_attribute`, `new_attributes`, `new_forms`; for `change_column_type`:
`column`, `type`, `date_format`, `table`; for `source_change`: `table`, `column`, `change`; for
`wire_relationships`: `relationships`; for `create_formula_attribute`: see above; for `rename_describe`: `items`; for `patch_fact_metrics`: `spec` or inline `metrics` / `presets`; for `create_level_metric`: `name`, `source_metric`, `entity_attribute`, `inner`, `outer`;
for `create_weighted_average`: `name`, exactly one of `value_attribute` / `value_metric`, `weight_metric`,
`entity_attribute`, `numerator_name`, `denominator_name`).

**change_plan.json** (written by `plan`): phases in order, each with its steps (finding id, action,
dry-run command, apply command), `before`, `unit_tests`, `regression` and `after` lists.
