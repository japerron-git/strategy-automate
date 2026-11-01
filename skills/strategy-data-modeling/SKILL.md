---
name: strategy-data-modeling
description: Design, review, migrate, and operationalize Strategy semantic models — Kimball-first. Business process, grain, conformed dimensions, attributes, facts, metrics, relationships, hierarchies, time semantics, and validation plans for Mosaic and classic surfaces. Strategy's SQL engine is built for star/snowflake schemas; this skill enforces that invariant before any REST write. Use it for "design a model", "review my model", "what is the grain", "star schema", "conformed dimension", "fact or dimension" or "plan a classic-to-Mosaic migration".
---

# Strategy Data Modeling (Kimball-first planning layer)

Use this skill when the user asks to design, inspect, explain, review, migrate, validate, or improve a Strategy / MicroStrategy semantic model. This skill is the **planning layer** that sits between `strategy-automation` (which classifies the surface) and execution skills (`skills/build-mosaic-model/SKILL.md`, `skills/strategy-validation/SKILL.md`).

**Skill precedence (one-way, no loops):**

```
strategy-automation (classify surface)
  └─► strategy-data-modeling (plan — this skill)
        └─► skills/build-mosaic-model/SKILL.md (build-mosaic-model) OR legacy mining
              └─► strategy-validation (verify)
```

Do NOT route back up the chain. This skill calls execution skills; execution skills do not call this one.

To review an existing model **against live data** — business questions run through MCP, graded, and
turned into an ordered change plan — use the sibling planning skill
`skills/strategy-model-assessment/SKILL.md` instead; this skill reviews the design.

## Required memory (load on entry)

Kimball foundations (load first, every time):

- `memory/reference_data_modeling_foundations.md` — the ONE Kimball design file: grain, conformed dims, star/snowflake topology, additivity, anti-patterns, plus sections for Strategy schema objects (Kimball → object mapping), Attribute design, Fact and metric design, Relationship design, Hierarchy design, and Time modeling
- `memory/reference_strategy_data_validation.md` — 10-check design suite + 5-query runnable suite

Mosaic work (additional):

- `memory/checklist_strategy_automation_modeling_playbook.md` — mandatory pre-build pass: surface routing, build sequence, changeset/rollback gates, publish readiness, validation close-out
- `memory/feedback_mosaic_relationship_wiring.md` — conformed-dim encoding + error-code fix recipes
- `memory/feedback_mosaic_ship_bar.md`

Classic / migration (additional):

- `memory/reference_strategy_legacy_to_mosaic_mining.md` — start-here hub; classic inventory checklist + object→Mosaic migration mapping

## Output contract — Kimball-first model plan

For any modeling request, produce or update a model plan containing:

1. **Topology declaration** — `star | snowflake | galaxy | bridge-heavy | non-Kimball`. Non-Kimball stops the work and confirms with the user.
2. **Table classification** — every input table labeled `fact | dim | bridge | snowflake_parent_dim | degenerate_dim | noise`.
3. Business process
4. Declared grain (per fact table)
5. Conformed-dim enumeration — every entity that appears in ≥2 tables, with the list of (table, column) expressions it must span
6. Source systems and tables
7. Attributes and forms
8. Facts
9. Governed metrics (each tagged with additivity class: `additive | semi-additive | non-additive | derived`)
10. Relationships (parent/child/relationship_table/type, and the archetype from `reference_mosaic_relationship_archetypes.md`)
11. Hierarchies
12. Time roles and transformations
13. Security / governance notes
14. Assumptions and open questions
15. Validation suite
16. Build sequence

Prefer the templates in `skills/build-mosaic-model/examples/`:

- `model_plan_template.yaml`
- `attribute_plan_template.yaml`
- `relationship_plan_template.yaml`
- `validation_suite_template.yaml`

## Non-negotiable rules

- Do not create facts, dimensions, or relationships before declaring grain.
- Do not mix grains in one fact table.
- Do not create relationships from column-name similarity alone.
- Validate relationship cardinality before relying on rollups.
- Prefer governed metric definitions over exposing many raw aggregations.
- Treat hierarchies as user navigation, not merely joins.
- Use changesets for metadata writes and discard on failure.
- Close every build with validation or an explicit validation-pending note.

## Required workflow

1. Parse the business intent.
2. Identify the business process or processes.
3. Declare the grain in one sentence.
4. Identify candidate fact tables and dimension tables.
5. Draft attributes, forms, facts, metrics, and time roles.
6. Draft relationship and hierarchy plans.
7. Identify security, governance, and naming constraints.
8. Turn the draft into a structured plan file.
9. Run discovery / profiling to confirm keys, nulls, and cardinality.
10. Refine the plan from evidence before writes.
11. Hand off to the execution skill.
12. Validate and write durable findings back to memory.

## Relationship safety checks

Before relationship writes:

1. Confirm child and parent attributes exist.
2. Confirm both have ID forms.
3. Confirm required expressions exist on the relevant tables.
4. Confirm cardinality with profiling evidence.
5. Confirm the relationship is not already implied by co-resident attribute mappings.
6. Confirm the current changeset stage accepts relationship writes.

## When to ask for clarification

Stop and ask when:

- multiple grains are plausible
- metric definitions are ambiguous
- fiscal / calendar rules are unknown
- a many-to-many path may duplicate totals
- security requirements are unclear

You may proceed best-effort only when the user explicitly wants automation from known tables, the grain is high-confidence, and assumptions will be recorded and validated.

## Final answer format

Use this structure:

- Modeling summary
- Decisions made
- Model plan
- Validation plan
- Build steps
- Risks / assumptions
- Files changed or commands to run
