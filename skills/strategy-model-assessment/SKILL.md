---
name: strategy-model-assessment
description: Assess an existing Strategy Mosaic model against live data and the business it serves — scope the model, research the industry and its use cases with cited sources (documents the user supplies first), write a graded question bank (levels 1-5), run it through the Mosaic MCP connector, grade correctness and discoverability, and turn defects, limitations and enhancements into an ordered, approvable change plan with test plans. Use it for "assess this model", "how good is this model", "what's missing from the model", "test the model with business questions", "can an analyst answer X", "model fitness review" or "what should we fix before production".
---

# Strategy Model Assessment (evidence-based review → change plan)

Use this skill to find out how well an existing model answers the questions its users will ask,
and what to change so it answers them correctly and easily. It reviews a model **by asking it
questions against live data**, not only by reading its schema.

**Where it sits.** This is a planning-layer skill, next to `skills/strategy-data-modeling/SKILL.md`
(which plans a design from the schema). It ends with a change plan and hands off; it never applies
changes itself:

```
strategy-model-assessment (assess → findings → approved change plan)
  └─► skills/build-mosaic-model/SKILL.md   (apply the plan, phase by phase)
        └─► skills/strategy-validation/SKILL.md   (verify with the same question bank)
```

A re-assessment after the changes is a new run that the user starts — not a loop between skills.

## Required memory (load on entry)

- `memory/reference_model_assessment.md` — level definitions, grading rubric, defect checklist,
  change phases, file formats.
- `memory/reference_mcp_tools.md` — the Mosaic MCP tools and the SQL shapes that change answers.
- `memory/reference_mosaic_derived_metrics.md` — §0d (periodic-snapshot metrics) when the model has
  a snapshot fact.
- `memory/reference_data_modeling_foundations.md` — grain, additivity, conformed dimensions.
- `memory/feedback_mosaic_ship_bar.md` — naming, descriptions, formats.
- `memory/checklist_strategy_model_review.md` — post-build review checklist (business fit, rollups,
  hierarchies); use it alongside the defect checklist.

## Inputs to confirm

- Project and model name (the model must be **certified** to be visible to MCP).
- Purpose and audience, if the user knows them (who asks what, for which decisions).
- **Documents the user can supply** for industry or use-case expertise: the organization's data
  dictionary or glossary, KPI and metric definitions, report or dashboard specifications, business
  requirements, policy or methodology documents, the data provider's guide. Ask for them; they
  outrank public sources for how *this* organization defines and uses things.
- Question count and level mix (defaults: 100 questions; mix 20/25/25/20/10 for levels 1-5).
- Trusted references for expected values, if any (source extract, warehouse SQL, another model,
  a published report). Without one, correctness grading is consistency-only — say so up front.
- Whether a backup copy of the model exists before any change is applied.

## Workflow

All files go in one working folder per assessment (outside the repo, or under a dated `captures/`
folder kept local — they hold tenant data). Script: `skills/strategy-model-assessment/scripts/assess_model.py`
(signs in to MCP like the connector does; see `skills/strategy-platform/scripts/strategy_mcp.py`).

**Any model, any stage.** The same workflow serves a raw automated build and a curated, governed model;
the maturity estimate from step 1 sets the emphasis (see "Maturity levels" in the memory note).

**Sessions where the script cannot sign in** (a cloud workspace with the Mosaic connector attached):
add `--replies <folder>` to `scope`, `relationships`, `lookups` and `run`. The script then lists the
calls it needs in `<folder>/pending.json`; call each connector tool with the listed arguments, save
the reply text to the listed file (start it with `ERROR:` if the tool failed), and run the same
command again until nothing is pending (exit code 3 means "replies still needed").

### 1. Scope the model → `scope.json`

```bash
python3 $REPO/skills/strategy-model-assessment/scripts/assess_model.py scope \
  --project "<project>" --model "<model>" --out scope.json
```

Records every attribute and metric (type, description, formula) from `get_semantics`, and profiles
each attribute with bounded queries: distinct count, min / max, sample values for low-cardinality
columns. On large models pass `--where` to scope profiling, or `--no-profile`. For the declared
relationships, lookup tables and derived-metric definitions, add the REST view where it can run:
`build_mosaic.py get-model-object --kind data_model --model-id <id>` and `validate-topology`.

`scope` also writes a **maturity estimate** (M0 raw build → M4 analyst-ready) from names,
descriptions, formulas and profiles: default names, metrics that look like attributes (IDs or codes
summed), numeric attributes, decoded codes, a latest-period flag, constant attributes. Confirm it,
then set the emphasis: an M0-M1 model needs grain, aggregation, naming, lookups and relationships
first (group repeated defects into one finding per pattern); an M3-M4 model needs edge semantics,
discoverability and level 4-5 questions.

### 1b. Relationships → `relationships.json`

```bash
python3 assess_model.py relationships --project "<project>" --scope scope.json --out relationships.json \
  [--entity "<entity key>"] [--declared declared.json]
```

Tests, in the data, whether each child value maps to exactly one parent value. The entity key is
tested against every attribute (**fixed per entity** vs **varies within the entity** — e.g. over
periods); other pairs are ranked by name affinity and cardinality and tested in batches. Results:
- **parent-child** candidates (direct parents only), for drill paths, hierarchies and level metrics
  (a level attribute must be an ancestor of the report rows);
- **one-to-one** pairs — usually a code and its description that should be forms of one attribute,
  or a redundant attribute;
- **entity-level** attributes — candidates for an entity table, and the attributes that must be
  counted or summed once per entity.

With `--declared` (a list of the model's relationships as `[{child, parent}]`, from the REST view),
it reports declared relationships the data contradicts and supported ones not declared. A dependency
in the data is evidence, not proof: recommend a relationship only when the domain notes say it makes
business sense — "weak" evidence (fewer than 30 child values) is often coincidence in small samples.

### 1c. Lookup tables → `lookups.json` and CSV templates

```bash
python3 assess_model.py lookups --project "<project>" --scope scope.json \
  --relationships relationships.json --csv-dir lookups/ --out lookups.json
```

Recommends lookup tables where the model needs them: coded attributes with no description form
(codes explained only in a description, or not at all), a period table with calendar parents and a
latest-period flag when there is none (point-in-time metrics otherwise resolve "latest" per group),
and an optional entity table. **Offer the user the CSVs**: the script writes a template per lookup
with the key values taken from the data; fill the description columns only from cited sources (the
user's documents or the provider's dictionary, step 2) and name the source in the `source` column —
or leave them blank for the user. A lookup becomes a finding with an `add_lookup_table` action; the
change plan turns it into the steps to add and map the table (map it to the **existing** attribute:
replacing an attribute deletes the derived metrics that filter on it). List lookup columns that metrics
will filter on (flags) under `new_attributes`: a filter can target an attribute but not a form.

From the scope, write down: fact grain (one row per what, per when?), snapshot vs transaction facts,
the entity key, the period attribute, entity status / exit fields, metrics that look additive but
are not, and attributes with no description.

### 2. Become a subject-matter expert → `domain_notes.json`

Two kinds of knowledge, kept apart:

- **Model facts** — what this model holds (from step 1). No citation needed; the scope is the source.
- **Domain knowledge** — how the industry works, *outside* the model: standard measures and their
  exact definitions, reporting and regulatory conventions, the data provider's own field dictionary
  and code lists, the questions analysts routinely ask, known pitfalls (double counting, timing
  lags, cohort conventions).

**Sources, in this order:**

1. **Documents the user supplied** (`type: user_document`) — read them first. They define the
   organization's own measures, use cases and conventions; cite the document and the section or
   page. Where an organization's definition differs from the industry's, keep both claims and say
   which one the model should follow — the difference is often a finding.
2. **The data provider's published dictionary and user guide** (`type: provider`).
3. **Regulators and standards bodies** (`type: regulator | standard`).
4. **Reputable industry references** (`type: industry`).

**Every domain claim needs a source** (title, plus URL or the document's section / page). Do not state industry definitions from
memory as fact; mark anything unsourced as an assumption. Then **check claims against the data**
where possible (does the documented code list match the codes present? does the documented lag
match the period fields?) and record the result. Contradictions between the dictionary and the
data are findings.

**Use cases.** Record the uses the model must serve in `domain_notes.json` `use_cases` — from the
user's documents first, then from what the user says, then from the research — each with the
audience, a priority (high / medium / low) and its source. The question bank is built against them.

Supplied documents may be confidential: cite them by name and section, quote no more than a
definition needs, and keep them and the notes out of the repo.

`assess_model.py check --domain domain_notes.json` rejects claims without a source; with `--bank` as
well, it reports use cases that no question covers and questions that cite unknown claims or use cases.

### 3. Write the question bank → `question_bank.json`

Write questions a real analyst, manager or regulator in this domain would ask, spread across
levels 1-5 (definitions in `memory/reference_model_assessment.md`) and across the use cases and
business areas found in step 2 — more questions for high-priority use cases, at least one for every
use case. For each question record: the business question in plain words, the SQL that
answers it (governed metrics first; `{{MODEL}}` for the model name), the expected answer path
(`governed_metric | simple_sql | expert_sql | not_answerable`), the columns that key the result,
the expected-value source, the use case it serves, and the domain claims it relies on. Include questions the model probably
*cannot* answer — they find missing data and metrics.

Expected values, strongest first:
1. **Trusted reference** — rows from a source extract, warehouse SQL, another model or a published
   report (`expected.source: reference_file | inline`).
2. **Independent recomputation** — compute from an extract of the source data outside Strategy.
3. **Raw-column SQL on the same model** (`expected.source: same_model_sql`) — checks consistency
   only (a governed metric vs its own columns); never call it proof of correctness.

`assess_model.py check --bank question_bank.json` validates ids, levels, paths, and reports the
level mix against the target.

### 4. Run and grade → `results.json`, `graded.json`

```bash
python3 assess_model.py run   --project "<project>" --bank question_bank.json --out results.json
python3 assess_model.py grade --bank question_bank.json --results results.json --out graded.json
```

`run` executes every question through the MCP `query` tool and records rows, timing and errors.
`grade` compares rows with expected values (the comparison engine of
`skills/build-mosaic-model/scripts/strategy_validate_models.py`) and summarizes by level, path and
use case.
Then review each question by hand for the parts a script cannot judge:

- **Correct?** — pass / fail / error / ungraded (no expected value).
- **Answer path actually needed** — governed metric, simple SQL, expert SQL, or not answerable.
  A question that needed expert SQL where a metric could exist is an enhancement.
- **Discoverable?** — would an analyst (or an AI agent reading `get_semantics`) pick the right
  metric from its name and description? Wrong-metric risk is a finding.
- **Fast enough?** — note slow queries and time-outs.

For an unbiased read of discoverability, have a fresh client with no context answer a sample of the
questions using only the connector, and compare its answers with the graded ones.

### 5. Findings → `findings.json`

Include the structural findings from steps 1-1c (maturity, relationships, lookups) as well as the
question results. Group what failed or was hard into findings, each with category, impact, effort, evidence (question
ids, observed vs expected), recommendation, proposed actions, risks, dependencies and the questions
that will test the fix. Walk the defect checklist in `memory/reference_model_assessment.md` even for
questions that passed — several defects (per-group "latest" periods, rows dropping from results)
only show up at certain groupings.

Impact: **high** = wrong numbers or a core question unanswerable; **medium** = right numbers only
with expert SQL, or easy to misuse; **low** = naming, formatting, convenience.

Present the findings to the user for **approve / modify / decline**. Record each decision in
`status` and `decision_note`. Nothing is applied without approval.

### 6. Change plan and test plans → `change_plan.json`

```bash
python3 assess_model.py plan --findings findings.json --model-id <modelId> --out change_plan.json
```

Orders approved findings into the seven phases (source data → types and lookups → attributes and
relationships → fixes to existing objects → new objects → names, descriptions, formats → publish,
certify, permissions), respecting `depends_on`. Every change to the source data (new columns, types,
sentinels) should be a `source_change` action: `plan` merges them all into one rebuild at the start of
phase 1, so the source is reloaded once. It writes `build_mosaic.py` commands for each action
it knows (a `--dry-run` command and an apply command), manual steps otherwise. Each phase lists:

- **Before:** a backup copy of the model; for any attribute removal or replacement, the derived
  metrics that filter on it (they are deleted with it).
- **Unit tests:** the questions named in each finding's `tests`.
- **Regression tests:** the whole bank (`run` + `grade`), compared with the previous `graded.json`.
- **After:** republish when a phase changed tables, attributes or fact metrics; re-certify after any
  edit (edits drop certification, and MCP cannot see an uncertified model).

### 7. Apply and verify (handoff)

Apply each phase with `skills/build-mosaic-model/SKILL.md` (dry run → user approval → apply), then
verify with `skills/strategy-validation/SKILL.md` using the same question bank. Until the validator
reads question banks directly, `assess_model.py run` + `grade` is the runner.

### 8. Update the findings

Set each finding to `applied`, `verified` or `failed` with the evidence; add new findings that the
re-run exposed. The updated `findings.json` is the record for the next round.

## Rules

- Read-only until a change plan is approved; every write goes through `build-mosaic-model`.
- Never present unsourced industry knowledge as fact.
- Prefer governed metrics in question SQL; when a question needs raw SQL, that is evidence.
- Avoid SQL shapes that change answers (`COUNT(DISTINCT CASE …)` next to metrics, filters inside
  subqueries, `COUNT(*)`) — `memory/reference_mcp_tools.md`.
- Keep tenant data, model names and results out of durable repo files; working files stay local.
- Keep each question's SQL bounded (`LIMIT`, filters) on large models.

## Final answer format

- Scope summary (grain, entities, periods, maturity estimate, notable gaps)
- Relationships and lookup tables recommended (with the CSVs offered)
- Domain summary with sources (user documents first) and the use cases with their coverage
- Score: pass / fail / ungraded by level; answer paths (governed / simple / expert / not answerable)
- Findings by impact, with the decision needed from the user
- Change plan by phase with test plans (after approval)
- Files written
