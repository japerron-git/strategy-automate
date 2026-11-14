---
name: build-mosaic-model
description: Build a Strategy Mosaic (MicroStrategy) semantic model from scratch against live warehouse tables. Use when the user asks to "build a mosaic model", "create a semantic model in Strategy", or provides a DB instance + schema + tables and wants a model wired up. Handles auth, warehouse-table discovery, model/table/attribute/metric creation, relationships, ACL, security filters, publish, and post-build edits via `scripts/build_mosaic.py` against the Strategy REST API. Kimball-first — expects star / snowflake / galaxy topology with conformed dimensions.
---

# Build a Strategy Mosaic model from scratch

This skill is the **execution layer** for Mosaic data-model creation and modification. The **planning layer** is `skills/strategy-data-modeling/SKILL.md` — route through it first unless the user gave you a complete pre-planned build.

## Scope

Owns the Modeling and datasource API areas (`python3 skills/strategy-platform/scripts/strategy_api.py ops --tag build-mosaic-model`): Data Models, Mosaic, Changesets, Workspaces (plus the internal Model Server, Open Semantic Layer and Data Server), the classic schema-object areas (Attributes, Facts, Metrics, Filters, Prompts, Custom Groups, Consolidations, Derived Elements, Transformations, Hierarchies, System Hierarchy, User Hierarchies, Tables, Schema, Base Formulas, Subtotals, Calendars, Drill Maps), Security Filters, and Datasource Management, Drivers and Gateways. Classic objects and datasource administration have their own sections below.

**Out of scope for this skill** — route to `skills/strategy-automation/SKILL.md` and `memory/reference_strategy_surface_matrix.md`:
- Runtime analytics (reports, dashboards, documents, prompt answers, exports)
- AI / Agent / Bot work
- Non-Mosaic cube / dataset work (Intelligent Cube, OLAP cube, Super Cube, MTDI, Push Data)
- Subscriptions, users, groups, roles, VLDB, object administration

## Load these memory files on entry

Every one of these owns a piece of the build surface; this skill delegates the detail to them. Load before synthesizing any payload.

- `memory/reference_strategy_error_codes.md` — grep this **FIRST** on any 4xx/5xx before looking anywhere else.
- `memory/reference_data_modeling_foundations.md` — Kimball: grain, conformed dims, star/snowflake/galaxy/bridge topology, additivity classes.
- `memory/reference_mosaic_business_logic_translation.md` — intent → build plan; the mandatory pre-build artifact.
- `memory/feedback_business_logic_pass_mandatory.md` — non-optional rule.
- `memory/feedback_mosaic_relationship_wiring.md` — conformed-dim recipe + the `8004ccdb` / `8004ccc7` / `8004e409` failure modes + fix pattern.
- `memory/feedback_build_mosaic_session_leak.md` — one session per pipeline (one process or `MSTR_REUSE_SESSION=1`); the session cap was the most common cause of mid-build failure.
- `memory/reference_mosaic_rest_api.md` — verified endpoint paths (auth, datasources, catalog, data models, changesets, security, translations).
- `memory/reference_mosaic_modeling_concepts.md` — payload shapes for attributes, metrics (compound / conditional / level / transformation), relationships, filters, transformations.
- `memory/reference_mosaic_publish_path.md` — the one publish file: 3-step flow, do-not-fire-both rule, dataType publish-readiness gate (prevents iServerCode -2147212544 stalls).
- `memory/reference_mosaic_vs_legacy_surfaces.md` — subtype 779 + extType 448 (Mosaic) vs other subtype 779 (data-import cube) vs 776 (classic cube) classification before any publish/refresh/execute write.
- `memory/reference_strategy_object_cloning.md` — clone-and-remap procedure when a payload shape is unknown.
- `memory/feedback_mosaic_ship_bar.md` — ship-bar checklist: naming, DESC-form report/browse displays, metric format tokens, description length cap, SF naming.

## User inputs

- **DB instance name** (looked up by name, resolved to `dbInstanceId`)
- **Schema** (warehouse namespace)
- **Table names** (may span multiple DB instances; each `(instance, schema, table)` triple is treated independently but all tables land in one model)
- Optional: derived metrics, security filters / ACL grants or denies, `data_serve_mode` (`connect_live | in_memory | off_memory`), translations, certification, publish / refresh instructions.

**Destination folder and project** are read from env vars (`MSTR_BASE`, `MSTR_PROJECT_ID`/`MSTR_PROJECT_NAME`, `MSTR_DEST_FOLDER_ID`). See `memory/reference_strategy_env.md`.

## Deliverable

A committed Mosaic data model containing:

- One logical table per input physical table
- Attributes (one per ID/key/text column, multi-table expressions for conformed dims)
- Fact metrics (aggregation function chosen per column semantics — never blanket SUM)
- Relationships (inferred from shared keys + ERD/dictionary overrides)
- Security filters, ACL grants/denies, translations, certification as requested
- Optional publish (for in-memory) or serve-mode switch
- Model URL: `{BASE}/app/library#/model/{modelId}`

## Execution flow — one session

**Always use `scripts/build_mosaic.py`**. Do not re-implement REST calls inline. Keep the pipeline in one session — `build-from-config`, one Python process, or separate subcommands under `MSTR_REUSE_SESSION=1` (end it with `strategy_auth.py logout`); see `memory/feedback_build_mosaic_session_leak.md`.

1. **Confirm env.** `MSTR_BASE` + project + a sign-in method: `MSTR_API_TOKEN`, `MSTR_USER`+`MSTR_PASSWORD`, or none at all for single sign-on (`--auth-method sso`; the human clicks **Allow** once in their browser and later runs reuse the cached session). Never hardcode or print secrets, and never pass them as flags. See `memory/reference_strategy_authentication.md`.
2. **Auth (handled by script).** `strategy_auth.py` signs in (`POST /api/auth/login`, or `/api/auth/delegate` for SSO); scripts log out with `POST /api/auth/logout` unless the session is shared with a browser. The Modeling identity token (`X-MSTR-IdentityToken`) is grant-dependent: some tenants need it for Mosaic writes, on others it downgrades privileges (`8004cb09`) — see `memory/feedback_mosaic_identity_token_privilege_downgrade.md`. Never add it to classic/project workflows.
3. **Resolve DB instance.** `list-datasources --name <substr>`; fail loud on ambiguity.
4. **Discover warehouse tables.** `describe-tables` (plural — one login per run). Endpoints are `GET /api/datasources/{id}/catalog/namespaces/{namespaceId}/tables` and `GET /api/datasources/{id}/catalog/namespaces/{namespaceId}/tables/{tableId}` with base64 namespace/table IDs. Use `discover` for live path variants if on an unfamiliar iServer build.
5. **Translate business logic → build plan.** Produce the artifact described in `memory/reference_mosaic_business_logic_translation.md`: topology declaration, table-role classification (fact/dim/bridge/etc.), grain per fact, conformed-dim enumeration, attribute plan, metric plan (with additivity), relationships, assumptions log. Mandatory even with no supplied context — inspection-only inference is still a pass, not a skip.
6. **Preflight gate (ERROR-severity = stop).**
   ```bash
   python3 scripts/preflight_model_check.py \
     --instance "<DB Instance>" --schema <SCHEMA> \
     --tables <T1> <T2> ... \
     [--blueprint /tmp/blueprint.json] \
     --out /tmp/preflight.json --fail-on ERROR
   ```
   Exit code 1 = fix input before calling `build`. See `memory/reference_mosaic_preflight_skill.md`.
7. **Build.** Either:
   - `build --name ... --source "INSTANCE:SCHEMA:T1,T2,..." --dictionary /tmp/dict.json [--erd /tmp/erd.dbml] [--data-serve-mode ...] [flags for SF/ACL/translate/certify/publish]`, OR
   - `build-from-config --config spec.yaml` for declarative multi-step builds (preferred when applying SF + ACL + publish + certify).
8. **Verify conformance.** For every attribute you expect to be a conformed dim, `get-model-object --kind attribute --model-id ... --object-id ...` and confirm `forms[*].expressions[*].tables` covers every expected table. If missing, PATCH before writing relationships. See `memory/feedback_mosaic_relationship_wiring.md`.
9. **Relationships (second changeset).** Issue only the PUTs that pass the step-3 and step-5 prerequisites in the wiring recipe.
10. **Quality gate.** `validate-model --model-id M [--strict-orphans] [--diff-against OTHER_ID] [--json]` — enforces the rules in `feedback_mosaic_build_quality.md`. Exits non-zero on failure.
11. **Publish (in-memory only).** `publish --model-id M`. ONE trigger per run — never fire `/api/cubes` AND `/api/dataModels/{id}/publish` together (see `memory/reference_mosaic_publish_path.md`). For `connect_live` models, skip publish — it's a no-op.
12. **Validate data correctness.** Route through `skills/strategy-validation/SKILL.md` with a trusted comparator. A build without validation is not shippable.
13. **Print the model URL.**

## Naming, descriptions, and inputs from ERDs / dictionaries

The skill uses a three-tier fallback for every attribute + metric:

1. **Explicit override** — `--dictionary path.{json,yaml,csv}` entry for `TABLE.COLUMN`.
2. **ERD relationships** — `--erd path.{json,yaml,dbml,mmd,sql}` (repeatable) replaces relationship inference. Shared-column conformance still runs.
3. **Inference** — friendly-title-case column name; metric name `Total <Col> (<Short Table>)`; shared-column → `one_to_many` with child table as the join table.

Supported ERD formats (parsed by `load_erd`): JSON/YAML list of `{parent,child,relationship_table,type}`; DBML `Ref:`; Mermaid `erDiagram`; SQL DDL `REFERENCES`.

Supported dictionary formats (parsed by `load_dictionary`): JSON/YAML with `attributes`, `metrics`, `relationships`, `tables`; CSV with `table, column, kind, name, description, function`.

If the user supplies an **image ERD**, Claude reads it in-session, converts to the supported format, saves to `/tmp/<model>.erd.json`, and passes via `--erd`.

**Descriptions are Claude's responsibility.** Auto-generate business descriptions from column/table semantics before the build runs — never ship mechanical "Column X from table Y" descriptions when domain knowledge yields something meaningful. Only fall back to the generic template when the column name is truly opaque.

## User + access preflight

Before any ACL / security-filter / user writes:

```bash
python3 scripts/build_mosaic.py resolve-users --user <name-or-email>   # or --file users.csv
python3 scripts/build_mosaic.py search-objects --name "<Object>"        # get object IDs
python3 scripts/build_mosaic.py get-model-object --kind legacy_attribute --object-id ATTR_ID --show-expression-as tokens --out /tmp/before.json
```

Only PATCH after the exact ID and body are reviewed:

```bash
python3 scripts/build_mosaic.py patch-model-object \
  --kind legacy_attribute --object-id ATTR_ID \
  --json-file /tmp/attribute.patch.json \
  --before-out /tmp/attribute.before.json \
  --yes
```

Modeling Service `PATCH` replaces top-level fields — start from a current `GET`, keep everything that must survive, verify with another `GET`.

## Classic schema objects (project semantic layer)

Attributes, facts, metrics, filters, prompts, hierarchies, transformations and project security filters are top-level Modeling Service objects (`/api/model/...`), not Mosaic data-model objects. Plan changes with `skills/strategy-data-modeling/SKILL.md`. Recipes: `memory/reference_strategy_legacy_semantic_admin.md` (attribute + fact create verified 2026-08-25; security filters) and `memory/reference_strategy_tutorial_semantic_field_study.md`.

- Read and patch existing objects with `get-model-object` / `patch-model-object` ("User + access preflight" above; `--kind project_attribute|project_fact|project_metric|project_table|project_filter|project_hierarchy|project_transformation`, aliases `legacy_attribute|legacy_metric|filter|hierarchy|transformation`). Each kind uses the verb the spec defines (PUT for metrics, facts and filters; PATCH otherwise) and refuses any other; schema-level kinds open a schemaEdit changeset.
- Create through `strategy_api.py`; the dry run prints the request and the changeset it will open:

```bash
API="python3 skills/strategy-platform/scripts/strategy_api.py"
$API call ms-createAttribute --schema-edit --body @attribute.json      # also ms-postFact, ms-postMetric, ms-postFilter, ms-postPrompt
$API call ms-postSecurityFilter --body @sf.json                         # classic project security filter
$API call updatePartialSecurityFilter_1 -p id=<sfId> --body '{"operationList":[{"op":"addElements","path":"/members","value":["<user or group id>"]}]}'
$API call getSecurityFilterUsers_1 -p id=<sfId>                        # read back the members
```

- Metrics, facts and prompts are replaced whole with PUT (`ms-putMetric`, `ms-putFact`, `ms-putPrompt`); attributes, tables and hierarchies take PATCH. Start from a current GET either way.
- No identity token on classic Modeling calls (`skills/strategy-automation/SKILL.md`, Operating Rules).
- The `strategy_api.py` lines here are spec-verified only.

## Datasources and connections

Build discovery is `list-datasources`, `list-namespaces` and `describe-tables`. Datasource administration — connections, logins, mappings, project assignment — goes through `strategy_api.py`; none of it has a recorded live run here yet:

```bash
$API call getDatasources
$API call getDatabaseConnections
$API call getDatasourceLogins
$API call getProjectDatasources -p id=<projectId>
$API call testDatabaseConnectionWithBody --body @conn.json --yes     # tests a connection (a POST, so it needs --yes)
```

- A login body holds a warehouse password: the human writes it, or use a `vaultSecret` login backed by a vault (`skills/strategy-admin/SKILL.md`, workflow 10). Keep `exportDatasourceLogins` output out of the repo.
- `getDBMSs`, `getDSNs` and `getDrivers` (`/api/dbobjects/...`) are deprecated.

## Invocation examples

Single source:

```bash
python3 scripts/build_mosaic.py build \
  --instance "Snowflake Prod" --schema SALES \
  --tables CUSTOMER ORDER LINEITEM \
  --name "Sales Mosaic"
```

Multi-source (forces `in_memory`; see `feedback_mosaic_multi_db_connect_live.md`):

```bash
python3 scripts/build_mosaic.py build \
  --source "Snowflake Prod:SALES:CUSTOMER,ORDER" \
  --source "Postgres Billing:FIN:INVOICE" \
  --name "Customer 360" \
  --data-serve-mode in_memory
```

## Subcommand reference

Full subcommand index lives in `memory/reference_mosaic_build_skill.md`. Start there when you don't know which subcommand to run.

## Auto-detected schema topology

The skill auto-classifies topology from column-name patterns. Override with `--dictionary` + `--erd` when needed.

- **Star** — one fact, many dims, no sub-dim chains.
- **Snowflake** — dim chains naturally emerge when a dim's PK appears in another dim and are wired as attribute relationships (no hierarchy object is created: the spec has no Mosaic hierarchy endpoint).
- **Galaxy / constellation** — conformed dim (a non-PK descriptor in ≥2 tables) is promoted to one multi-table attribute.
- **Noise columns** (skipped): `SOURCE_SYSTEM`, `LOAD_TIMESTAMP`, `LAST_UPDATED_AT`, `INGESTION_DATE`, `LOAD_DATE`, `ETL_BATCH_ID`, `DW_CREATED_AT`, `DW_UPDATED_AT` when present in 3+ tables.
- **Bridge / junction** (all-FK tables) — not auto-wired as `many_to_many`; dictionary/ERD must declare.
- **Non-Kimball (OBT / EAV / graph)** — stop and confirm with the user; reshape upstream.

## Behaviour worth knowing (2026-10-05 hardening)

- **ACL writes merge.** `--grant` / `--deny` add bits to a trustee's existing entry and keep its type; `--replace-trustee` sets the entry exactly. Granting and denying the same right in one command stops the run. A trustee new to the ACL needs `:user` or `:user_group` when the account can't read users and groups.
- **Security filters** resolve members before creating anything; a name that is both a user and a group, or an ID that doesn't exist, stops the run. A filter whose members couldn't be bound counts as a failure.
- **`build` order:** post-build specs are validated before the model is created; the summary (with `model_id`) always prints; publish runs only when tables, attributes, metrics and relationships succeeded; certify runs last and only on a clean build.
- **Opt-in only:** `--release-locks` (lock release, checked against the lock owner), `--use-batch` (UI-internal batch endpoint), `--unverified-ok` (create-transformation, attach-transformation), `--any-type` (delete-model on something that isn't a Mosaic model), `api-call --show-secrets`.
- **`refresh --refresh-type`** is honoured (`replace` default; `incremental` = upsert); any table status ending in `error` fails fast; the publish instance is kept if the network drops mid-publish.
- **`patch-model-object --kind metric`** is a derived metric (`/metrics`, PUT); fact metrics are `--kind fact_metric`.
- **Derived metrics** (`memory/reference_mosaic_derived_metrics.md` §0, §0c, §0d), one changeset each, `--dry-run` on all three: `create-compound-metric` (arithmetic over metrics as an expression tree; `--join outer` keeps groups where an operand is blank; `--replace` rewrites in place), `create-conditional-metric` (Sum/Avg/Count of a fact metric or `--count-attribute` distinct count, repeatable `--and` conditions, `--level-rule last|first --level-attribute <period>` for snapshot facts), `delete-derived-metric` (derived metrics only; refuses while other derived metrics refer to it unless `--force`), `create-level-metric` (once-per-entity amount on a periodic-snapshot fact, `Sum(Max([M]){~+,[Entity]+}){~+}`), `create-weighted-average` (value weighted by a once-per-entity amount: numerator and denominator helpers plus their outer-join ratio; `--value-attribute` or `--value-metric`).
- **`set-column-types`** changes column types in the model without reloading the source: the table's column list and its pipeline get the new type (`--set NAME TYPE [DATE_FORMAT]`, repeatable, or `--spec` CSV/JSON; NAME is a column, or an attribute or fact metric that reads one column, because the connector shows attribute names only; types `text`, `integer`, `bigint`, `double`, `date`, `timestamp`; dates need the source pattern, e.g. `yyyyMM`), then the attribute forms and fact metrics that read the column are lined up (the service usually does this itself; only differences are written). A lookup or mapping table joined through the same attribute (its key column read by the attribute's form) takes the same type, or the join would break; `--no-joined` turns that off. `--dry-run`, `--table` for a table other than the widest (names match loosely: case, spacing and a file extension don't matter). Republish for the data to change. Use it for codes stored as numbers, numbers stored as text and integer dates.
- **`create-formula-attribute`** adds an attribute computed from the table's columns (`--formula 'Year([FIRST PAYMENT DATE])' --data-type integer`; `--update` rewrites a single-form one). Prefer a lookup table: it is faster, and replacing a derived attribute later deletes the derived metrics built on it. Verified: functions, columns, integers; string literals and decimals need `--unverified-ok`. One changeset, `--dry-run`; republish afterwards.
- **`add-column-attribute`** adds an attribute that reads one existing column as-is (`--column` takes the column, or the fact metric or attribute that reads it), so a numeric column read as a fact metric (a code, a flag, a small count) can also be grouped and filtered on. The fact metric stays. It refuses a name that a metric already has. Same body as `create-formula-attribute` with a single column token; not yet run live. `--dry-run`; republish afterwards.
- **`add-time-attributes`** adds year, quarter and month (week, day) intervals to a date or timestamp attribute (`--attribute`, `--intervals`, or `--spec`), in one changeset; it re-sends existing intervals, since the request replaces the set. A number stored as a date is converted first with `set-column-types ... date yyyyMM`. On the connector each interval shows as `<attribute> (<interval>)__id / __start / __desc`. Verified live with a yyyyMM integer converted by `set-column-types ... date yyyyMM`. `--dry-run`; republish afterwards.
- **`show-forms`** makes hidden forms visible: it adds them (`--forms`, names or categories such as ID) to the attribute's report and browse displays, keeping the ones it has. The connector lists only displayed forms. A form already shown is left alone. `--dry-run`; republish and re-certify afterwards.
- **`rename-describe`** renames attributes and fact metrics and/or sets their descriptions by name (`--spec` JSON `{items: [{object_type, current_name, new_name, description}]}`), all in one changeset, no republish. Descriptions are capped at 250 characters; an item already applied is skipped, so a rerun is harmless. A connector form name (`<attribute>__<form>`, `<attribute> (<form>)`) describes its attribute (forms share its description) and can't be renamed. Every item is checked before anything is written, and all problems are reported together.
- Write failures exit non-zero in merge-attributes, wire-relationships, translate, add-security-filter and delete-model.

## Path drift — when an endpoint 404s

Strategy REST paths drift between versions. If a call 404s:

1. `python3 skills/strategy-platform/scripts/strategy_api.py sync`, then `ops --search "<term>" --internal` and `describe <operationId>` — the tenant's own spec, public and internal operations.
2. Older fallback: `openapi-summary` / `openapi-search "<term>"` against `{Library}/api/openapi.yaml?visibility=all`.
3. `discover` for live catalog variants (`/api/datasources` vs `/api/dbobjects/databaseInstances`, `/catalog/tables` vs `/tables` vs `/namespaces/{ns}/tables`).
4. Update constants at the top of the helper script; update the corresponding memory file with the tenant-specific finding.

## Failure modes to watch

- **Silent under-joined model** (no error, `forms[].expressions` doesn't span all expected tables) — `feedback_mosaic_relationship_wiring.md`.
- **Session cap trip** (`8004cb0a`, iServerCode `-2147072486`) mid-pipeline — `feedback_build_mosaic_session_leak.md`.
- **Publish stall** (iServerCode `-2147212544` or `-2147072194`) — `reference_mosaic_publish_path.md` ("DataType preconditions" and "Never fire both publish endpoints" respectively).
- **Duplicate attribute name** (`8004e409`) — conformed dim not declared; see wiring recipe.
- **Form PATCH failure** (`8004cc63`, `8004cd0a`) — fix form names at CREATE time, not post-hoc PATCH.
- **Description too long** (`8004cc10`) — keep under ~250 chars (`feedback_mosaic_ship_bar.md`).

Every one of these has a row in `memory/reference_strategy_error_codes.md`. Grep there before retrying.
