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

`assess_model.py lookups` first looks for the model's **existing lookup and mapping tables** — named by
a model export (`build_mosaic.py export-model`, then `lookups --model-export`), else inferred from the
probe (attributes listing values with no fact rows) and the relationships between them — and proposes
**extending** each one (`extend_lookup`: one CSV per table keyed on its most detailed code, starting
from the table's own rows and keeping every existing column, plus description columns) instead of a
new table and a second join. A replacement must keep every column its attributes read (`plan` refuses
one that drops a column: `add_lookup_table` with `extends_table` and `keep_columns`). It skips values
readable as they are (words such as "High", "Prepaid"; a missing definition there is a description
change; longer tokens such as deal names are identifiers, not codes), and reports rows the probe saw
lost through an attribute's lookup (`blank_key`, with the blank count), whether or not the attribute
gets a CSV; blank keys the probe saw kept are a note only. With the export it also writes the template
under the **table's own column names**, keeps **every physical column** (a form such as a bucket's sort
id reads a column the connector names differently), reads blanks as blanks (`CAST` alone turns a NULL
into 0, which a reload would write into the table), lists columns no attribute reads (copy them from
the source file), adds no description column for an attribute that already has a description or label
form, and reports **hidden forms** (`hidden_form`: forms the export has but get_semantics omits; they
answer as `"<attribute> (<form>)"`; make them visible instead of duplicating them). A hidden **key**
form is reported too, and is the worse case: nobody can filter or group on the codes. The connector may
list an attribute only through a form (`"<attribute>__code"`, `"<attribute> (id)"`); the export still
attaches it to its table, and a table's key is the attribute whose form reads the fact as well (the
join), queried by its connector name, so blank keys are counted on that join column. An attribute
belongs to every table its forms read, not only its lookup-table setting: the join attribute often has
the fact as its lookup table, and a description table may be set on no attribute. Each table's columns
are read through the form expression on that table (a period reads one column in the fact and
another in a calendar map). Hidden-form entries carry `key_form`. Without the export
the headers are connector names: don't replace a table from such a CSV. Time-attribute suggestions name
the table the column lives in (converting it changes the table for every model reading that file).
Then it recommends:
**code lookups** (coded text attributes with no description
form or attribute), **time attributes** for every date (a date stored as a number or text — yyyyMM,
yyyyMMdd, text yyyy-MM — is converted first; detected only when every sampled value and the min and max
are real dates of one pattern AND the name (date, period, month, as of, effective) or description says
so, an identifier-like name needing the description; calendar parents then come from year/quarter/month intervals, no CSV), a **period
lookup** for the latest-period flag only (when no flag exists; time attributes give no "latest"),
and, optionally, an **entity table** (many attributes fixed per entity). It writes CSV templates with
the key values from the data; description columns are filled only from cited sources (user documents,
provider dictionary), with the source named per row in the sources file, or left for the user.
Templates are written in the table's own load format (the export's import settings: separator,
quote character, header row, encoding; a new table uses the format most of the model's tables use),
with no `source` column: a column the table lacks breaks a reload. Sources go in the
`<file>_sources.csv` beside it, which is not loaded. With no quote character a value can't hold the
separator, a double quote or a line break (rephrase it; never switch separators on your own): verified
live, the loader splits on every separator when the table has no quote character.
`check-lookup --lookups lookups.json --id <id> --csv <file> --record` confirms a filled file is
load-ready (every line splits into the table's columns, the header matches exactly, the existing
columns are unchanged from the template row for row, blanks still blank); with `--project` after a
reload it also compares the reloaded table with the file. Add a lookup by mapping its
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

### Which rows a query reads: anchor on a fact metric (verified 2026-11-11 on two unrelated models)

An aggregate over attributes only runs on the attributes' lookup tables — the explicit ones, or the
narrow virtual lookup Mosaic makes per attribute when the model has none — not on the fact rows.
Aggregates of fact metrics run on the fact. So:
- `SUM(CASE WHEN <attribute condition> THEN 1 ELSE 0 END)` counted lookup rows: 1 (one matching status)
  and 12 (promotions) where the fact had 869 and 216 rows. `COUNT(*)` counted fact rows alone, but
  promotion-table rows (86) once a CASE on an attribute joined the query: never rely on it.
- `AVG`/`SUM` of a numeric attribute averages or sums the lookup's values (a mapping table's 01-35
  averaged to 18 where the per-entity answer was 9.6).
- Queries with attributes only list the lookup's members, including members with no fact rows, and a
  lookup that lacks a key drops those rows from any query that touches its attribute.
- Counts that held: `COUNT(<fact metric>)` with the condition in `WHERE`, and
  `COUNT(CASE WHEN <condition> THEN <fact metric> END)`; sums: `SUM(CASE WHEN <condition> THEN
  <fact metric> END)`. A condition on a metric (`CASE WHEN <metric> > 100 THEN 1`) also counted fact rows.
- `COUNT(<metric>)` skips blanks: count rows with the **anchor metric** — a raw fact metric filled on
  every row (`scope` records the fullest one as `anchor`), or a governed count metric.
- `GROUP BY` an arithmetic or cast expression of an attribute (`CAST(<period> / 100 AS INTEGER)`)
  groups by the attribute itself, even when regrouped in an outer query: use a lookup with the parent
  (year, quarter). CASE ranges in `GROUP BY` were reported to work (not re-tested).
- Not reproduced on a model without mapping tables (treat as model-specific when seen): `COUNT(DISTINCT
  CASE …)` / `FILTER` dropping `WHERE`, a metric inside `CASE` inside `SUM`, `IN (subquery)`, form
  columns inside subqueries, `DATE_DIFF` on real date columns.
- **Wrapping a metric's aggregate can change its value** (seen on one model, 2026-11-11): for fact
  metrics whose own aggregation is not a sum (a Max on loss amounts, an average on a rate), plain
  `SUM("<metric>")` returned the governed value (the Max: one loan's amount; the rate's average), while
  `CAST(SUM("<metric>") AS VARCHAR)` returned the per-row sum ($25.0M against $653k). Not reproduced on a
  model whose averages and ratios are derived metrics. `1.0 * SUM("<metric>")` kept the governed average
  there (shown with 1 decimal); `ROUND` is untested. So: read governed values plain, as analysts write
  them (the connector's thousands separators are parsed by the engine), and never wrap a non-sum metric
  in `CAST`; use `CAST` around a metric only when a row-level value is meant.
- **Averages of an integer-typed metric are truncated** (same model): plain `SUM("<avg metric>")` read
  FICO 746.87 as 746 and LTV 79.02 as 79 — truncated, not rounded — while `AVG("<avg metric>")` returned 3
  decimals. The metric's data type (integer) drops the fraction. Set the metric's type to a decimal; until
  then read it with `AVG(...)`. `probe` compares the two for every raw average metric. `probe` compares the two forms and
  flags a metric whose value changes (a Max or other non-sum aggregation as a warning; an average as a
  note, since that is expected; an empty column, NULL plain and 0 wrapped, as neither).
- Ratios of summed metrics: `SUM(a) / SUM(b)` on decimals came back with 3 decimals on one model;
  `1.0 * SUM(a) / SUM(b)` kept full precision. Arithmetic is safe around summed metrics only: for a
  non-sum metric, read each aggregate plain and divide afterwards.
- Models with several fact tables (user guidance, not yet tested here): a metric present in several
  facts can be steered to one fact by adding a hint metric from that fact to the query; use one anchor
  per fact table, and say in descriptions which hint metric to add.

**Saved SQL is checked for the no-regroup rule.** `question-sql` and `best-answer` refuse SQL that groups
by an expression instead of a plain column, at any level (an outer query over a subquery too), and
`check --bank` warns about any already in the bank. Seen live: a vintage grouped by
`CAST(date / 100 AS INTEGER)` returned each year several times (the comparison failed on duplicate
keys), and FICO bands grouped by a `CASE` timed out. Group by the attribute or its time attribute.
Number keys lose their thousands separators before comparison ("2,015" = "2015"). A query that times
out is graded `timeout`, not `error`: it says the query is slow (or wrongly shaped), not that the model
answered wrong. The maturity check for summed rates leaves out weighted-average parts (a numerator
`Sum(rate * weight)` or its denominator).

## Open items (engine and plugin)

- **Descriptions for derived metrics and the model.** `rename_describe` covers attributes and fact
  metrics only, so these fall back to manual steps. To verify live: a full-body PUT of a derived metric
  (read with `--show-expression-as tokens`, so the PUT keeps its expression) changes only its
  description; a PATCH of the data model sets the model description. Then both join the one phase-6
  command.

Resolved:
- *Showing hidden forms:* `show_forms` / `build_mosaic.py show-forms` adds forms to the attribute's report
  and browse displays. Verified by comparing two exports: the forms that became visible to the connector
  were exactly those added to both display lists (set in the UI); the build already sets displays
  through the same PATCH. Running it on a form already shown changes nothing.
- *Reloading a table:* no API replaces a model's file table in place (Push Data upload sessions are for
  datasets; publish reloads from the existing source), so the reload stays manual. The phase script
  exports the model before and after it and runs `relationship-diff`, which stops the script when a
  relationship was lost and lists the actions that restore it.
- *Which export and scope each output used:* `lookups`, `join-paths`, `probe` and `roles` record
  `sources` (file name and a short hash); the workbook shows them, warns when outputs were made from
  different exports, and marks a lookup "out of date" when its table is no longer in the model.
- *Questions out of approved findings:* `check --findings` warns about a manual step that is a question
  for someone; `plan` counts manual steps per phase.

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
- **Best answer:** an expert answer to every question, written during the assessment as SQL over the
  model's own columns (`question.best_answer`: sql or `not_answerable`, rationale, round), with whatever
  it takes to be right: once per entity, the latest period, sentinels excluded, distinct counts. It runs
  every round, including for questions the governed path can't answer. Ranking of what a question is
  graded against: an outside reference (`inline`, `reference_file`) > the best answer > a plain
  same-model check. Where an outside reference exists, the best answer is checked against it too
  (`best_vs_reference`). Set it with `assess_model.py best-answer --reason-kind initial`; change it only as
  an override with a recorded reason (`decision`: the user decided a definition; `definition`: the
  assessor chose one, for the user to confirm; `data_change`: the source changed; `sql_error`: the
  earlier SQL was wrong). The history keeps every version, and SQL identical to
  the question's own is refused. Grading flags `best_answer_moved` (same SQL, new result: the data or
  model changed under it), `best_answer_overridden`, and `suspicious_agreement` (the best answer moved
  and the question now passes: check that both didn't change for the same wrong reason). A best answer
  from the same model is the assessor's definition, not proof: prefer an outside reference when one exists.
- **Question SQL versions:** when a phase adds the object that answers a question (or its SQL was wrong,
  or a type change altered the values it filters on), rewrite it with `assess_model.py question-sql
  --reason-kind new_object|sql_error|type_change|decision --reason ...`; the earlier SQL and path stay in
  `sql_history`. Grading against the previous round reports `answerability`: questions that could not
  run before and now do (`newly_runnable`), paths that got simpler (`simpler_path`, e.g. expert SQL →
  governed metric) and the count answered by governed metrics before and after. Answerability gained is
  a better progress signal than the pass rate alone.
- **Probes** (`assess_model.py probe`): automatic checks that compare query shapes, because the common
  defects only show in some (two live rounds on one model: a probe that tested each object alone and
  unfiltered, or filtered on the period value, or read metrics through CAST, missed the defects).
  Shapes: alone; filtered with the filter analysts use — a latest-period flag (two values, "latest" or
  "current" in its name) when the model has one, else the latest period, or `--filter "<attr>=<value>"`;
  grouped with a partner attribute (3-30 values, `--split`). Attributes: fact rows kept in each shape
  against the matching anchor count (a lookup missing a key drops rows once a filter or second attribute
  joins); values an attribute-only query lists that have no fact rows, named; a numeric attribute read
  from its lookup (COUNT over it far below the fact rows). Metrics, every kind, read plain: the CAST form
  differs from the plain one (an expression bypasses the metric's own aggregation); the filtered total
  differs from the filter's row in the grouped query; the filtered total equals the unfiltered one
  though the filter drops most **entities** (entity-level values can't change when every entity is kept,
  so rows are the wrong measure); the same value in every group of the period, and of the partner under
  the filter; summed metrics whose period groups don't add up; one period only without "latest" in the
  name or description; a raw metric reading a different table from the anchor's (a lookup or mapping
  table, unless it is another fact). Gaps: periods with no fact rows inside a partner value's range.
  The assessment probes every attribute with at most 200 values and every metric; verify-changes
  probes what the phase touched (`--changed-since <previous scope> --findings findings.json --phases N`).
  A warning is evidence, not a verdict.
- **Cost** per question and round: queries, SQL characters sent, reply characters received and an
  estimated token count (characters / 4), for the governed answer and the best answer separately,
  totalled per level in the summary. It covers connector I/O only: a chat can't see its own token counts,
  so Claude's reasoning is not included. A falling cost to reach the right answer is a maturity signal.
  `run` also measures the model description (the `get_semantics` reply, which every analyst session
  reads first) as `summary.cost.model_description`: it grows with each metric and description, so a
  round can make answers cheaper and every session's start dearer. In replay mode, `run` asks for the
  `get_semantics` reply too (`semantics.txt`).
- **Baseline** (ungraded questions only): a reviewed answer frozen with `assess_model.py baseline`
  (`question.baseline`: rows, round, note). Grading marks the question `same` or `changed` against it and
  lists `baseline_changes`; the verdict stays `ungraded`. A baseline says what the model answered when
  someone checked it, not that it was right, so a change is reviewed (an intended fix or a regression),
  never counted as a fail, and baselines never count as passes. Freeze only answers reviewed and judged
  right; refresh a baseline after a finding changes it on purpose. Prefer a real expected value whenever
  an outside reference exists.

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
- Columns in the wrong role (`assess_model.py roles`): codes, flags and small counts read only as
  measures (users can't group or filter by them; summing a code means nothing), entity characteristics
  summed across a snapshot's rows, continuous values held only as attributes. A column can need both
  roles. Fix roles in phase 3, before metrics are built on them; add a question that groups by each
  confirmed column, so the fix is tested.

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

**Answers are enforced per phase.** A finding still `proposed` blocks the phases of its actions, or its
own phase when it has none, wherever it came from (assessment, verification, a check): `plan` gives the
phase `blocked_by` and no script is generated for it until the finding is answered. `plan_id` marks a
plan; scripts made from an older plan refuse to run.

**Test outcomes every round, not edits.** Tables and attributes change in too many ways to predict, so
each round (assessment and every verification) tests two outcomes:
- *Names:* `references` compares this round's connector names with the last round's. A form added can
  rename `attr` to `attr__id`; a renamed object keeps its export id. Saved SQL (question, best answer,
  expected) is rewritten where exactly one name replaces the old one (reason kind `model_change`);
  anything else is a finding that needs an answer. A best answer that moves after such a rewrite needs
  the user's confirmation.
- *Join paths:* `join-paths` groups `COUNT(<anchor>)` by each attribute, per fact table. With no join
  path Mosaic cross-joins: every group equals the total, and metrics filtering on the attribute filter
  nothing. Grouping alone misses a filter that is ignored when the attribute isn't selected (seen live:
  a flag filter returning a count unrelated to its group), so each attribute is also filtered on its
  smallest value and the count compared with that value's group. Every attribute is tested, by its key
  form when it has several forms; one mapped directly to a fact table can't cross-join it, but its filter
  is still tested. Verified live: a filter is dropped when the attribute has no parent-child
  relationship to the attribute its table joins the fact through (grouping still works through the
  shared column). Numbers come back with thousands separators ("201,812"), so filter values lose them.
  With the export, an ignored-filter finding proposes the missing relationship (the attribute → the one
  attribute of its table that joins the fact, through that table); a lost relationship is proposed back
  with its table. Findings from checks carry `checks` instead of tests: the next run lists them under
  `earlier_findings.resolved` (verified) or `still_present` (failed), and `add-findings` updates an
  unanswered one's proposal but never an answered one. A raw fact
  metric names a table before its column; `Sum({Other Metric})` is derived and never an anchor. Exports
  keep fact-metric expressions under `fact` and relationships under `hierarchy.relationships`. With the export it also checks which tables each attribute reaches and compares the declared
  relationships with the last round (lost ones are findings). In a model with several fact tables an
  attribute can join one and not another, so each fact table is tested. Single-value attributes can't
  be tested.

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
               "applied_phases": [4], "decision_note": ""}]}
```
`applied_phases` lists the phases of a finding that reached the model. A finding can span phases (a
metric in phase 4, its description in phase 6): once it is `applied` or `verified`, `plan` still plans its
actions in phases not listed, so verifying the first part never drops the rest. Without the field, `plan`
warns when an applied or verified finding spans phases.
`phase` defaults from `category` (table above). Action kinds `plan` turns into commands:
`create_conditional_metric`, `create_compound_metric`, `create_level_metric`,
`create_weighted_average`, `delete_derived_metric`, `patch_model_object`, `patch_fact_metrics`, `publish`,
`certify`, `change_column_type`, `source_change` and `wire_relationships` (below), `add_lookup_table` (manual
steps: add the CSV, map it to the existing attribute, give its key column the attribute's type, create
`new_attributes` as attributes of their own and `new_forms` as forms, load the file with the table's
import settings, check the lookup; `plan` refuses a `source` column and a failed `check-lookup`); anything else is `manual` with `steps`. Lookup suggestions say which columns are forms
(descriptions) and which are attributes (flags, calendar parents); `plan` warns when a description, name or
label column is listed as an attribute that no metric filters on.

- **Fix it in the model first; change the source last.** Before proposing a `source_change`, check
  whether the model can make the fix itself: a column type or date format is `change_column_type`.
  Only a change the model cannot express (new data, values that must be rewritten) needs the source
  changed and reloaded.
- **Where a new column comes from, in order of preference:**
  1. **A lookup table** for anything that depends on one attribute's value: descriptions, groups and
     flags of a code (`code_lookup`), the latest-period flag (`period_lookup`). A lookup is fast at run
     time, and it can be replaced later without deleting the metrics built on it. Calendar parents of a
     date are **time attributes** (`add_time_attributes`, after `change_column_type` for a number stored
     as a date); lookups for them (`lookups --calendar-lookups`) only when the column can't be converted.
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
  on a one-table model. Lookup and mapping tables already joined through the same attribute take the same
  type in the same command, so their joins keep working. After the type phase, questions that compare the
  column with numbers (`= 1`, `/ 100`) need text or date comparisons. Every command that takes an
  attribute name also accepts the connector's form names, `<attribute>__<form>` and `<attribute> (<form>)`.
- **`source_change`** (`table`, `column`, `change`) is a change the model cannot make: a new column from
  outside the model, sentinels to NULL, values rewritten (padding codes that the source stores without
  their zeros). `plan` gathers every `source_change` into **one** rebuild step at the start of phase 1,
  followed by one republish and re-certify. Each reload costs a re-map of attributes and fact metrics, so
  never plan several. `plan` reports a `source_change` whose text describes a type change.
- **`add_column_attribute`** (`name`, `column`: the column or the fact metric or attribute that reads it,
  `description`, `display_format`, `data_type`, `table`): an attribute that reads one existing column
  as-is, so a column read as a fact metric can also be grouped and filtered on. Not a calculation, so no
  `requested_by_user`; the fact metric stays. Phase 3 (category `attribute`), republish afterwards.
  The name must differ from every metric's (the command refuses a clash: the connector would show two
  columns with one name). `plan` refuses placeholders left from `roles.json` (`<...>`). Not yet run
  live: the body is the formula attribute's, verified, with a single column token.
- **`create_formula_attribute`** (`name`, `formula` in Strategy syntax with `[COLUMNS]`, `description`,
  `display_format` text | number | date, `data_type`, `table`, `update`, `requested_by_user`,
  `unverified_ok`): verified live for functions, columns and integers (`AddMonths([PERIOD], -1)`,
  `Year([date])` typed integer, `MonthsBetween`). String literals and decimals are unconfirmed and need
  `unverified_ok`; `plan` warns on any other function. The dry run checks the columns only: the tenant
  checks the functions when the step is applied, inside a changeset that is discarded if refused.
  Republish afterwards.
- **`wire_relationships`** (`relationships`: a list of `{parent_attribute, child_attribute,
  relationship_table, type}`, type one_to_many | many_to_many | one_to_one; `relationship_table` is
  optional: the command uses the one table both attributes read, preferring the parent's lookup table, and
  says which it chose) declares parent-child
  relationships. `plan` merges them into one `build_mosaic.py wire-relationships` command at the start of
  phase 3 and ships its hints file as `specs/relationships.json`.
- **`rename_describe`** (`items`: `{object_type: attribute | fact_metric, current_name, new_name,
  description}`, descriptions at most 250 characters): `plan` merges every item into one
  `build_mosaic.py rename-describe` command in phase 6 and ships its spec as `specs/rename_describe.json`.
  Takes effect at once, no republish; derived metrics take their description when they are created.
  Use it for every rename and description change instead of a manual step. Name the attribute, not a
  connector form name (`<attribute>__<form>`): forms share the attribute's description, so write one
  description that covers them. `plan` warns on form names and refuses a form rename or two different
  descriptions for one attribute; the command checks every item before writing and reports all problems
  at once. A description that quotes codes quotes them as stored after any type change ('01', not 1).
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
  raw fact counts an entity once per period, and a plain average weights by row count. Setting a fact
  metric's aggregation to Avg leaves that row weighting in place (an entity with more periods counts
  more), so propose the per-entity average with it: `create_level_metric` with `outer: Avg`
  (`Avg(Max([M]){~+,[Entity]+}){~+}`).
- **Counts by a status that can change read one period.** On a snapshot fact an entity's status (an exit
  code, a status tier) can change between periods, so "entities that ever had status X" picks up
  stale values. Count at the latest period (latest-period flag or level rule `last`) unless the question
  really asks for "ever". Check with the data: entities with more than one value of the status.

- **Percent formats follow the stored scale.** `percent` multiplies by 100, for values stored as 0-1
  fractions; values stored in points (a ratio of 76.01, a rate of 3.875) take `percent_0_100`, or `fixed2`
  where more decimals matter. Check the column's range before choosing: a derived metric's format can't be
  changed by these commands after it is created.

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
