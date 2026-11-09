---
name: Strategy REST error-code index
description: Flat lookup of every Strategy / Mosaic error code observed in this repo — 8004cc## / 8004cd## / 8004cf## / 8004e4## codes plus signed iServerCodes — keyed to the specific memory file that teaches the fix. Grep this file first when a 4xx/5xx response surfaces before grepping anywhere else.
type: reference
tags: [error-code, mosaic, classic, publish, build, session-management, kimball]
---

## How to use this file

When a Strategy REST call returns 4xx or 5xx, you'll see a `code` field (e.g., `8004ccdb`) and often an `iServerCode` (e.g., `-2147072486`). Grep this file for the code — each row points to the memory file with the root cause and fix. This is the fastest path from symptom to resolution.

**Do not retry blind on any of these codes.** All are class-of-error conditions (structural / resource / permission), not transient network issues. Retry without diagnosis burns session cap.

## Code index — Mosaic / Modeling Service

| Code | Symptom | Root cause / Kimball lens | Fix file |
| --- | --- | --- | --- |
| `8004cb04` | "Intelligence Server Gateway Error: Missing attribute v in element  tkn" on `/metrics` POST/PUT | A token in `expression.tokens` lacks its `value` key — `end_of_text` must be `{"type":"end_of_text","value":""}` (capture docs show it abbreviated without value). | `reference_mosaic_derived_metrics.md` |
| `8004cd15` (code `-2147214845`) | "Validation Error happens for the expression in the fact … Searching for 'Sign' resulted in no matches" on a formula attribute POST | The formula names a function the tenant's function library doesn't have; the tenant looks each function up by name when the request is applied (a local dry run can't catch it). `Sign` is not available (2026-11-09). | `reference_mosaic_rest_gotchas.md` (Formula attributes) |
| `8004cb0a` (iServerCode `-2147072486`) | "Maximum number of interactive session per user for project exceeded" | iServer project-interactive session cap. Root cause in this repo until 2026-10-05: scripts logged out with `DELETE /api/auth/login`, a 404, so every run held its session ~30 min. Logout is `POST /api/auth/logout`. Stop retrying; `kill-sessions` (monitoring privilege) or wait for the idle timer; keep chains in one session (`MSTR_REUSE_SESSION=1`). | `feedback_build_mosaic_session_leak.md` |
| `8004cb09` | "You do not have … privilege(s) to perform the task" on a Modeling write you own | Either the identity token was minted before `X-MSTR-ProjectID` was set (project-less token), or the privilege is project-level-only and any identity token downgrades the check. Retry with the identity token OFF. | `feedback_mosaic_identity_token_privilege_downgrade.md`, `reference_strategy_authentication.md` |
| `8004c768` | "Wrong projectId" on Modeling GETs | `X-MSTR-IdentityToken` held session-wide overrides the project; send it only on changeset writes. | `reference_mosaic_fact_metric_aggregation.md` |
| `8004e403` | Model-root ACL PATCH with `subType=logical_table` fails | The model root's subtype is `report_emma_cube`. | `reference_mosaic_acl.md` |
| `ERR009` (401) | "The user's session has expired, please reauthenticate" | No session or an expired one; also the answer to any `Authorization: Bearer` token on REST (REST does not take OAuth bearer tokens). Sign in again (`strategy_auth.py`). | `reference_strategy_authentication.md` |
| `ERR001` "OIDC not available" | `GET /api/config/oidc/native` | The tenant has no OIDC login mode (4194304); use the browser `sso` method. | `reference_strategy_authentication.md` |
| HTTP 404 on `DELETE /api/auth/login` | logout silently did nothing | Not an operation in the REST API; logout is `POST /api/auth/logout`. | `feedback_build_mosaic_session_leak.md` |
| `8004cc10` | "Object Description …" — PATCH with >250-char description rejected | Mosaic data-model description has an undocumented ~250-char cap. Truncate; detail goes in external docs. | `feedback_mosaic_ship_bar.md` |
| `8004cc63` | "Attribute does not contain a form with the ID" on PATCH (HTTP 404) | Something else on the attribute still references a form id absent from the PATCHed `forms[]` — usually `displays`. Removing a form requires `forms` AND corrected `displays` in ONE atomic PATCH; forms-then-displays fails on the first call. For form NAMES, still fix at CREATE time. | `feedback_mosaic_ship_bar.md` |
| `8004cc7c` *(if seen)* / `8004c767` | "attribute_form_custom … not found in metadata" for predicate_form_qualification on a CUSTOM DESC form | Use `predicate_element_list` (Shape B) for SF qualifications on custom forms. | `reference_mosaic_security_filter.md` |
| `8004ccc7` | "Table cannot be used as the join table for a relationship involving attribute" | `relationship_table` does not contain an expression of BOTH parent AND child attribute. Kimball: the fact row must physically connect both conformed dims. Pre-flight with `wire-relationships --dry-run` (validates the relationship_table prerequisite inline before any PUT). | `feedback_mosaic_relationship_wiring.md` |
| `8004ccdb` | "Attribute appears in a relationship more than once" | Self-reference — parent and child resolve to the same conformed attribute object id. Kimball: you don't declare a relationship between two columns of the same conformed dim; the dim IS the join. | `feedback_mosaic_relationship_wiring.md` |
| `8004ccde` | "The tree or token is required for expression" on attribute PATCH | A form expression was sent with only `text` (read-only on GET). Writes require `tokens` or `tree`. Use `mosaic_safety.make_expression()` or `normalize_expressions()`. | `reference_mosaic_safety_helpers.md` |
| `8004ccfc` | "Duplicate model name in folder" | Old model still present. Delete via `DELETE /api/objects/{oldMID}?type=3`. | `reference_strategy_object_cloning.md` |
| `8004cd0a` | "form property requires a non-empty name" | During UPDATE every form in `forms[]` must have a non-empty name. At CREATE, name can be omitted. | `feedback_mosaic_ship_bar.md` |
| `8004cd15` | "Object (of type: Attribute) not allowed in this place" — PATCH refs auto-generated managed attribute IDs | Naive expression-append PATCH resolves column-reference tokens against MSTR's managed-attribute objects. Create multi-table expressions on the FIRST POST (entity-first); post-hoc, use the GET-first clone-and-remap PATCH. | `feedback_mosaic_gotchas.md` + `feedback_mosaic_relationship_wiring.md` |
| `8004cd15` (variant) | "Validation Error … Invalid Expression" on `/metrics` tokens | A bare object_reference followed by a level cluster with no function wrapper — the token grammar requires an aggregate function around the operand (`Sum(...)`, `Count(...)`). | `reference_mosaic_derived_metrics.md` |
| `8004cf06` | "attribute has no report display" at commit | `displays.reportDisplays` not set. Applies to EVERY attribute-create path (fresh build, clone, canary scripts) — PATCH `displays` after each attribute POST, before commit. Verified on a fresh 1-attr model 2026-06-11. | `reference_strategy_object_cloning.md` |
| `8004d232` | "The table change will make connect-live Mosaic model invalid" | Multi-DB (≥2 databaseInstance.objectId) under `dataServeMode: connect_live`. Use `in_memory` instead. | `feedback_mosaic_multi_db_connect_live.md` |
| `8004d711` | "When metric expression is empty or not object reference to (aggMetric), dimty should be null" on `/metrics` | Tree format cannot carry dimty: operator roots AND bare tree references both reject dimty units. Level metrics must be written as TOKENS with attribute-only dimty; compounds as tree with `dimty: null`. | `reference_mosaic_derived_metrics.md` |
| `8004d716` | "Invalid parameter value normal at field aggregation for DimtyUnit" on `/factMetrics` | Fact-metric attribute dimtyUnits accept only semi-additive enums (`DssAggregationFirst/LastInFact/InRelationship`) — they are NOT level pins. Level pinning lives on `/metrics` only. | `reference_mosaic_derived_metrics.md` |
| `8004e409` | "Duplicate attribute name" | Conformed dim not declared — same logical entity created as N separate attributes. Kimball: promote to one conformed attribute with multi-table expressions. | `feedback_mosaic_relationship_wiring.md` |
| `8004e42f` | "Table has no attribute/metric" at commit | A table was created without any attribute or metric bound to it. Commit rejects. | `reference_strategy_object_cloning.md` |
| `8004e457` | "Given object is not a Mosaic model" | Object is not a Mosaic model (subtype 779 + extType 448; data-import cubes share subtype 779). Call `GET /api/objects/{id}?type=3` and check `subtype` + `extType` before any `/api/model/dataModels/*` call. | `reference_mosaic_vs_legacy_surfaces.md` |
| iServerCode `-2147072194` | 500 "Cube report … is being published by job N" on `GET /api/dataModels/{id}/publishStatus` | Two publish endpoints fired concurrently — `/api/cubes/{id}?cubeAction=publish` AND `/api/dataModels/{id}/publish`. The losing instance's `publishStatus` locks out for the full job. | `reference_mosaic_publish_path.md` |
| publishStatus `status=-2147418587` | In-memory publish reaches a terminal negative status after some tables report `completed`; cube execute probe still returns iServerCode `-2147072488` | CubeServer materialization failed after partial table load. Capture the per-table status before the data-model instance expires, then inspect publish prerequisites: clean physical-table dataTypes, large fact-table feasibility, warehouse resume/availability, and CubeServer health. | `reference_mosaic_publish_path.md` |
| iServerCode `-2147212544` | CubeServer parallel-mode stall on in-memory publish | Physical-table `dataType` values carry warehouse-catalog sentinels (`variable_length_string`, `fixed_length_string`, `precision=-1`, etc.). Clone UI-verified types. | `reference_mosaic_publish_path.md` |
| iServerCode `-2147209151` (404) | `/api/projects/{id}` reports project not loaded | Project is unloaded. Probe `/api/projects` + `/api/projects/{id}` before use. | `reference_strategy_project_loading.md` |
| iServerCode `-2147072488` | 500 "Intelligent Cube … for locale … is not published" on `POST /api/v2/cubes/{id}/instances` | The cube has not materialized — this is the DEFINITIVE negative publish probe (a 202/204 on the publish trigger proves nothing). Execute-probe after every publish; if this code appears, the publish job died or is still queued. | `reference_mosaic_publish_path.md` |

## Code index — classic / project semantic layer + ACL

| Code | Symptom | Root cause | Fix file |
| --- | --- | --- | --- |
| `8004c738` | "User does not have Control access" on `GET /api/model/dataModels/{id}/securityFilters` | Session user is not the owner. Only the SF owner can list per-model SFs. Expected response when sweeping across a tenant — filter these out of inventory. | `reference_strategy_mosaic_field_study.md` |
| `8004c908` | "Invalid value for field 'type': 'normal'" creating an attribute form | Form `type` enum only accepts `"system"` for inline forms | `reference_strategy_legacy_semantic_admin.md` |
| `8004cb04` | Compound metric create: referenced metric "does not exist in the object context" | Classic compounds cannot reference metrics created in the SAME uncommitted changeset — commit bases first, reference in a new changeset | `reference_strategy_legacy_semantic_admin.md` |
| `8004cc41` | "Schema editing is in use by another user" opening a schemaEdit changeset | A committed schema changeset's lock lingers past logout; `DELETE /api/model/schema/lock` (works when the lock owner is your account) | `reference_strategy_legacy_semantic_admin.md` |
| `8004cb15` | "The changeset/instance does not belong to the user" on changeset DELETE | Changesets are session-scoped; from a new session release the schema lock instead of deleting the old changeset | `reference_strategy_legacy_semantic_admin.md` |
| `8004d711` | "dimty should be null" / "conditionality should be null" on metric PUT | Only aggMetric-reference metrics may carry `dimty`/`conditionality` — pop BOTH from the echoed GET body before writing a compound expression | `reference_strategy_legacy_semantic_admin.md` |
| `8004d713` / `8004d714` | "should not add Aggregation into metricSubtotals" / "implementation of Total could only be Total" | `aggregateFromBase`/`subtotalFromBase: true` conflict with subtotal implementations — set both false in the same PUT | `reference_strategy_legacy_semantic_admin.md` |
| `-2147212797` | Cube publish/report load: "Dimensional Metric … Loading is interrupted by invalid data" | Metric was created as a flat function tree; classic simple metrics need the parser-built embedded agg_metric — write the expression as a raw-text token instead | `reference_strategy_legacy_semantic_admin.md` |
| `-2147205488` | Cube publish: "Maximum number of results rows per report exceeded … 32000" | Project governors `maxCubeResultRowCount` / `maxReportResultRowCount` / `maxInternalResultRowCount`; raise via `PATCH /api/v2/projects/{id}/settings` (PUT demands the full settings map) | `reference_strategy_legacy_semantic_admin.md` |
| `-2147072488` | Cube "not published" on execute despite publish 202 | Publish job died silently (governors) or was CANCELED — publish jobs are session-bound and logout kills them; read the real error via `GET /api/v2/cubes/{id}/instances/{instanceId}` | `reference_strategy_legacy_semantic_admin.md` |
| `8004c90a` | "Unrecognized field: standaloneFilter" on `POST /api/model/reports` | The OpenAPI-documented standalone-filter shape is not accepted by the observed build — reference a filter object via `predicate_filter_qualification` (`predicateTree.filter` + `isIndependent: 0`) instead | `reference_strategy_legacy_semantic_admin.md` |
| `8004c901` (variant) | "Json input missing the following field: information" on `PUT /api/model/filters/{id}` | Modeling PUTs are full-replace — echo the GET body's `information` block alongside the new `qualification` | `reference_strategy_legacy_semantic_admin.md` |
| `8004da0c` | "Managed object cannot be added to normal report template" on `POST /api/model/reports` | An attribute resolved via search was a managed/template attribute (search returns them with empty ancestors, e.g. same-named "Quarter"/"Month"). Prompt/filter creates accept them silently; the report validator is what catches it. Resolve real schema attributes via `/api/model/systemHierarchy` or an existing report's `dataTemplate.units`, then repair prompts with a full-replace PUT | `reference_strategy_legacy_semantic_admin.md` |
| `ERR001` (generic) | Generic platform error wrapper | Inspect `iServerCode` to classify. Not actionable on its own. | — |

## Symptom index (when you don't know the code)

| Symptom | Likely code | Fix file |
| --- | --- | --- |
| "Model has no joins" in the Mosaic UI despite shared columns | — (no error, silent fail) | `feedback_mosaic_relationship_wiring.md` |
| Previously-wired relationships vanish after a later wiring run (every PUT returned 200, exit 0) | — (silent; relationship PUT is full-replace in BOTH directions) | `reference_mosaic_rest_gotchas.md` |
| Trino query returns zero rows across a cross-table group-by | — (silent) | `feedback_mosaic_relationship_wiring.md` |
| Metric totals balloon by a factor of N vs source | — (silent Cartesian) | `feedback_mosaic_relationship_wiring.md` |
| Publish status stuck at 500 for minutes, cube actually materialized | `-2147072194` | `reference_mosaic_publish_path.md` |
| Publish terminal status after partial table completion, cube still unqueryable | `-2147418587` | `reference_mosaic_publish_path.md` |
| Publish silently never produces a queryable cube | `-2147212544` | `reference_mosaic_publish_path.md` |
| Changeset commit fails with no clear reason after many calls | `8004cb0a` | `feedback_build_mosaic_session_leak.md` |
| Conformed dim duplicated across tables (`Customer`, `Customer (Orders)`, `Customer (Shipments)`) | `8004e409` OR silent conformance skip | `feedback_mosaic_relationship_wiring.md` |
| Form name shows as "R Regionkey ID" or "None" in UI | — (quality issue) | `feedback_mosaic_ship_bar.md` |
| Classic cube publish 202 but `HEAD /api/cubes/{id}` X-MSTR-CubeStatus stays 0, no error anywhere | — (job died: see `-2147205488`, or session logout canceled it) | `reference_strategy_legacy_semantic_admin.md` |
| Classic cube REpublish reports success in seconds but data stays stale/wrong | — (status header reflects the OLD cache; only an execute probe tells the truth) | `reference_strategy_legacy_semantic_admin.md` |
| Dossier/grid on a classic cube shows `--` for Avg/StDev metrics above cube grain | — (dynamic aggregation defaults to none; set the Aggregation-subtotal implementation) | `reference_strategy_legacy_semantic_admin.md` |
| Multi-pass temp-table cube SQL runs 10+ min on pooled Postgres (rows are tiny) | — (un-analyzed temp tables → nested-loop plans; set VLDB Intermediate Table Type = Derived table) | `reference_strategy_legacy_semantic_admin.md` |
| Workstation prompt editor: "The filter … contains prompts. Filtered element browsing is not supported with a filter that contains prompts." | — (editor-preview limitation only — nested/cascading prompts still run fine; author via placeholder-filter swap or REST) | `reference_strategy_legacy_semantic_admin.md` |

## Housekeeping

- Every new durable feedback file that teaches a fix for a new code MUST add a row here. This is the index-of-record.
- Do not delete rows. If a code is no longer reproducible on a newer iServer build, annotate with `(verified fixed in iServer <version>)` — future operators on older tenants still need the note.
- Kimball lens: many of these codes surface when the model violates a conformed-dim or star-schema invariant (`8004ccc7`, `8004ccdb`, `8004e409`). When you see one, ask "what would Kimball do here?" before reaching for a REST workaround.
