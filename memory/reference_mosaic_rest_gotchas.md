---
name: Mosaic REST API payload gotchas
description: Exact payload shapes and ordering rules for creating Mosaic models / tables / attributes / metrics / relationships via REST — pipeline table shape, column-objectId ephemerality, EOT + character operator tokens, and the destructive relationship PUT (use put_relationships_merged()). Things the OpenAPI spec does not make obvious.
type: reference
---

Verified against a Strategy ONE Library Modeling Service in 2026. These rules came out of a real build of a product-hierarchy model on a Snowflake-backed datasource; the shapes are tenant-agnostic.

## Model creation
- `POST /api/model/dataModels` body **must** use `"subType":"report_emma_cube"` in `information`. `logical_table` is rejected with `Invalid subtype DssSubTypeTable. Expected subtype: DssSubTypeReportEmmaCube`.
- `destinationFolderId` is required; the caller must have Write access. `Subject Areas` is read-only for non-admins — fall back to `My Objects` (`GET /api/folders/myPersonalObjects` → `"My Objects"`).
- Model-scoped default folder IDs vary per tenant. Never hardcode. Always resolve from `/api/folders/preDefined/{type}` or `/api/folders/myPersonalObjects` for the current session's project.

## Physical tables
- `POST /api/model/dataModels/{id}/tables`: the accepted shape on current Library servers (including Strategy ONE Cloud tenants, where the docs' alternative fails) is `physicalTable.type: "pipeline"` with a `pipeline` field containing a **stringified JSON** describing `rootTable → source → importSource`. `warehouse_partition_table` (mentioned in the MSTR Modeling docs) is rejected with 400 `Invalid value for field 'type'`. A bare `normal` type rejects the `databaseInstance` field (`Unrecognized field: databaseInstance`).
- The pipeline JSON shape that works (see `_make_pipeline_table_body()`, used by `cmd_build`, in `skills/build-mosaic-model/scripts/build_mosaic.py`) — generate **fresh UUIDs for every inner id** on each build:
  ```json
  {
    "id": "<uuid>",
    "rootTable": {
      "id": "<uuid>", "type": "root",
      "children": [{
        "id": "<uuid>", "name": "<TABLE>", "type": "source",
        "columns": [{ "id":"<uuid>", "name":"COL", "dataType":{...}, "sourceDataType":{...} }, ...],
        "importSource": { "type":"single_table", "dataSourceId":"<dbInstanceId>", "namespace":"<schema>", "tableName":"<TABLE>", "sql":"" }
      }]
    }
  }
  ```
- The outer `physicalTable.columns` list is a separate flat array with `{information:{name}, dataType, columnName}` — **no top-level `id`** — while the inner pipeline `rootTable.children[].columns` uses `{id, name, dataType, sourceDataType}`. Both the outer and the inner pipeline `columns` must be present.
- When the pipeline shape misbehaves on a tenant, the fallback is the object form via clone-and-remap — see `reference_strategy_object_cloning.md`.

## Changing a column's type in an existing model (verified 2026-10-30)
`PATCH /api/model/dataModels/{mid}/tables/{tid}` with `{"physicalTable": ...}`, inside a changeset: set the new
`dataType` (and `format`, e.g. `yyyyMM`, for a date read from integers) on the column in BOTH
`physicalTable.columns` and the pipeline's `rootTable.children[].columns`; leave `sourceDataType` as it is.
Trim every `information` block to `objectId`, `subType`, `name` before sending. The source is not reloaded:
the new type applies at the next republish. In the verified run (13 columns: integer → text, integer →
date, text → double) the service also updated the attribute forms and fact metrics that read those columns,
so a second pass to align them found nothing to write. `build_mosaic.py set-column-types` does both steps.

## Renames and descriptions (verified 2026-10-30/31)
`PATCH /api/model/dataModels/{mid}/attributes/{id}` or `.../factMetrics/{id}` with `{"information": {"name",
"description"}}` inside a changeset renames and/or describes; it takes effect on commit, with no republish.
Descriptions over 250 characters are refused. Derived metrics need a full-body PUT instead.
`build_mosaic.py rename-describe` sends this shape.
- A form has no description of its own: the connector shows the attribute's description on every form
  (`<attribute>__<form>`, `<attribute> (<form>)`). Describe the attribute once, covering its forms.
  `rename-describe` maps a form name to its attribute for a description and refuses to rename one.
- Not yet confirmed: whether the connector's `get_semantics` shows a new description before the next
  republish. Check it after applying; republish if the old text still shows, and record the result here.

## Formula attributes (verified 2026-10-30/31)
`POST /api/model/dataModels/{mid}/attributes` in a changeset, one ID form (system form id
`45C11FA478E745FEA08D781CEA190FE5`, `category: ID`) whose expression is tokens, never text: `function`,
`character` (operators, parentheses, commas), `column_reference` with the column's objectId, `integer`,
then `end_of_text`. Include `keyForm`, `attributeLookupTable` and report/browse `displays`, or the commit
is refused. The server infers the form's type from the column inside the formula, so `Year([a date])`
comes back as a date unless `dataType` is set to integer. Confirmed: `AddMonths([PERIOD], -1)`,
`Year([date])`, `MonthsBetween([a], [b])`. String literals and decimals are unconfirmed. `Sign` does not exist
(refused with `8004cd15`, "Searching for 'Sign' resulted in no matches", 2026-11-09): the tenant resolves each
function by name when the request is applied, inside the changeset, which is discarded on refusal.
`Case([<an amount column>] > 0, 1, 0)` typed integer was accepted (2026-11-09) and, after the republish,
flags exactly the rows with a positive amount (rows flagged 1 = rows with the amount > 0): `Case` and
the `>` comparison work in formula attributes.
`build_mosaic.py create-formula-attribute` sends this shape.

## Column objectIds (the #1 trap)
- After `POST .../tables`, column objectIds are **visible inside the changeset** but are **ephemeral until commit**. A subsequent attribute/metric that references those IDs in the same uncommitted changeset will fail at commit with `Object with ID '...' and type 26 (Column) is not found in metadata`.
- Safe ordering: **commit tables first**, then re-`GET /api/model/dataModels/{mid}/tables/{tid}?showColumns=true` to capture the post-commit column objectIds, then open a new changeset for attributes/metrics.
- Exception to ordering: Mosaic will reject commit of a changeset that contains only tables with `Mosaic model cannot be saved because it is empty` and `table doesn't have any metric or attribute`. Workaround: include at least one attribute **or** fact metric referencing each table in the same changeset that creates the table. The simplest version is a single throwaway attribute or a per-table fact metric per new table. Tables committed by the auto-builder script (`build_mosaic.py build`) satisfy this automatically because attributes/metrics are created in the same changeset.

## Attributes (multi-form)
- `POST /api/model/dataModels/{id}/attributes` body shape:
  ```json
  {
    "information": {"name","description","subType":"attribute"},
    "forms": [
      { "id":"45C11FA478E745FEA08D781CEA190FE5", "category":"ID", "type":"system", "displayFormat":"number",
        "dataType": {...},
        "expressions": [{"expression":{"text","tokens":[<column_reference>, <eot>]}, "tables":[<logical_table refs>]}],
        "alias":"COL", "lookupTable": {...} },
      ... custom forms ...
    ],
    "keyForm": {"id":"45C11FA478E745FEA08D781CEA190FE5"},
    "attributeLookupTable": {"objectId","subType":"logical_table","name"},
    "displays": {"reportDisplays":[{"id","name"}], "browseDisplays":[...]}
  }
  ```
- The ID form's constant `objectId` `45C11FA478E745FEA08D781CEA190FE5` is a platform-wide key form id (safe to reuse across tenants).
- An ID form whose expression tables include multiple logical tables (`LU_CATEGORY` AND `LU_SUBCATEG`) is **how you tell Mosaic two tables share that attribute**. Without this, there is no join path and queries cartesian.
- **Multi-table ID expressions — the non-obvious shape.** In legacy semantic-layer JSON, a single `expressions[0]` entry lists all tables in `tables[]` and its tokens reference one project-level column objectId. **This does NOT work in Mosaic.** Mosaic validates column objectIds per physical table and each table has its own. Submit **one expression entry per `(table, column)` pair** inside the `expressions[]` array — each with its own `tables:[one_table]` and its own tokens pointing at that table's column objectId. Without this split, you get either `Table X doesn't have the columns: Y` at attribute creation, or `Table cannot be used as the join table for a relationship involving attribute` at relationship creation.
- For relationships: the parent attribute's `expressions[]` must include the `relationshipTable` — i.e., `Category → Subcategory via LU_SUBCATEG` requires Category to have an expression entry with `tables:[LU_SUBCATEG]` referencing that table's `CATEGORY_ID` column object.
- The `expression.tokens` array must end with `{"type":"end_of_text","value":""}` or Modeling Service rejects it with `The tree or token is required for expression` (a misleading error — the real issue is missing EOT). *(Caveat 2026-06-11: simple single-token `column_reference` expressions commit fine WITHOUT the EOT on a Strategy ONE Cloud tenant — `cmd_build` and captured build drivers ship that shape. Treat EOT as required for formula/multi-token expressions, optional-but-harmless for single-column ones.)*
- **Every attribute needs `displays` set before COMMIT** — `8004cf06 "Attribute … cannot be saved because it has no report display"` fires at changeset commit otherwise. Either include `displays` in the create body or PATCH `{"displays":{"reportDisplays":[…],"browseDisplays":[…]}}` right after each create. Applies to fresh builds, clones, and 1-off canary scripts alike (verified 2026-06-11).
- **Adding forms to an EXISTING attribute (verified 2026-09-11, Strategy ONE Cloud):** `PATCH …/attributes/{id}` with the full `forms` list works, but (a) new forms need `expression.tokens` (`column_reference` with the table's column objectId + `end_of_text`) — text-only → `8004ccde`; (b) the service **reassigns every custom form's `id` on each forms PATCH**, so `displays` sent in the same body that reference client-side ids (or even the previous custom-form id) fail with `8004cc63`. Recipe: PATCH forms with `displays` parked on the stable ID form `45C11FA478E745FEA08D781CEA190FE5`, re-GET the attribute, then PATCH `displays` using the freshly assigned ids (look forms up by `name`). Clearing an AI-detected geo role (`geographicalRole: city/country`, `semanticRole: geo` on the form — it is what puts `__latitude/__longitude` pseudo-columns on the SQL surface) needs explicit `"geographicalRole":"none","semanticRole":"none"`; omitting the keys leaves it unchanged. Deleting an attribute (`DELETE …/attributes/{id}`, schemaEdit changeset) works once no relationship references it — PUT the retired attribute's relationships as `[]` in the prior changeset.
- **Custom form categories work at CREATE.** `category` values beyond ID/DESC (`CODE`, `LONGDESC`, `FIRSTNAME`) were accepted by `POST …/attributes` with `type:"system"` forms (verified 2026-06-11, Strategy ONE Cloud tenant) — multi-form attributes with DESC-first displays can be fully shaped at create time; no fragile post-hoc form PATCH needed.

## Fact metrics — function support varies by tenant
- `function:"count_distinct"` on `POST …/factMetrics` returned a bare 500 on a Strategy ONE Cloud tenant (2026-06-11) while `count`, `sum`, `avg`, `max` all work. When a distinct count is required and the column is unique in its lookup table, plain `count` is equivalent; otherwise check the tenant before promising distinct-count metrics.

## Hierarchies endpoint absent on some Library builds
- `POST /api/model/dataModels/{id}/hierarchies` AND `…/userHierarchies` → 404 `8004cc04` on a Strategy ONE Cloud tenant (2026-06-11). Auto-hierarchy drill objects cannot be created via REST there; relationships alone still drive drill. `cmd_build` logs a WARN and continues — treat the WARN as expected on this build family.

## Fact metrics (custom formulas)
- `POST /api/model/dataModels/{id}/factMetrics` with `text`-only expression is rejected. Provide tokens:
  ```json
  "expressions": [{"expression":{
     "text":"QTY_SOLD * (UNIT_PRICE - DISCOUNT)",
     "tokens":[
       {"type":"column_reference","value":"QTY_SOLD","target":{"objectId":"<col id>","subType":"column","name":"QTY_SOLD"}},
       {"type":"character","value":"*"},
       {"type":"character","value":"("},
       {"type":"column_reference","value":"UNIT_PRICE","target":{"objectId":"<col id>","subType":"column","name":"UNIT_PRICE"}},
       {"type":"character","value":"-"},
       {"type":"column_reference","value":"DISCOUNT","target":{"objectId":"<col id>","subType":"column","name":"DISCOUNT"}},
       {"type":"character","value":")"},
       {"type":"end_of_text","value":""}
     ]
  }, "tables":[{"objectId":"<od logical table id>","subType":"logical_table","name":"ORDER_DETAIL"}]}]
  ```
- Operators (`* + - / ( )`) are `character` tokens — NOT `operator`, `metric_reference`, or `object_reference`, all of which were tried and rejected. This is what makes a "derived calculation" (fact metric with an inline formula) finally commit. Functions (`Sum`, `Concat`, `ApplySimple`) are `function` tokens with `target.subType:"function"` and the well-known function objectId. Use `showExpressionAs=tokens` on a working metric to see the exact shape before inventing one.

## Relationships
- `PUT /api/model/dataModels/{id}/attributes/{childId}/relationships?changesetId=<cs>` (or via header `X-MSTR-MS-Changeset`). Body is the full list, not a delta.
- Must be a separate changeset from attribute creation. Attributes need to be committed first; otherwise the relationship references objects the server hasn't materialized yet.
- `relationshipTable` is the fact/bridge table where the join actually occurs (for 1:M, the child's lookup table; for M:M, the bridge table).
- **PUT is a wholesale REPLACE of the attribute's entire relationship inventory — in BOTH directions.** Strategy does NOT diff against the existing set, does NOT append, and does NOT respect direction: if you PUT attribute A with only its outgoing relationships, every incoming relationship that previously pointed AT A is silently deleted. Every call returns `200` and the script exits 0, so the wipe is invisible. This is the single most common silent failure in Mosaic relationship wiring. Two observed bites:
  - Multiple PUTs against the same child attribute within one changeset (e.g., once per parent): only the last PUT survives. Observed on an Item attribute with 5 parents — all 5 calls returned 200 but only Warranty→Item persisted.
  - Re-PUT in a later wiring pass: on a TPC-DS Galaxy build, Level-B relationships wired successfully, then Level-A wiring re-PUT the same shared FK attributes without the previously-written Level-B rels — the entire Level-B set was wiped with no error and no log line.
- **The fix: use `put_relationships_merged()` in `skills/build-mosaic-model/scripts/build_mosaic.py`, never a raw PUT.** It (1) GETs the attribute's current relationships, (2) dedupes by `(parent_objectId, child_objectId, relationship_table_objectId)`, (3) PUTs the union — both old + new. `cmd_wire_relationships` groups plan rows by child attribute and uses the merge helper by default; the destructive full-replace mode is opt-in via `--replace`. (Older guidance to hand-build a `per_child: dict[childId, list[relationship_body]]` map and issue one consolidated PUT per child predates the merge helper and is superseded by it — consolidation alone does not protect incoming rels or later passes.)
- **`--replace` is correct only for:** (a) cleanup — wiping a known-bad relationship graph to rebuild from a fresh hints file; (b) migration — intentionally replacing the legacy relationship set with a new conformed-dim layout, with a `--before-out` snapshot already saved. Anywhere else, the merge default is correct.
- **Pre-flight:** if a wiring run might touch previously-wired attributes, dump their current relationships first: `build_mosaic.py get-model-object --kind attribute --model-id <id> --object-id <attr-id> --out before/<attr-id>.json`.
- **Detection:** after wiring, run `build_mosaic.py validate-topology --model-id <id> --strict`. Non-zero exit means isolated attributes — exactly what a silent wipe produces.
- Related code: `8004ccdb` (relationship self-reference — parent and child resolve to the same conformed attribute object id). Not caused by the wipe, but it often appears alongside it in cleanup scripts; see `feedback_mosaic_relationship_wiring.md`.
- **The UI does NOT use `PUT /attributes/{id}/relationships`.** The Studio UI routes all relationship writes through `POST /api/model/batch` (see `reference_mosaic_batch_api.md`), which has per-relationship add/remove sub-ops and avoids the wholesale-replace trap entirely. Our helpers should migrate to batch; until they do, `put_relationships_merged()` is the standing mitigation for the per-object write path.

## Login modes per tenant (observed)
| Tenant | Working modes | Notes |
|---|---|---|
| Example SAML-backed demo tenant | 16 (LDAP — the old label said SAML; SAML is 1048576, browser only → `MSTR_AUTH_METHOD=sso`) | Modes 1 and 8 rejected |
| Example standard-auth tenant | 1 (standard) | Mode 8 (Anonymous) returns `INVALID_AUTH_MODE` |

Always tell the helper how to sign in per tenant: `MSTR_AUTH_METHOD` (`sso` for SAML/OIDC tenants), with `MSTR_LOGIN_MODE` only choosing 1 (Standard) or 16 (LDAP) for password sign-in. Different users on the same tenant may have different SSO configs — test before assuming. The initial `/api/auth/login` call succeeds even when the user has no access to the target project, so a successful login is not proof of a working project session. Always validate by calling a project-scoped endpoint.

## DB instance + schema discovery
- `GET /api/datasources` (helper `list-datasources`) is **project-agnostic**: it returns every datasource visible to the user regardless of the `X-MSTR-ProjectID` header, not the project's attachments. Filter client-side if the user only wants datasources attached to a specific project.
- `list-namespaces --instance-id X` can return 500 `Database error connection. Please try again` when the instance is orphaned or its credentials have rotated. Surface the full error; do not silently retry.
- Warehouse schema names must be passed exactly as the datasource returns them (often uppercase). Trino federation schema naming is different from the MSTR project name — Trino schemas are typically lowercased with spaces preserved and require double-quoting in SQL (e.g., a project named `Shared Studio` becomes `"shared studio"` in Trino).

## v2 cube execute (runtime probe + validation surface)
- `POST /api/v2/cubes/{modelId}/instances?limit=N` body `{}` executes a published Mosaic cube (subtype 779 + extType 448) and is the definitive publish-completion probe (200 = materialized; 500 `-2147072488` = not published). Async: poll `GET …/instances/{instanceId}` while `status==1`.
- `requestedObjects` aggregates ONLY when ≥1 attribute is requested: `{"requestedObjects":{"attributes":[{"id":…}],"metrics":[{"id":…}]}}` returns one row per attribute element (rollup view — ideal for Σ-consistency validation). **Metrics-only `requestedObjects` does NOT return a grand-total row** — it returns the base-grain grid as if no view were requested (observed 2026-06-11). For grand totals, aggregate a one-attribute rollup client-side.
- Executes against a published in-memory cube of ~3000 rows took ~2–3 min wall each on the observed tenant (instance polling, shared job queue) — budget accordingly in validation loops.

## Trino federation naming
- Column names from the Trino side differ from Mosaic REST. Descriptor attributes appear as `"<name> (<id form name> id)"` (e.g., `"category name (category desc id)"`). Check `get_semantics` output first; never guess column names from the UI.
- Verified 2026-09-11 on a Studio-built in-memory model: `SHOW COLUMNS FROM "<model>"` (direct Trino, catalog `sql`, schema = project name lowercased, basic auth with the MSTR user) is the authoritative column list. Naming is `<attribute> (<form>)` lowercase — `channel (id)`, `retailer (name)`, `sale date (day) (desc)` — and metrics are bare lowercase metric names. Custom forms use their form *name* (`product (derived sku)`), system forms their category (`(id)`, `(desc)`, `(start)`).
- **The ID form is hidden whenever the attribute has any other form** (Retailer/Product/Promotion expose only `(name)` / `(derived sku)`; single-form attributes expose `(id)`). Consequence: same-name elements with different keys are indistinguishable on the SQL surface (region-specific retailer keys collapse into one name). If entity identity matters (graph extraction, dedup, joins to external systems), take keys from REST cube/report execution or add the ID column as a custom form.
- Geo pseudo-forms (`region (latitude)`, `region (longitude)`) and the auto time hierarchy (`<date> (day|week|month|quarter|year) (id|start|desc)`) appear on the SQL surface without a corresponding REST form — match REST↔Trino by (attribute, form) and treat leftovers as pseudo-columns, never as missing forms.
- Direct Trino from Python: `trino.dbapi.connect(host=<Library host>, port=443, http_scheme="https", catalog="sql", schema="<project lower>", auth=BasicAuthentication(user, pw))`; `SHOW COLUMNS` on a 43-column model returned in ~2 s.
- Category hierarchy + fact joins fail with `A cartesian join is detected` until the bridging entity attribute (Subcategory on both `LU_SUBCATEG` and `LU_ITEM`) exists AND the relationships are wired. Both are required.

## Delete
- Verified path on Strategy ONE Cloud: `DELETE /api/objects/{id}?type=3` (generic objects endpoint).
- `DELETE /api/model/dataModels/{id}` worked on some Library builds but **404s on the observed Strategy ONE Cloud tenant family** — a 404 there means try the objects path, NOT idempotent success. (resolved 2026: newer tenant-verified observation wins — see `reference_strategy_object_cloning.md`)

## Session cap
- Canonical coverage: `feedback_build_mosaic_session_leak.md` — symptom (`500 Maximum number of interactive session per user for project exceeded …`, `8004cb0a` / iServerCode `-2147072486`), the one-session-per-pipeline rule (one process or `MSTR_REUSE_SESSION=1`), try/finally `m.logout()`, and recovery. (Earlier guidance here to wait 5–10 min after capping is superseded: iServer reaps project-interactive sessions on a ~30-min idle timer — budget the full 30 minutes and do not paper over with retries.)

## Dates stored as numbers, and time attributes (from a Studio-built model export, 2026-11-12)
- A yyyyMM integer column typed as a date in Studio is stored as `dataType: {type: date, precision: 0,
  scale: 0}` plus `format: "yyyyMM"` on the physical column AND on the pipeline column; `sourceDataType`
  stays integer. The attribute form gets `dataType: date`, `displayFormat: "date"`, `semanticRole: "date"`
  (an integer yyyyMM form had `"month"`), and the attribute a display format (`format.values`:
  `number_category` "7", `number_format` "yyyyMM"). `set-column-types ... date yyyyMM` writes the column and
  form shape; the attribute display format is not written (not needed for the connector).
- A **date** base takes time attributes (timestamp is not required): `smartAttributes` on the attribute,
  `smartAttributeType` `day_interval`, `week_interval`, `month_interval`, `quarter_interval`,
  `year_interval`, each with forms ID (integer), Start (timestamp) and DESC (text). On the connector:
  `<date> (month)__id` = 202201 (the yyyyMM value), `__start`, `__desc` = "2022-01"; quarter id 202201 for
  2022-Q1, desc "2022-Q1"; year id 2022. Grouping by them aggregates correctly (unlike GROUP BY an
  expression of the number). No 15-minute interval appears for a date base.
- Write: `PUT /api/model/dataModels/{id}/attributes/{attributeId}/smartAttributes` in a changeset, body
  `{"smartAttributes": [{"subType": "attribute_smart", "name": "<attribute> (Month)", "objectId"?: <existing>, "definition":
  {"smartAttributeType": "month_interval", "parameters": [], "ascending": true}}]}`. It replaces the whole
  set: send existing ones with their objectId. Each entry needs a `name` (refused without:
  8004c901 "Json input missing the following field: name"); Studio's are "<attribute> (Year)".
  `build_mosaic.py add-time-attributes` does this. **Verified live 2026-11-12** on a scratch model:
  `set-column-types ... date yyyyMM` on an integer yyyyMM column, republish, `add-time-attributes
  --intervals year,quarter,month`, republish, certify. The connector showed the column as a date,
  `(month)__id` = the old integer, quarters grouping 3 months, and a yyyyMM filter on `(month)__id` working.
- After a conversion, a description that says how the value was stored ("stored as YYYYMM integer") is
  wrong: change it in the same finding (`rename_describe`).


## File tables: the import settings decide how a reload splits (verified live, 2026-11-13)

- A file table's import settings are in the model export under the pipeline's `importSource.dataOptions`:
  separator, quote character, header row, encoding.
- With no quote character (`quote = ""`), the loader splits on **every** separator, quotes included:
  a reloaded file whose value was quoted to hide a comma loaded with its columns shifted. Verified by
  loading such a file by hand. Write a reload file in the table's own format, keep the separator,
  quotes and line breaks out of values, and give it exactly the table's columns (an extra column such as
  `source` breaks a straight reload). `assess_model.py check-lookup` checks a file against these rules.
