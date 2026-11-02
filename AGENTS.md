# AGENTS.md — canonical, LLM-agnostic entry point

You are operating inside the **strategy-automate** repo: skills, field-verified notes and Python helpers for automating **Strategy** (formerly MicroStrategy) through its REST API and Mosaic MCP server. It aims to cover every surface Strategy exposes through an API, SDK, MCP, CLI or reproducible hook: Mosaic semantic models, the classic / legacy semantic layer, runtime analytics, cubes / datasets, platform admin, AI agents, and data validation. Known gaps are tracked in `memory/reference_strategy_automation_coverage.md`.

This file is the **canonical cross-tool entry point**. Every LLM-specific shim at the repo root (`CLAUDE.md`, `GEMINI.md`, `CODEX.md`, `GROK.md`, `OLLAMA.md`, `CURSOR.md`, etc.) points here. If you are a model or tool not listed there, read this file + `memory/MEMORY.md` and proceed — nothing else is tool-specific.

**Harness assumptions (apply across LLMs):**
- The repo is plain Markdown + Python 3.9+ (standard library + `requests`). No Anthropic-specific, OpenAI-specific, or Google-specific SDK calls — every helper is `requests` against Strategy REST; one optional variant (`strategy_library_publications_mstrio.py`) imports `mstrio-py`.
- `$REPO` in skills and notes means the root of this checkout.
- `SKILL.md` frontmatter (`name`, `description`) follows Anthropic's skill convention, but any harness that reads Markdown with YAML frontmatter can use it. A skill-unaware LLM can read each `SKILL.md` as a normal instruction file.
- `memory/MEMORY.md` is a flat index with one-line hooks; any LLM can `grep` or keyword-match to find the relevant memory file on demand.
- Shell helpers live in `skills/strategy-platform/scripts/` (sign-in, any REST operation, MCP) and `skills/build-mosaic-model/scripts/` (Mosaic build, inventory, mining, validation). Invoke them via whatever tool-call mechanism your harness exposes (Bash, shell, execute_command, tool-use-bash, etc.).
- Configuration comes from env vars (`MSTR_BASE`, `MSTR_PROJECT_ID`/`MSTR_PROJECT_NAME`, `MSTR_DEST_FOLDER_ID`) — see `memory/reference_strategy_env.md`. Never hardcode.
- **Sign-in:** every script takes `--auth-method` / `MSTR_AUTH_METHOD` (default `auto`) through `skills/strategy-platform/scripts/strategy_auth.py`: `MSTR_API_TOKEN`, `MSTR_USER`+`MSTR_PASSWORD` (Standard or LDAP), or — with no credentials at all — a cached browser single-sign-on session (`sso`). Never type a user's password, never print tokens, never pass secrets as command-line flags. An `sso` sign-in needs the human to click **Allow** in their browser; if a command waits for it, tell them. See `memory/reference_strategy_authentication.md`.

## Git workflow

- Operator configures their own remotes and git identity locally (`git remote -v`, `git config user.email`) — do not hardcode remote URLs or identities here.
- Default pull/push targets `origin`. Run `git pull --ff-only` before starting shared work and `git push` after commit.
- Before committing, run `python3 tests/tools/check.py` (the CI gates: tests, ruff, mypy, bandit, coverage) plus `git diff --check`. The suite is hermetic and offline; `tests/live/smoke.py` is the only thing that touches a tenant, read-only and only when you run it.
- Never commit `.env`, `.claude/`, credentials, SSH keys, tenant IDs, raw tenant payloads, personal names, corporate email addresses, local logs, or anything else enumerated in `memory/feedback_generalize_durable_artifacts.md`. Tenant object IDs are tolerated only in dated `captures/` READMEs; run scripts and raw payloads there stay local (`.gitignore`).

## Cold-start routing — pick the right branch FIRST

```
User task → which branch?
0. Error code in the transcript (8004…, iServerCode -2147…)?
     → memory/reference_strategy_error_codes.md   (fastest path symptom → fix)
1. Building a NEW model?
     → skills/strategy-data-modeling/SKILL.md  (plan grain, dims, conformance, topology)
     → skills/build-mosaic-model/SKILL.md                  (execute via build_mosaic.py)
2. Modifying an existing model / admin / runtime task?
     → skills/strategy-automation/SKILL.md    (NLQ router)
3. Validating numbers / checking data correctness?
     → skills/strategy-validation/SKILL.md
3a. Assessing an existing model with business questions (fitness, gaps, what to fix)?
     → skills/strategy-model-assessment/SKILL.md (findings + change plan)
     → skills/build-mosaic-model/SKILL.md → skills/strategy-validation/SKILL.md
4. Legacy (classic) → Mosaic migration?
     → memory/reference_strategy_legacy_to_mosaic_mining.md (start-here hub)
     → skills/strategy-data-modeling/SKILL.md → skills/build-mosaic-model/SKILL.md
5. Unknown endpoint / unfamiliar payload shape?
     → strategy_api.py ops --search / describe (skills/strategy-platform/SKILL.md) → clone-and-remap (reference_strategy_object_cloning.md)
```

**Skill precedence is strict: classify (automation) → plan (data-modeling) → execute (build) → verify (validation).** No skill routes back up the chain. `strategy-automation` and `strategy-data-modeling` do NOT route to each other in a loop.

**Kimball first on every modeling task.** Strategy's SQL engine is built for star / snowflake schemas and conformed dimensions. Declare topology (`star | snowflake | galaxy | bridge-heavy | non-Kimball`) and classify every table (fact / dim / bridge / snowflake-parent / degenerate / noise) BEFORE writing any payload. Non-Kimball topologies stop-and-confirm — see `memory/reference_data_modeling_foundations.md`.

## First move on any task

1. **Identify the surface.** Strategy concepts are duplicated across Mosaic and classic. Before touching endpoints, decide whether the user is asking about Mosaic data models, classic / project semantic layer, runtime analytics, cubes / datasets, AI agents, platform admin, or data validation. When uncertain, consult `memory/reference_strategy_surface_matrix.md`.
2. **Read the relevant memory file.** `memory/MEMORY.md` is the index — every other file has a `type` frontmatter (`user`, `project`, `feedback`, `reference`) and a one-line description. Load only the files you need.
3. **Use the skills when they fit** (precedence above). Each owns a slice of the REST API (`memory/reference_strategy_api_surface.md` maps every area):
   - `skills/strategy-automation/SKILL.md` — the NLQ router: classifies the request and hands it to the owning skill.
   - `skills/strategy-platform/SKILL.md` — the core: sign-in (`strategy_auth.py`), spec-validated access to **every** REST operation (`strategy_api.py`), MCP client (`strategy_mcp.py`).
   - `skills/strategy-data-modeling/SKILL.md` — the modeling-planning layer: declare business process, grain, attributes, facts, metrics, relationships, hierarchies, time semantics, and validation before build / migration / review work.
   - `skills/build-mosaic-model/SKILL.md` — the `build-mosaic-model` skill (discovery + build + ACL + security filter + publish + post-build edits).
   - `skills/strategy-validation/SKILL.md` — paired-query data-correctness validation against any reference source; Test Center comparisons.
   - `skills/strategy-model-assessment/SKILL.md` — planning-layer review of an existing model against live data: sourced domain research, a graded question bank (levels 1-5) run through MCP, findings and an ordered change plan handed to build and validation.
   - `skills/strategy-admin/SKILL.md` — users, groups, security roles, privileges, SCIM, tenants, license, server and project settings.
   - `skills/strategy-distribution/SKILL.md` — subscriptions, schedules, events, contacts, history list, scheduled model refresh.
   - `skills/strategy-content/SKILL.md` — reports, dashboards, documents, objects, folders, search, certification, cubes, datasets, Library.
   - `skills/strategy-migration/SKILL.md` — packages, migrations, project duplication, Git backup/restore.
   - `skills/strategy-ops/SKILL.md` — monitors, jobs, caches, user connections, project load, telemetry, change journal, server scripts.
   - `skills/strategy-ai/SKILL.md` — agents, bots, questions, MCP readiness, Explorer, ontology.
   - `skills/create-unstructured-data/SKILL.md` — upload documents and slide decks as knowledge for Strategy AI agents.
4. **Use environment configuration, never hardcoded values.** `MSTR_BASE`, `MSTR_PROJECT_ID` or `MSTR_PROJECT_NAME`, `MSTR_DEST_FOLDER_ID`, plus a sign-in method (above). See `.env.example` + `memory/reference_strategy_env.md`.
5. **Ask the tenant's own spec when endpoint details matter.** `python3 skills/strategy-platform/scripts/strategy_api.py ops --search "<words>"` / `describe <operationId>` / `call <operationId>` cover all ~1,700 operations with validation; writes need `--yes`.
6. **Classify automation coverage honestly.** Three layers: (1) every operation is *reachable* and validated through `strategy_api.py call`; (2) a domain skill records the *workflow* (steps, safety, read-back) for common tasks; (3) a typed helper exists only where a workflow is multi-step or risky. Use `memory/reference_strategy_automation_coverage.md` for the levels and the gap register; a validated single call is not a tested workflow. `tests/test_rest_contract.py` fails if a script calls an endpoint the spec does not have.
7. **On any REST failure, grep `memory/reference_strategy_error_codes.md` FIRST.** Every observed `8004cc##` / iServerCode maps to the memory file with the fix. Do not retry blind — all observed codes are class-of-error, not transient.

For Mosaic work, distinguish the entry path:
- **Modeling design / review first:** if the request is still deciding business process, grain, facts, dimensions, relationships, hierarchies, time semantics, or validation scope, route through `skills/strategy-data-modeling/SKILL.md` before touching build helpers.
- **Legacy-to-Mosaic migration:** mine/read the classic semantic layer first, then use its attributes, forms, facts, metrics, relationships, filters, and reports as the blueprint for the new Mosaic model.
- **Brand-new Mosaic model:** start from warehouse discovery plus ERD/data dictionary/preflight checks, then build and validate against the best available comparator.

## Operating rules (apply to every tool)

- **Classify Mosaic vs legacy before every write.** Before hitting any endpoint that differs between the two families (publish, refresh, execute, serve-mode, ACL, security filter), call `GET /api/objects/{id}?type=3` and branch on `subtype` + `extType`: 779 with extType 448 → Mosaic data model; 779 with another extType → data-import (MTDI) cube; 776 → classic Intelligent Cube; anything else → stop and classify further (`classify_object_surface()` does this). See `memory/reference_mosaic_vs_legacy_surfaces.md` for the endpoint-pair cheat sheet. For publishing, `memory/reference_mosaic_publish_path.md` is canonical. The documented Mosaic flow is `POST /api/dataModels/{id}/instances` → `POST /api/dataModels/{id}/publish` → poll `GET /api/dataModels/{id}/publishStatus` until every table is `completed` (`build_mosaic.py publish` also accepts the legacy `loaded`) or one is `error`; `build_mosaic.py publish` does this. `POST /api/cubes/{id}?cubeAction=publish` is internal/deprecated in the spec — a tenant-observed fallback only. Never trust a 202/204 alone; a scoped Trino/MCP query (not a whole-model `COUNT(*)`) is the final proof.
- **Consumer-grade naming is the ship bar.** Any model you build or modify must pass the checklist in `memory/feedback_mosaic_ship_bar.md` — business-named attributes, non-empty form names, business-friendly descriptions, sensible metric formats, no hardcoded example usernames or personal names.
- **Every Mosaic build closes with data validation or an explicit pending note.** Route through `skills/strategy-validation/SKILL.md`; validation is comparator-dependent, and the reference source can be another Mosaic model, a classic project report/model, a flat file, direct warehouse SQL, an external system/API, or a saved REST fixture (NOT Mosaic-to-Mosaic only). If no trusted comparator is available, say validation is pending and do not call the build shippable.
- **Changesets are the unit of write.** Open → mutate → commit, or discard on failure. Relationships / ACLs / translations typically require a separate changeset after object creation.
- **One session per chain.** Steps that share server state (build → publish, a report instance and its pages, a package holder and its import) need one session: `MSTR_REUSE_SESSION=1` / `strategy_api.py call --reuse-session`, or one process; end it with `strategy_auth.py logout`. Logout is `POST /api/auth/logout` — see `memory/feedback_build_mosaic_session_leak.md`.
- **Preserve tenant-verified gotchas.** When a script's endpoint returns 404 or a payload shape changes, update both the script and the corresponding memory file. Never silently work around.
- **Never hardcode credentials, tenant IDs, or personal names.** Pull from env vars; parameterize security filters.
- **Keep every durable artifact generalizable.** Skills, memories, scripts, examples, and templates in this repo must work against *any* Strategy tenant, DB engine, schema, or domain — concrete tenant / DB / user / model values belong in env vars, CLI flags, user-supplied inputs, or `captures/`, never hardcoded into durable text. See `memory/feedback_generalize_durable_artifacts.md` for the scrub checklist and the self-audit grep.
- **Prefer `/usr/bin/python3`** on machines whose Anaconda Python has an older OpenSSL — TLS handshakes to some Strategy Cloud tenants hang otherwise.
- **For destructive operations** (deletes, force patches, migration commits), enumerate the target IDs and confirm before acting unless the user explicitly requested the destruction.

## Repo layout (quick reference)

- `memory/` — durable knowledge, indexed by `memory/MEMORY.md`.
- `tests/` — hermetic suite (`_hermetic.py` first in every module), `fake_tenant.py` (in-process REST stand-in), `live/smoke.py` (read-only live check), `tools/check.py` (CI gates locally), `tools/osv_audit.py` (dependency audit).
- `skills/strategy-platform/` — the core: `strategy_auth.py` (sign-in), `strategy_api.py` (any REST operation, validated against the spec), `strategy_mcp.py` (MCP client), `_client.py`.
- `skills/strategy-{admin,distribution,content,migration,ops,ai}/SKILL.md` — domain skills, one per group of API areas (`memory/reference_strategy_api_surface.md`).
- `skills/build-mosaic-model/` — the build skill: SKILL.md, examples/, and all REST helper CLIs under `scripts/`.
- `skills/strategy-data-modeling/SKILL.md` — modeling-planning layer for grain, dimensions, metrics, relationships, hierarchies, and validation design.
- `skills/strategy-automation/SKILL.md` — NLQ router.
- `skills/strategy-validation/SKILL.md` — data-correctness validator.
- `skills/strategy-model-assessment/` — question-driven model assessment: SKILL.md, `scripts/assess_model.py` (scope, relationships, lookups, run, grade, plan, check), `examples/`.
- `skills/create-unstructured-data/` — unstructured-data (nugget) creation for AI grounding: PPTX→Markdown extractor + multipart upload/status scripts.
- `tests/` — hermetic unit tests (`python3 -m unittest discover -s tests`); run in CI.
- `captures/` — dated tenant-specific transcripts (raw payload files stay local per `.gitignore`).
- `.env.example` — env-var template (copy to `.env`); `pyproject.toml` — dependency declaration + lint config.
- `README.md` — human setup guide, per-AI-tool onboarding.

## Memory index

`memory/MEMORY.md` is the authoritative index — one line per file, grouped by section. Grep or scan it on demand; do not maintain (or trust) a second list of memory files anywhere else, including here. The handful you will reach for constantly:

- `reference_strategy_error_codes.md` — every observed error code → the memory with the fix. Grep FIRST on any 4xx/5xx.
- `reference_strategy_env.md` — env-var + CLI-flag convention.
- `reference_strategy_surface_matrix.md` — route ambiguous nouns to the right surface (Mosaic vs classic vs runtime).
- `reference_data_modeling_foundations.md` — Kimball foundations backing every modeling decision.
- `reference_strategy_automation_coverage.md` — coverage levels and the gap register.
- `feedback_generalize_durable_artifacts.md` — the scrub checklist for anything you write back into the repo.

## MCP tools (configured separately per AI tool)

Each AI tool configures MCP servers through its own settings — this repo does NOT ship MCP server configuration. When a correctly-configured Mosaic MCP session exists, the following tool names are available and referenced by memory/skills:

- `get_projects` — list projects in the connected catalog.
- `get_models` (older servers: `get_mosaic_models`) — list **certified** content only: Mosaic models (`Mosaic Model`) and governed classic cubes, reports and datasets (`Other Model`); a published but uncertified model does not appear.
- `get_semantics` — return the annotated attribute/metric surface for a Mosaic model. Name lookup fails until the model is certified.
- `query` — execute a Trino-compatible SQL query against the published Mosaic layer.

The memory writes say "MCP" — don't hunt for a server-id prefix. If your tool exposes these four tool names under any namespace, you're good. Each tenant runs two MCP servers: Mosaic (`{host}/collaboration/mcp/mosaic`, these tools) and Agents (`{host}/collaboration/mcp/agent`). Scripts can reach either with the same single sign-on through `skills/strategy-platform/scripts/strategy_mcp.py` (OAuth, as the MCP connector does; `--server mosaic|agent`).

**If your harness has no MCP support**, every MCP tool has a REST fallback:
- `get_projects` → `GET /api/projects`
- `get_models` → `GET /api/searches/results?type=3&certifiedStatus=CERTIFIED_ONLY` (then keep subtype 779 + extType 448 for Mosaic models)
- `get_semantics` → `GET /api/model/dataModels/{id}/attributes` + `/factMetrics` + `/metrics`
- `query` → direct Trino HTTPS connection (host `<tenant>:443`, catalog `sql`, schema `<project-name-lower>`, basic auth with a Strategy password), or the cube-instance API for result sets: `POST /api/v2/cubes/{id}/instances` → `GET /api/v2/cubes/{id}/instances/{instanceId}` (a `POST /api/dataModels/{id}/instances` is a publish instance and returns no data).

## Running under specific LLM harnesses

All harnesses follow the same contract: read this file, load `memory/MEMORY.md` on demand, invoke `skills/strategy-platform/scripts/strategy_api.py`, `skills/build-mosaic-model/scripts/build_mosaic.py` and their siblings via whatever shell/tool-call mechanism is available. Harness-specific notes live in the root shim for that harness (`CLAUDE.md`, `CODEX.md`, `GEMINI.md`, `GROK.md`, `OLLAMA.md`, `CURSOR.md`) — one file per harness, maintained there only. Any harness without a shim needs no configuration: read this file plus the memory index and follow the routing.

Note for skill-aware harnesses (Claude Code included): the `SKILL.md` files here are read on demand via the routing above — they are NOT auto-discovered project skills unless you link them under `.claude/skills/` or install them as a plugin. Prefer a symlink to a copy: a copied `SKILL.md` (and any scripts copied with it) goes stale when this repo changes.
