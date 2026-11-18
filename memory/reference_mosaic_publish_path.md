---
name: Mosaic in-memory publish — endpoints, single-trigger rule, and dataType preconditions
description: The ONE publish file for Mosaic data models (subType 779 + extType 448). The documented flow is `POST /api/dataModels/{id}/instances` → `POST …/publish` (tables[].refreshPolicy) → `GET …/publishStatus` (per-table `completed`) → `DELETE …/instances/{instanceId}`; `POST /api/cubes/{id}?cubeAction=publish` (what the Studio UI fired in 2026-04 captures) is internal + deprecated in the spec — tenant-observed fallback only. Pick exactly ONE trigger per run (firing both locks `publishStatus` into 500 iServerCode -2147072194). Either path silently no-ops or stalls (-2147212544) unless physical-table dataTypes are clean pipeline types, not warehouse-catalog sentinels. Load before any Mosaic publish/refresh.
type: reference
---

> **Current rule (2026-10-05)** — from the REST docs (`common-workflows/mosaic/publish/refresh-a-data-model`, available since August 2025) and the tenant's OpenAPI spec (tag Data Models, all public):
>
> 1. `POST /api/dataModels/{id}/instances` → 204; instance id in the `X-MSTR-DataModelInstanceId` response header.
> 2. `POST /api/dataModels/{id}/publish` with that header and body `{"tables":[{"id":"<tableId>","refreshPolicy":"replace"}]}` → 204. `refreshPolicy` ∈ add / delete / update / upsert / replace / ignore / reserved; table ids from `GET /api/model/dataModels/{id}/tables`.
> 3. `GET /api/dataModels/{id}/publishStatus` (same header) → `{status, tables:[{id, status}]}`. Per-table `status` enum: `reserved`, `waiting`, `publishing`, `completed`, `error`, `schema_comparison_completed`, `schema_comparison_error`. Done = every table `completed`; stop on `error` / `schema_comparison_error`. There is no `loaded` (older text below said so; `build_mosaic.py publish` now accepts `loaded|completed`).
> 4. `DELETE /api/dataModels/{id}/instances/{instanceId}` → 204 once the publish is done.
>
> `POST /api/cubes/{id}?cubeAction=publish` is `publishCube` in the spec — **internal and deprecated**, described as publishing an "Intelligent cube or MTDI cube". Use it only as a tenant-observed fallback when the documented flow fails, never in the same run as steps 1–3. Whatever the trigger, confirm with an independent probe (cube-execute probe or Trino/MCP query) — `publishStatus` under-reported on this tenant family (history below). A *scheduled* refresh is a subscription, not a publish call (`reference_strategy_subscriptions_and_schedules.md`).
>
> Everything below this box is dated tenant observation (history). Where it conflicts with the box, the box wins; the dataType preconditions and completion-probe advice still apply.

## Background (history, 2026-04) — a 2026-04-22 memory was half wrong

`reference_mosaic_vs_legacy_surfaces.md` declared "`/api/cubes/*` is NOT the publish path for a Mosaic data model — use `POST /api/dataModels/{id}/publish`." After 2026-04-23, corrected: both paths work for a properly-typed Mosaic model. The real failure mode that memory captured was the dirty dataTypes — not the endpoint choice (see "DataType preconditions" below). Keep that memory for the classification rules (subType 779 + extType 448 vs 776) and the endpoint-pair cheat sheet; read this one for the actual publish trigger. Observations here are from a Strategy ONE Cloud tenant; recheck endpoint choice on different iServer build families.

## What the Studio UI calls when you click "Publish" (history — tenant observation, 2026-04)

Single request (internal + deprecated `publishCube` in the spec — see the box at the top):
```
POST /api/cubes/{modelId}?cubeAction=publish
Headers: X-MSTR-AuthToken, X-MSTR-ProjectID
Body: (empty)
-> HTTP 202 Accepted
```

The server queues a CubeServer publish job (visible as `Cube report "<model name>" is being published by job <N>` if you race a second publish). Data lands in the cube within tens of seconds for small models; ~1–2 minutes for 4-table models with 5–50k rows per table.

No instance header is needed. No body is needed. The cube materializes for Trino federation automatically; no follow-up "activate" step.

## The Modeling-native 3-step flow (the documented flow — see the box at the top)

The default trigger. It is also the only one with per-table refresh policies (add / replace / delete / update / upsert / ignore / reserved) and a returned instance id for polling. Delete the instance when done (`DELETE /api/dataModels/{modelId}/instances/{instanceId}`).

```
# 1. Create a data-model instance (no body). 204; instance id in RESPONSE HEADER.
POST /api/dataModels/{modelId}/instances
-> 204, X-MSTR-DataModelInstanceId: <inst>

# 2. Trigger publish. A body with a tables[] entry per logical table is REQUIRED — an empty body or bare {} fails (400 "tableRefreshSettings cannot be null"; observed as 500 on some builds).
POST /api/dataModels/{modelId}/publish
Headers: X-MSTR-DataModelInstanceId: <inst>
Body: {"tables":[{"id":"<tid>","refreshPolicy":"replace"}, ...]}    # one entry per logical table
-> 204 (fire-and-forget)

# 3. Poll status.
GET /api/dataModels/{modelId}/publishStatus
Headers: X-MSTR-DataModelInstanceId: <inst>
-> 200 {"status": <int>, "tables":[{"id","status":"reserved|waiting|publishing|completed|error|schema_comparison_completed|schema_comparison_error", ...}]}

# 4. Clean up.
DELETE /api/dataModels/{modelId}/instances/{inst}
-> 204
```

Top-level statuses observed (tenant history). The spec documents the top-level `status` as an EnumDSSXMLStatus value (0 MsgID, 1 Result, 4 JobRunning, 5 InSQLEngine, 6 InQueryEngine, 13 Waiting, …), so the readings below are tenant interpretations, not the enum — `1` may well mean "result ready", not "running". Decide completion from the per-table enum plus an independent probe:
- `0` — all tables done (happy end).
- `1` — job queued/running. Tables list may be empty for the first tens of seconds.
- `5` — reserved.
- `6` — schema comparison completed.
- `-2147418587` — terminal failure after partial table completion. Observed with several tables reporting `completed`, followed by a cube execute probe returning `-2147072488` (not published). Treat as a failed materialization, not a still-running publish. Capture the full per-table status before the instance expires, then inspect clean dataTypes, large fact-table feasibility, warehouse availability/resume, and CubeServer health.
- `-2147212544` — CubeServer parallel-mode stall. With REF-clean dataTypes this is rare; with warehouse-catalog types it is the default outcome on the observed Strategy ONE Cloud tenant family (see "DataType preconditions" below).

Per-table statuses (spec enum `TablePublishStatus.status`): `reserved`, `waiting`, `publishing`, `completed`, `error`, `schema_comparison_completed`, `schema_comparison_error`. Happy end = `completed`; terminal failure = `error` or `schema_comparison_error`. (Corrected 2026-10-05: earlier versions said the happy end was `loaded` — that value is not in the enum; `build_mosaic.py publish` accepts `loaded|completed`.)

**Instance id lifetime.** The X-MSTR-DataModelInstanceId expires within a couple minutes; if you see repeated `404 ERR004 "Message not found in user history list"` on `publishStatus`, your instance was reaped — mint a new one and re-publish. A short-lived session (one Python process, keep polling in the same `requests.Session`) rarely trips this; longer polling with multiple login cycles does.

## Which path to use in automation (corrected 2026-10-05)

| Situation | Use |
|---|---|
| "Just publish this model; don't care about refresh policies" | 3-step Modeling flow with `refreshPolicy:"replace"` on every table, then delete the instance. (Earlier versions said `/api/cubes/{id}?cubeAction=publish` — internal + deprecated in the spec; keep it as the fallback when the documented flow fails on a tenant, never in the same run.) |
| "Publish and confirm every table is done" | 3-step Modeling flow — only this returns per-table status. |
| "Incremental refresh (add/update specific tables)" | 3-step Modeling flow with specific `refreshPolicy`. |
| "Policy gate before declaring validation ready" | Poll `/publishStatus` until every table `status:"completed"` (no `loaded` in the enum) AND confirm with an independent probe. Don't trust the 202/204 alone. |

## Completion polling — what actually works (2026-06-11 correction)

Observed on a Strategy ONE Cloud tenant (2026-06-11 capture): **`GET /api/cubes/{id}` is definition-only** — the response carries `id`, `name`, `result.definition.availableObjects` and NO status/size/rowCount fields, so it CANNOT serve as a publish-completion poll (an earlier revision of this file suggested it could). Verified completion probes, in order of preference:

1. **Cube-execute probe**: `POST /api/v2/cubes/{id}/instances?limit=1` body `{}` — 200 with data ⇒ published; `500 iServerCode -2147072488 "Intelligent Cube … is not published"` ⇒ not (yet) materialized. Definitive in both directions.
2. **3-step `publishStatus`** with the SAME instance that fired the 3-step publish — only valid when the 3-step flow was this run's single trigger.
3. **A scoped Trino/MCP query** (one attribute and one metric, with a `LIMIT`) when an MCP/Trino session is available — not a whole-model `count(*)`, which can hit the job time limit on big models (`reference_mcp_tools.md`).

A `202` from `/api/cubes` or `204` from `/publish` proves only that the job was queued — and **`publishStatus` itself can LIE**. Verified 2026-06-11 (Strategy ONE Cloud family): `publishStatus` polled with the same instance that fired the 3-step publish returned `status=1, tables:[]` continuously for 5 minutes — and 9 seconds after the last poll, a cube-execute probe returned 200 with the full 3000-row cube. The cube had materialized while `publishStatus` still reported "running, no tables". Treat `status=1/tables:[]` as "unknown", never as "stalled": interleave the cube-execute probe (`POST /api/v2/cubes/{id}/instances?limit=1` → 200 = done, `-2147072488` = not yet) into every publish poll loop, and let IT decide.

**A republish has no such proof.** The model already answers with its old data, so the execute probe says nothing. Seen 2026-11-18 (a one-table in-memory scratch model, replace): `publishStatus` answered `{"status": 1}` (no `tables` key) for the full 600 s while the server showed the publish complete, and the new fact metric read right through the connector afterwards. `_mosaic_publish_verified` now prints a progress line every 60 s, and on a republish whose `publishStatus` never lists a table it stops after 120 s with "publish NOT CONFIRMED" (exit 0, instance kept so a running job isn't aborted); check the new data in the connector. A republish that lists its tables still waits for every table to complete. A real completion signal for this case (a cube last-update time, say) is not yet found.

### Snowflake warehouse auto-resume makes publish look stalled

Same observation set: publishes triggered against a COLD (auto-suspended) Snowflake warehouse showed nothing queryable for 10+ minutes (execute probe → `-2147072488` 12 minutes after a 202). After a connect_live query forced the warehouse to resume (~2-min first-query latency), a fresh trigger had the cube queryable within ~5 minutes. Publish jobs appear far more sensitive to warehouse-resume latency than interactive queries — they may sit invisible (`status=1, tables:[]`, no error) for the entire resume + queue window. Operational rules:
1. Budget 10–15 minutes of execute-probe polling before declaring a publish dead on auto-suspend warehouses — or pre-warm the warehouse with any small live/catalog query right before triggering. **Pre-warm CAUTION (2026-06-17):** a `connect_live` execute (`POST /api/v2/cubes/{id}/instances`) is *synchronous* — iServer holds the HTTP connection while the warehouse resumes, so an un-timed-out client call HANGS on the very resume it is meant to trigger (observed: 10+ min, zero output, looked like a crash). If you pre-warm this way, give the call a finite `(connect, read)` timeout (e.g. `(15, 300)`), treat it as best-effort, and keep it OFF the critical path. Simplest robust path: skip the separate pre-warm and just trigger publish + execute-probe with the 10–15 min budget — the publish job resumes the warehouse on its own. See `feedback_rest_timeout_and_warehouse_resume_hang.md`.
2. Do NOT fire repeat triggers every few minutes "because nothing is happening" — queued jobs serialize on the cube lock and you can't tell which one landed.
3. The differential ladder below still applies when the execute probe stays negative past that budget.

### Differential ladder when publish stays unqueryable with clean types

Run IN ORDER (each one short session; scripts in `captures/20260611-pharma-build/`):
1. **REF execute** — known-good model loads ⇒ cube-serving infra + privileges fine; failure is publish-specific.
2. **Privileges** — `GET /api/sessions/privileges`: 162 Publish Intelligent Cube, 164 Web publish, 144 Administer Cubes, 316 Use Mosaic Studio.
3. **1-table publish canary** (`canary_build.py`) — also unqueryable ⇒ not your model's shape/size.
4. **connect_live canary** (`CANARY_SERVE_MODE=connect_live`) — live rows ⇒ warehouse + DS connection healthy, AND the warehouse is now HOT.
5. **Hot-warehouse re-publish** — ONE fresh trigger right after step 4, then execute-probe for 10+ min. Loads ⇒ root cause was warehouse-resume latency (2026-06-11 outcome). Still nothing ⇒ genuine CubeServer/publish-pipeline failure — escalate to the tenant admin with trigger timestamps.

## Never fire both publish endpoints in the same run

When publishing an in-memory Mosaic data model, call **exactly one** of the two publish endpoints per run:

- `POST /api/dataModels/{id}/instances` + `POST /api/dataModels/{id}/publish` + `GET /api/dataModels/{id}/publishStatus` (+ instance delete) — the documented three-step flow, uses instance id, only this returns per-table status.
- `POST /api/cubes/{id}?cubeAction=publish` — UI-equivalent in 2026-04 captures, internal + deprecated in the spec, returns 202 immediately, no instance needed. Fallback only. Poll by Trino probe or the cube-execute probe (not `GET /api/cubes/{id}` — definition-only, see above).

Do NOT issue both. If you fire `/api/cubes` first and then follow up with the 3-step `publish` POST, Strategy sees two publish jobs racing on the same cube. The CubeServer serializes them (accepts the first, queues/rejects the second), and `publishStatus` against the LOSING instance id returns `500 ERR001 iServerCode -2147072194` "is being published by job N" for the full duration of the winning job. Your polling loop sees the error continuously, times out, and reports failure — even though the cube is actually being published successfully.

### Observed (history)

Strategy ONE Cloud tenant, multi-DB in-memory model (captured run):
- `/api/cubes/{id}?cubeAction=publish` → 202 (started publish job N).
- `/api/dataModels/{id}/publish` fired 200ms later → 204 (second publish, queued/blocked).
- `GET /api/dataModels/{id}/publishStatus` with the 3-step instance id → **21 consecutive 500 responses over ~5 minutes**, all `iServerCode: -2147072194` "Cube report … is being published by job N". The script never saw a green status.
- User checked the Library UI: publish had completed **in seconds**. MCP Trino query confirmed the cube had materialized its expected row count.

### Fix pattern — single trigger + independent completion probe

```python
# Single trigger = the documented 3-step flow (corrected 2026-10-05; the 2026-04 version of
# this snippet fired the internal/deprecated /api/cubes/{id}?cubeAction=publish instead)
inst = s.post(f"{BASE}/api/dataModels/{MID}/instances").headers["X-MSTR-DataModelInstanceId"]
hdr = {"X-MSTR-DataModelInstanceId": inst}
r = s.post(f"{BASE}/api/dataModels/{MID}/publish", headers=hdr,
           json={"tables": [{"id": t, "refreshPolicy": "replace"} for t in TABLE_IDS]})
assert r.status_code == 204

# Poll publishStatus for per-table "completed", but let an independent probe decide
deadline = time.time() + 600
while time.time() < deadline:
    time.sleep(15)
    st = s.get(f"{BASE}/api/dataModels/{MID}/publishStatus", headers=hdr).json()
    if any(t.get("status") in ("error", "schema_comparison_error") for t in st.get("tables", [])):
        raise RuntimeError(st)
    probe = s.post(f"{BASE}/api/v2/cubes/{MID}/instances", params={"limit": 1}, json={})
    if probe.ok: break          # cube-execute probe; 500 iServerCode -2147072488 = not published yet
    # (or a scoped query: strategy_mcp.py query --project "<project>" --sql '<attribute + metric … LIMIT 1>')
s.delete(f"{BASE}/api/dataModels/{MID}/instances/{inst}")
```

If the documented flow fails on a tenant, the fallback is ONE `POST /api/cubes/{MID}?cubeAction=publish` (202) in a separate run, with the same probe — never both triggers in one run.

### Why this tripped a real run (history)

The script was defensive — it tried both paths to be robust against either one failing. In this tenant family BOTH paths succeed, but they serialize on the cube lock, and the losing instance's `publishStatus` call is the one the script polled. Net effect: a working publish looked like an infinite stall.

**Don't confuse the two iServerCodes:** `-2147212544` = real stall (bad dataTypes — next section); `-2147072194` = job-in-progress lockout (you fired both endpoints).

## DataType preconditions — clean pipeline types, not warehouse-catalog types

Confirmed 2026-04-23 on a Strategy ONE Cloud tenant: when the publishable columns carry warehouse-catalog sentinels (precision=-1 or scale=-2147483648, `variable_length_string`, `fixed_length_string`, `binary`, `unsigned`, `decimal` with warehouse precision), the Mosaic in-memory publish accepts the request but the cube never materializes (status=1 with empty tables, or -2147212544 stall). A reference model built by the UI wizard shows normalized types (`utf8_char(32000,0)`, `integer(4,0)`, `integer(2,0)`, `double(P,S)`, `int64(8,0)`, `date(10,0)`, `time_stamp(26,6)` or `(23,9)`) — cloning those dataTypes into an otherwise-identical model fixes publish end-to-end. Recheck on other iServer build families before asserting this as universal.

### What happens if you skip this

`build_mosaic.py build` reads warehouse column metadata via `/api/datasources/{id}/catalog/tables/{tid}` and forwards those `dataType` objects verbatim into the new physical table's `physicalTable.columns[]` and pipeline. Two symptoms follow:

1. The helper's `publish` subcommand and the Mosaic 3-step flow return 2xx and then either:
   - `status=1` with `tables:[]` forever (nothing happens — IServer silently no-ops), or
   - `-2147212544` QueryEngine parallel-mode stall that the 2026-04-22 memory attributed to the tenant. We now know at least part of that stall was the dataType shape, not tenant health — a REF model built the same day with clean types publishes fine.
2. The UI's "Publish" button, which routes through `POST /api/cubes/{modelId}?cubeAction=publish` (202 Accepted), also works on these clean-typed models. On dirty-typed models the UI still hits `/api/cubes` but the resulting job errors inside the CubeServer component.

### Canonical dataType mapping (warehouse → Mosaic in-memory)

Apply this when building a physical table to publish. Source values come from `/api/datasources/{id}/catalog/tables/{tid}`; target values are what the UI-created reference model (REF) used.

| Source `type` | Source shape | → Target `type` | Target precision | Target scale |
|---|---|---|---|---|
| `variable_length_string` | precision=-1, scale=-MIN_INT | `utf8_char` | 32000 | 0 |
| `fixed_length_string` | precision=any, scale=-MIN_INT | `utf8_char` | 32000 | 0 |
| `integer` | precision=4, scale=-MIN_INT | `integer` | 4 | 0 |
| `binary` | precision=1, scale=-1 | `integer` | 2 | 0 |
| `unsigned` | precision=1, scale=-MIN_INT | `integer` | 2 | 0 |
| `decimal` | precision=P, scale=0 | `int64` | 8 | 0 |
| `decimal` | precision=P, scale=S (S>0) | `double` | P | S |
| `time_stamp` | precision=8 | `time_stamp` | 26 | 6 |
| `time_stamp` | precision=9 | `time_stamp` | 23 | 9 |
| `date` | precision=0, scale=-MIN_INT | `date` | 10 | 0 |

`-MIN_INT` above = `-2147483648` (Java `Integer.MIN_VALUE`, meaning "not set" in the warehouse catalog payload).

### Where the dataType lives

Two places must stay in sync on every table:

1. `physicalTable.columns[i].dataType` — the public column definition.
2. `physicalTable.pipeline` (JSON string) — the pipeline spec has its own `rootTable.children[*].columns[*].dataType` AND `sourceDataType`. Both need the cleaned type.

Aligning them by column name is the safe pattern — before POSTing the table, walk the pipeline, ensure the `id` of each pipeline column matches `physicalTable.columns[i].information.objectId`, and ensure both `dataType` and `sourceDataType` carry the target shape.

### The clean-types-via-clone pattern

The fastest way to fix an already-built dirty-typed model is to clone a known-good reference:

1. Fetch REF model via `GET /api/model/dataModels/{refId}?showExpressionAs=tokens`, then for each table `GET /api/model/dataModels/{refId}/tables/{tid}?showColumns=true`.
2. Deep-copy each table body; strip `information.objectId`/`information.dateCreated` and the pipeline's `id`/`rootTable.id`/`children[*].id`/`columns[*].id` — mint fresh UUIDs. Keep column NAMES identical so `physicalTable.columns` and the pipeline's column list still zip by name.
3. Keep every `dataType`/`sourceDataType` from REF unchanged — this is the whole point.
4. POST each rebuilt table to the new model inside a changeset.
5. Clone attributes and fact metrics too, using text-only `column_reference` tokens (`{"type":"column_reference","value":"COL_NAME"}` — no `target.objectId`) so Mosaic re-binds to the new column ids by name on commit. Do NOT carry REF's `expressionId`/`target.objectId` values — those will collide.
6. Commit tables + attributes + metrics in ONE changeset. The "table has no attribute/metric" commit check (`8004e42f`) requires at least one attribute or metric per table to be created before commit.
7. Follow up with a second changeset for relationships and a third for security filters.

## Helper note (updated 2026-10-05)

`build_mosaic.py publish` classifies the object first (779 + extType 448 → Mosaic), then runs the documented 3-step flow and polls `publishStatus` until every table reads `loaded|completed`, failing on `error`. It never fires `/api/cubes/*` for a Mosaic model; `/api/cubes/{id}?cubeAction=publish` is used only for a classic (776) cube. (The 2026-04 version of this note said the helper tried `/api/cubes/{id}` first with no status poll — no longer true.) Still add an independent probe before declaring validation ready.

## Verified on 2026-04-23 (history — tenant-family: Strategy ONE Cloud)

- `/api/cubes/{id}?cubeAction=publish` → 202, followed by Trino query success on the materialized model.
- `/api/dataModels/{id}/publish` with per-table bodies → 204, followed by `publishStatus` returning `status=1 tables:[]` while a parallel `/api/cubes` publish completed. Conclusion: the Modeling-native publish accepts the request but queues it in a way the CubeServer may not always drain on this tenant family — the `/api/cubes` path is the reliable one here. Recheck on other iServer build families.

(Raw reproduction, including model IDs and query text, lives under `captures/` on the run date. Link the capture from the follow-up ticket rather than re-embedding IDs here. The 2026-04-22 tenant-level QueryEngineServer stall narrative lives in `captures/2026-04-22-queryengine-publish-incident/README.md`.)

## REST republish does not rebuild new attributes or relationships (observed 2026-11-15)

After a phase that added relationships (wire-relationships), time attributes and column attributes, the documented
3-step publish (16 tables, replace) reported every table `completed` at the first poll, and certify succeeded. The
connector then listed the new objects but returned every new attribute blank (time attributes and column attributes)
and still ignored filters that the new relationships should have fixed. A publish from the Studio UI (no re-certify)
fixed all of it. Together with the 2026-09-22 observation below (REST trigger a no-op when no table or column
changed), the rule: **after model-level changes (attributes, time attributes, relationships), publish from Studio.**
`plan` adds that as a confirmed manual step after the REST republish, also after a `patch_model_object` on an
attribute or a table (assumed to behave like the other attribute changes, not yet verified; 2026-11-16); `publish`
warns when every table says completed at the first poll. What Studio sends today is not captured: capture its request (browser network panel)
the next time it is needed, to replace the manual step.

## Republish completion proof (observed 2026-09-17)

On a **republish** of an already-published in_memory model, the cube-execute probe (`POST /api/v2/cubes/{id}/instances`) keeps succeeding against the OLD cube, so a 2xx probe proves nothing about the new definition. Prove the republish landed by probing a metric whose definition you just changed (e.g., a `sum`→`avg` function fix: values drop from thousands to single digits) or a table you just added; poll that until the new shape appears. One trigger per run still applies.

**Metric function/format edits do NOT need a republish (observed 2026-09-21, Strategy ONE Cloud, in_memory model):**
a `PATCH /factMetrics/{id}` that flipped `function` sum→avg read back as averages in both the cube-instance API and the
Trino/MCP surface immediately after the changeset commit, before the `cubeAction=publish` job (202) had run. Aggregation
function, format and description changes are applied at query time; so are compound (derived) metrics. Reserve the
republish + changed-definition probe for data-shape changes — new tables, new columns, refreshed rows.

**Derived-metric-only changes and the cube-instance API (observed 2026-09-22, Strategy ONE Cloud, in_memory model):** a
derived (level) metric created after the last publish is listed in `GET /api/v2/cubes/{id}` `availableObjects` and queryable on
the Trino/MCP surface immediately, but `POST /api/v2/cubes/{id}/instances` answers `ERR006 "Failed to find the metric … in the
report or cube"` until a publish job is registered. `POST /api/cubes/{id}?cubeAction=publish` returned 202 yet produced no new
cube version in 20 minutes (cube cache monitor `GET /api/monitors/caches/cubes?clusterNode=<node>&projectIds=<pid>`: state loaded,
processing=false, lastUpdateTime unchanged — the REST trigger appears to be a no-op when no table/column changed). The Studio
UI's Publish button DID reload the cube (new version on both cluster nodes in ~1 minute), after which the cube-instance API
served the metric. A Modeling-native 3-step publish fired a minute after that returned 204 with every table `reserved` but
produced no further cube version (duplicate/no-op), so it is not established as the fix for this case. Practical rule: after
derived-metric-only changes, verify through Trino/MCP, and have the model owner press Publish in Studio (or make a real
table/column change) before relying on cube-instance or dashboard grids. Cache-monitor ids are base64 of
`<cacheId>:<projectId>:<node>` (pad to a multiple of 4 before decoding); match a model through `source.id`; the cache monitor
is the reliable publish-completion evidence when `publishStatus` is unavailable.

