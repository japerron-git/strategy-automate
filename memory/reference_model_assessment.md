---
name: Model assessment — question levels, grading, defect checklist, change phases, file formats
description: Reference for skills/strategy-model-assessment — the 1-5 question levels, the grading rubric (correctness, answer path, discoverability), a defect checklist distilled from field assessments, the seven change phases, and the JSON formats for domain notes, question banks, results, findings and change plans.
type: reference
---

Companion to `skills/strategy-model-assessment/SKILL.md` and its script
`skills/strategy-model-assessment/scripts/assess_model.py`. Domain-neutral: industry knowledge comes
from sourced research per assessment, never from this file.

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
`create_conditional_metric`, `create_compound_metric`, `delete_derived_metric`,
`patch_model_object`, `patch_fact_metrics`, `publish`, `certify`; anything else is `manual` with
`steps`. `args` use the CLI option names without dashes (`source_metric`, `count_attribute`,
`attribute`, `elements`, `and` as a list of lists, `level_rule`, `level_attribute`, `formula`,
`join`, `format`, `description`, `replace`, `kind`, `object_id`, `json_file`, `spec`).

**change_plan.json** (written by `plan`): phases in order, each with its steps (finding id, action,
dry-run command, apply command), `before`, `unit_tests`, `regression` and `after` lists.
