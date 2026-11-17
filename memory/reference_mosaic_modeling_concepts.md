---
name: Mosaic modeling concepts and payload shapes
description: Every Mosaic modeling construct (attribute forms, relationships, metric kinds, filters, transformations, hierarchies, consolidations, prompts) with the JSON body shape expected by the Modeling Service — and which of them are data-model sub-resources vs project-level /api/model/* objects.
type: reference
originSessionId: initial-session
---
Attributes, fact metrics, derived metrics, tables, relationships, security filters, ACL and translations live under `/api/model/dataModels/{id}/...`. **Facts, filters, transformations, hierarchies, consolidations, custom groups and prompts do not** — the spec has no such data-model sub-resource (only a read-only `GET /api/model/dataModels/{id}/hierarchy`); they are project-level objects under `/api/model/facts`, `/filters`, `/transformations`, `/hierarchies`, `/consolidations`, `/customGroups`, `/prompts` (corrected 2026-10-05). Every write sends the changeset in the `X-MSTR-MS-Changeset` header. When in doubt, `GET` an existing object of the same kind, capture the JSON, generate fresh UUIDs for inner `id`s, remap `objectId`s, and `POST` it back (the clone-and-remap pattern).

## Attributes

Body keys: `information, forms[], keyForm.id, attributeLookupTable, relationships?, displays?, childAttributes?, hidden?, hierarchyInfo?`

- Form: `{id?, category:"ID"|"DESC"|"<custom>", type:"system"|"custom", displayFormat:"text"|"number"|"date"|"time"|"picture"|"url"|"email"|"symbol"|"html_tag"|"phone_number", expressions:[{expression:{tokens:[...]}, tables:[{objectId,subType:"logical_table",name}]}], lookupTable, alias?}`.
- **Key form** system id is the universal ID form `45C11FA478E745FEA08D781CEA190FE5` when the first form is a system ID; otherwise omit `id` on a custom form and point `keyForm.id` at the form's newly-minted id post-create.
- **Multi-form attribute (ID + DESC + display forms):** one entry in `forms[]` per physical column; `keyForm.id` references the ID form; `displays.reportDisplays` / `browseDisplays` select which forms render in reports and browse prompts (set via `PATCH` after create).
- **Compound keys = a form group** (corrected 2026-10-05; earlier text said "multiple forms with `category:"ID"`" plus a synthetic expression form). The spec's data-model attribute form is `oneOf` a simple form or a **form group** (`ms-FormGroup`): `{"name": "ID", "category": "ID", "isFormGroup": true, "childForms": [{"name": "ID (1)"}, {"name": "ID (2)"}]}` — the group has no expressions of its own; each child is an ordinary simple form in `forms[]` carrying one key column, and `keyForm` points at the group (`{"name": "ID"}` in the REST docs' "Compound attribute" sample).

## Attribute relationships
`PUT /api/model/dataModels/{id}/attributes/{childId}/relationships` with the changeset in the `X-MSTR-MS-Changeset` header (the spec has no `?changesetId=` parameter; corrected 2026-10-05)
```json
{"relationships":[{
  "parent":{"objectId":"<parentAttrId>","subType":"attribute"},
  "child":{"objectId":"<childAttrId>","subType":"attribute"},
  "relationshipType":"one_to_many"|"many_to_one"|"many_to_many"|"one_to_one",
  "relationshipTable":{"objectId":"<factOrBridgeTableId>","subType":"logical_table"}
}]}
```
- `relationshipTable` is where the join actually occurs — use the fact or bridge table that contains both keys.

## Facts (reusable column bindings — optional; fact metrics can embed directly)
Inside a Mosaic model the fact lives in the fact metric's `fact` block (next section); there is no `/api/model/dataModels/{id}/facts`. A standalone fact is a project-level object: `POST /api/model/facts` (corrected 2026-10-05). Shape:
```json
{"information":{"name":"Revenue"},
 "dataType":"number",
 "expressions":[{"expression":{"tokens":[{"type":"column_reference","value":"REVENUE"}]},
                 "tables":[{"objectId":"<tblId>","subType":"logical_table","name":"SALES"}]}],
 "entryLevel":[]}
```

## Fact metrics — simple (SUM/AVG/…)
```json
{"information":{"name":"Sum Revenue"},
 "fact":{"dataType":"number",
         "expressions":[{"expression":{"tokens":[{"type":"column_reference","value":"REVENUE"}]},
                         "tables":[{"objectId":"<tblId>","subType":"logical_table","name":"SALES"}]}],
         "extensions":[],"entryLevel":[]},
 "function":"sum",           // ms-EnumFunction names: sum|avg|min|max|count|stdev|stdev_p|var|var_p|median|mode|product|geomean|percentile|first|last
 "functionProperties":[],
 "dimty":{},
 "format":{"header":[],"values":[...]}}
```
- Corrected 2026-10-05: there is no `count_distinct`, `geo_mean` or `p95` function. A distinct count is `function:"count"` plus the function property `Distinct` = true, e.g. `"functionProperties":[{"name":"Distinct","value":{"type":"boolean","value":"true"}}]` (same name/value shape as the documented `UseLookupForAttributes` property — verify the read-back on first use). Percentiles use `percentile`; the geometric mean is `geomean`.

## Compound metrics (derived)
Compound metrics are derived metrics: `POST /api/model/dataModels/{id}/metrics` (**not** `/factMetrics`) with an `expression.tree`, no `fact` block, no top-level `function`, `dimty: null` (corrected 2026-10-05 — the earlier example here used `metric_reference` / `operator` *tokens*, which are not token types in the spec, and posted to `/factMetrics`):
```json
{"information":{"name":"Profit","subType":"metric"},
 "expression":{"tree":{"type":"operator","function":"minus","children":[
   {"type":"object_reference","target":{"objectId":"<revenueMetricId>","subType":"fact_metric"}},
   {"type":"object_reference","target":{"objectId":"<costMetricId>","subType":"fact_metric"}}
 ]}},
 "dimty":null,"format":{...}}
```
Nest operator nodes instead of parentheses (`plus`/`minus`/`times`/`divide`/`unary_minus`); a `{"type":"constant","variant":{"type":"int64","value":"100"}}` node gives a literal. Ratio/margin/CAGR all fit here. Verified bodies and the update rule (full-body `PUT`, no `PATCH`): `reference_mosaic_derived_metrics.md` §0.

## Conditional metrics (filter-scoped)
A conditional metric is a derived metric on `/metrics` (not a `/factMetrics` body with a `fact` block): `Sum(<fact metric>)` tokens plus an embedded element-list filter created under the metric (`POST .../metrics/{id}/embeddedObjects`), then bound through the tokens and a `conditionality` block (`filter` with `isEmbedded: true`, `embedMethod: "report_into_metric_filter"`, `removeElements: true`) — create, embed and bind in one changeset. Full verified bodies: `reference_mosaic_derived_metrics.md` §0c; helper: `build_mosaic.py create-conditional-metric`. The `embed` / `removeAttrQualifications` keys this section used to show were invented; the verified block uses `embedMethod` / `removeElements`.

## Level (dimensionality-override) metrics
Control aggregation level independent of the report template via `dimty` (spec `ms-Dimty` / `ms-DimtyUnit`; corrected 2026-10-05 — the earlier `dimensions` / `grouping` / `allowAddedDimension` shape does not exist):
```json
"dimty": {
  "dimtyUnits":[{"dimtyUnitType":"attribute",            // default|attribute|dimension|report_level|report_base_level|role
                 "target":{"objectId":"<attrId>","subType":"attribute","name":"<Attr>"},
                 "aggregation":"normal",                  // normal|first_in_fact|last_in_fact|first_in_relationship|last_in_relationship
                 "filtering":"ignore",                    // apply|absolute|ignore|none
                 "groupBy":true}],
  "excludeAttribute":false,
  "allowAddingUnit":true
}
```
- Level metrics are derived metrics on `/metrics`, written with tokens (the tree format cannot express a level) — the verified recipe is `reference_mosaic_derived_metrics.md` §0b: an attribute-ONLY dimty plus the `{[Attr]+}` token cluster.
- "Share of parent" / "index vs peers": pin the parent attribute with `groupBy:true` and `filtering:"ignore"` (keeps the whole parent but honors filters in other hierarchies — §0b); `"apply"` shrinks the parent to the filtered children, `"absolute"` also drops filters in other hierarchies.

## Transformations (time-shift / YoY / prior period)
**In a Mosaic model, use a time-shift derived metric instead** (`PreviousMonth(...)` and friends;
`reference_mosaic_derived_metrics.md` §0e). What follows is the classic project-level object.

Standalone **project-level** object — create first, then attach to metrics. There is no `/api/model/dataModels/{id}/transformations` (corrected 2026-10-05); the body below is an older sketch, not checked against the spec.

Create:
```json
POST /api/model/transformations
{"information":{"name":"Last Year"},
 "members":[{"attribute":{"objectId":"<dateAttrId>","subType":"attribute"},
             "offset":-12,
             "mappingTable":{"objectId":"<calendarTableId>","subType":"logical_table"}}]}
```

Attach (creates a new time-shifted metric) — unverified: the spec's `ms-FactMetric` / `ms-Metric` schemas have no `transformation` field, so capture a UI-built transformation metric before scripting this:
```json
POST /api/model/dataModels/{id}/factMetrics
{"information":{"name":"Revenue LY"},
 "fact":{...},"function":"sum","dimty":{},"format":{...},
 "transformation":{"objectId":"<transformationId>","subType":"transformation"}}
```

## Filters
Project-level `POST /api/model/filters` — there is no `/api/model/dataModels/{id}/filters` (corrected 2026-10-05). A filter that belongs to one Mosaic metric is created under the metric with `POST /api/model/dataModels/{id}/metrics/{metricId}/embeddedObjects` (`reference_mosaic_derived_metrics.md` §0c); a row-level filter on the model is a security filter (below).
```json
{"information":{"name":"EMEA"},
 "qualification":{"tree":{
    "type":"predicate_form_qualification",
    "predicateTree":{
       "function":"in",
       "attribute":{"objectId":"<regionAttrId>"},
       "form":{"objectId":"45C11FA478E745FEA08D781CEA190FE5"},
       "constant":{"type":"string","value":"EMEA"}
    }
 }}}
```
Predicate types (spec `ms-ExpressionNodeBase.type`): `predicate_form_qualification`, `predicate_metric_qualification`, `predicate_element_list`, `predicate_joint_element_list`, `predicate_filter_qualification`, `predicate_report_qualification`, `predicate_relationship`, `predicate_prompt`, `predicate_custom`, `predicate_column_qualification`, `predicate_banding_*` (there is no `predicate_false`).
Compose via `{"type":"operator","function":"and"|"or"|"not","children":[…]}` — operator nodes name their function in `function`, not `operator` (corrected 2026-10-05).

## Hierarchies (user-defined drill paths)
Project-level `POST /api/model/hierarchies` (User Hierarchies). A Mosaic model has only the read-only `GET /api/model/dataModels/{id}/hierarchy` (every relationship in the model's system hierarchy); there is no `/api/model/dataModels/{id}/hierarchies` (corrected 2026-10-05). Body sketch, not checked against the spec:
```json
{"information":{"name":"Geography"},
 "attributes":[{"id":"<regionId>","filters":[]},{"id":"<countryId>"},{"id":"<cityId>"}],
 "relationships":[{"parent":"<regionId>","child":"<countryId>"},
                  {"parent":"<countryId>","child":"<cityId>"}]}
```

## Consolidations (enumerated virtual elements)
Project-level `POST /api/model/consolidations` (no data-model sub-resource; corrected 2026-10-05). Body sketch, not checked against the spec:
```json
{"information":{"name":"Top Regions"},
 "elements":[{"name":"Americas","expression":"Region@ID IN ('US','CA','MX')"},
             {"name":"Europe","expression":"Region@ID IN ('DE','FR','UK')"}]}
```

## Custom groups (dynamic banded buckets)
Project-level `POST /api/model/customGroups` (no data-model sub-resource; corrected 2026-10-05) — list of elements where each element is a filter expression + optional sub-banding (equal_count/equal_range/custom breaks).

## Prompts
Project-level `POST /api/model/prompts` (no data-model sub-resource; corrected 2026-10-05).
Types: `attribute_element` (pick specific elements), `attribute_qualification` (user writes predicate), `hierarchy_qualification`, `value` (number/date/text/bigDecimal), `object` (pick metadata object).

## Security filters
Create — `POST /api/model/dataModels/{id}/securityFilters` with `qualification.tree`.
Assign — `PATCH /api/dataModels/{dataModelId}/securityFilters/{sfId}/members` with `{operationList:[{op:"addElements",path:"/Members",value:[ids...]}]}` (`/Members` is what a live tenant accepted; the spec's `path` enum says `/members` — record both; `op` has no `replaceElements`). The older `POST /api/model/dataModels/{id}/securityFilters/{sfId}/members` variant is not in the current spec.
Top/bottom level override how the filter is applied vs report filter.

## Translations
Data-model object translations: `PATCH /api/model/dataModels/{modelId}/objects/{objectId}/translations?subType=<objectSubType>` with body `{name:{translationValues:{locale:{translation:"Client"}}}}` and/or `description`.

## ACL / object security
Data-model object ACL: `PATCH /api/model/dataModels/{modelId}/objects/{objectId}/acl?subType=<objectSubType>` with body `{acl:{trusteeId:{granted:<mask>,denied:<mask>,subType:"user"|"user_group"}}}` inside a changeset.
Rights (EnumDSSXMLAccessRightFlags; corrected 2026-10-05 — the old read=1/write=2/delete=4/browse=64/use=512/inherit=1024 table was wrong): browse=1, use_execute=2, read=4, write=8, delete=16, control=32, use=64, execute=128; view=197, modify=221, full=255. Inheritance is the separate `inheritable` flag. Classic objects: `GET/PUT /api/objects/{id}?type=` (`acl` body) — there is no `/api/objects/{id}/acl`. Details: `reference_mosaic_acl.md`.

## Certification
`PUT /api/objects/{id}/certify?type=<type>&certify=true` — Library marks with the certified badge. (The spec has no `PATCH /api/objects/{id}`; corrected 2026-10-05.)

## VLDB properties (SQL generation behavior)
Internal `GET/DELETE /api/objects/{id}/vldb/propertySets` + `PUT …/vldb/propertySets/{name}` (datasets/documents), and public `GET /api/model/{reports|cubes|datamarts}/{id}/applicableVldbProperties`; Modeling objects return theirs with `showAdvancedProperties=true`. There is no `/api/objects/{id}/vldbProperties` (corrected 2026-10-05). Use when the auto-generated SQL needs to match a specific warehouse's quirks.
