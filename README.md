# strategy-automate

Skills, field-verified notes and Python helpers that let AI coding agents automate **Strategy** (formerly MicroStrategy) through its REST API and Mosaic MCP server.

- **Build Mosaic semantic models** from warehouse tables: plan a star schema, create tables, attributes and metrics, wire relationships, set ACLs and security filters, then publish and certify (`build_mosaic.py`).
- **Check the result** with a structural gate (`validate-model`) and paired queries against a trusted reference (`strategy_validate_models.py`).
- **Migrate and administer classic projects**: inventory schema objects, trace reports to tables, convert classic objects to Mosaic, replay Library publications, upload documents for AI agents.
- **Reach every REST operation safely**: list, describe and call any of the ~1,700 operations in the tenant's own OpenAPI spec, validated before sending, with domain skills for admin, distribution, content, migration, operations and AI work (`strategy_api.py`).
- **Sign in the way your tenant does**: single sign-on through the browser (SAML, OIDC, any IdP), API tokens, LDAP or standard passwords — no password needed for SSO accounts (`strategy_auth.py`, `strategy_mcp.py`).
- **Reuse what was learned**: dated, tenant-verified notes on endpoints, payloads and error codes, indexed in [`memory/MEMORY.md`](memory/MEMORY.md).

Tested with **Claude Code** and **Codex CLI**; any harness that reads Markdown can use it — every per-LLM shim at the repo root points at [`AGENTS.md`](AGENTS.md).

**Kimball-first by default.** Strategy's SQL engine is built for star / snowflake / galaxy schemas with conformed dimensions. Every modeling workflow declares the topology (`star | snowflake | galaxy | bridge-heavy | non-Kimball`) and classifies each input table (`fact | dim | bridge | snowflake_parent_dim | degenerate_dim | noise`) before writing any payload; non-Kimball shapes stop and ask.

## How agents route work

The cold-start routing tree, the strict skill-precedence chain, and all operating rules live in [`AGENTS.md`](AGENTS.md) — maintained there only. In one line: `strategy-automation` classifies the surface and hands admin, distribution, content, migration, ops and AI work to its domain skill; for models, `strategy-data-modeling` plans, `skills/build-mosaic-model/SKILL.md` executes via REST and `strategy-validation` verifies the numbers; `strategy_api.py` reaches any other operation; and on any Strategy error code the first stop is [`memory/reference_strategy_error_codes.md`](memory/reference_strategy_error_codes.md).

## Repo layout

```
strategy-automate/
├── AGENTS.md                      # canonical LLM-agnostic entry point
├── CLAUDE.md CODEX.md GEMINI.md   # thin per-LLM shims (all point to AGENTS.md)
├── GROK.md OLLAMA.md CURSOR.md    # additional harness shims (+ .cursor/rules/)
├── README.md .env.example         # human setup + env-var template
├── memory/                        # durable knowledge, indexed by MEMORY.md
│   ├── MEMORY.md                  # flat index — grep or scan to find the right file
│   ├── reference_strategy_authentication.md # every sign-in method, incl. SSO without passwords
│   ├── reference_strategy_error_codes.md    # error code → memory with the fix
│   ├── reference_data_modeling_foundations.md  # Kimball foundations (all design sections)
│   ├── reference_mosaic_*.md                # Mosaic payload shapes, publish, ACL, SF
│   ├── reference_strategy_*.md              # surface matrix, env, OpenAPI, legacy, etc.
│   ├── feedback_*.md                        # durable fixes learned from failures
│   └── checklist_*.md                       # modeling playbook + review gate
├── skills/
│   ├── strategy-platform/         # the core every other skill uses
│   │   ├── SKILL.md
│   │   └── scripts/
│   │       ├── strategy_auth.py         # sign-in: password, LDAP, API token, browser SSO, identity token, OIDC
│   │       ├── strategy_api.py          # list / describe / call ANY REST operation, validated against the tenant spec
│   │       ├── strategy_mcp.py          # Mosaic + Agent MCP client with the MCP connector's OAuth sign-in
│   │       └── _client.py               # shared BaseMSTR, auth args, search, inventory helpers
│   ├── strategy-automation/SKILL.md      # NLQ router: classifies a request, hands it to the owning skill
│   ├── strategy-admin/SKILL.md           # users, groups, security roles, privileges, SCIM, tenants, settings
│   ├── strategy-distribution/SKILL.md    # subscriptions, schedules, events, contacts, history list
│   ├── strategy-content/SKILL.md         # reports, dashboards, documents, objects, search, cubes, datasets
│   ├── strategy-migration/SKILL.md       # packages, migrations, project duplication, Git
│   ├── strategy-ops/SKILL.md             # monitors, jobs, caches, connections, telemetry, server scripts
│   ├── strategy-ai/SKILL.md              # agents, bots, questions, MCP readiness
│   ├── build-mosaic-model/        # Mosaic modeling execution skill
│   │   ├── SKILL.md
│   │   ├── examples/              # model / attribute / relationship / validation plan templates
│   │   └── scripts/
│   │       ├── build_mosaic.py          # CLI: catalog, build, publish, relationships, SF, ACL, certify, validate, … (see --help)
│   │       ├── mosaic_safety.py         # stateless defensive helpers (error parsing, expression builders, merge-aware PUT)
│   │       ├── preflight_model_check.py
│   │       ├── schema_object_translator.py    # classic schema objects → Mosaic payloads
│   │       ├── strategy_mosaic_inventory.py   # walk subtype-779 models (Mosaic = extType 448)
│   │       ├── strategy_semantic_inventory.py # walk classic attrs / facts / metrics / filters / hierarchies
│   │       ├── strategy_semantic_mine.py      # top-down / reverse lineage for legacy → Mosaic
│   │       ├── strategy_library_publications{,_mstrio}.py # export + replay Library publications
│   │       ├── strategy_validate_models.py    # file-adapter + live Mosaic-to-Mosaic Trino diff
│   │       └── strategy_validate.py           # live-tenant runtime-workflow validator
│   ├── create-unstructured-data/  # upload documents / decks as AI-agent knowledge
│   ├── strategy-data-modeling/SKILL.md   # Kimball-first planning layer
│   ├── strategy-model-assessment/        # question-driven assessment of a live model → change plan (assess_model.py)
│   └── strategy-validation/SKILL.md      # paired-query numeric-correctness validator, Test Center
├── tests/                         # hermetic unit tests + REST contract test (run in CI)
├── captures/                      # dated tenant transcripts; raw payloads stay local
├── pyproject.toml uv.lock LICENSE # deps + lint config; MIT
└── .github/workflows/tests.yml    # CI: unittest on 3.9–3.14 + macOS, ruff, mypy, bandit, coverage
```

## Setup

### 1. Clone + configure env

```bash
git clone <this-repo> strategy-automate
cd strategy-automate
cp .env.example .env
# edit .env with your Library URL, project name/ID, dest folder (and credentials, if you use a password)
set -a; source .env; set +a
```

Required: `MSTR_BASE` and either `MSTR_PROJECT_ID` or `MSTR_PROJECT_NAME`. For building new Mosaic models, also set `MSTR_DEST_FOLDER_ID`. Full env-var list in [`memory/reference_strategy_env.md`](memory/reference_strategy_env.md).

**Sign-in.** Every script takes `--auth-method` (env `MSTR_AUTH_METHOD`, default `auto`):

| Your account | What to set |
|---|---|
| Single sign-on (SAML / OIDC / any IdP), no Strategy password | nothing — run `python3 skills/strategy-platform/scripts/strategy_auth.py login` once; it opens your browser, you click **Allow**, and later commands reuse the session from the OS keychain |
| SSO, but runs must not open a browser | `strategy_auth.py login --save-api-token --lifetime-minutes 480` |
| Standard or LDAP account | `MSTR_USER` + `MSTR_PASSWORD` (+ `MSTR_LOGIN_MODE=16` for LDAP) |
| CI / service account | `MSTR_API_TOKEN` |

`strategy_auth.py methods` shows what your tenant enables and what this machine would use. Details, the exact REST contracts and the tenant settings each method needs: [`memory/reference_strategy_authentication.md`](memory/reference_strategy_authentication.md).

### 2. Install Python deps

```bash
uv sync                                       # recommended: the locked versions, in .venv
uv run python3 skills/strategy-platform/scripts/strategy_auth.py methods
```

or, without uv, `python3 -m pip install --user "requests>=2.32.5"`.

`requests` (with its `urllib3`) is the only non-stdlib dependency, declared in [`pyproject.toml`](pyproject.toml) and pinned in `uv.lock` (dependency declaration only — scripts stay directly runnable, no install required). Optionally add `PyYAML` for YAML configs (used by `build-from-config` and other `--file *.yaml` inputs); without it the scripts fall back to a `ruby -ryaml` one-liner for YAML parsing. Prefer Python 3.10 or newer: the current security fixes in requests (2.33+) and urllib3 (2.8+) don't install on 3.9, which still works with the advisories reviewed in `tests/tools/osv_audit.py`. `strategy_auth.py methods` warns when the Python it runs under has older HTTP libraries. If your default Python is Anaconda and you see SSL-handshake timeouts against your tenant, use `uv sync` / `uv run` (or `/usr/bin/python3`) — some Anaconda builds ship an old OpenSSL that hangs on Strategy Cloud TLS.

### 3. Verify tenant connectivity

```bash
python3 skills/strategy-platform/scripts/strategy_auth.py methods
python3 skills/build-mosaic-model/scripts/build_mosaic.py auth-probe
python3 skills/build-mosaic-model/scripts/build_mosaic.py list-datasources
```

### 4. Wire the repo into your AI tool

The repo is **LLM-agnostic**. [`AGENTS.md`](AGENTS.md) is the canonical entry point; every shim at the repo root points there.

| Harness | Entry file | Notes |
|---|---|---|
| Claude Code | [`CLAUDE.md`](CLAUDE.md) | `CLAUDE.md` is auto-loaded and routes to `AGENTS.md`; skills + memory are read on demand. |
| OpenAI Codex CLI | [`CODEX.md`](CODEX.md) | Reads `AGENTS.md` on `cd`; skills loaded on demand. |
| Google Gemini CLI | [`GEMINI.md`](GEMINI.md) | Same contract as Codex. |
| xAI Grok / Grok Code | [`GROK.md`](GROK.md) | Each `SKILL.md` treated as long-form instruction. |
| Ollama local models | [`OLLAMA.md`](OLLAMA.md) | Bootstrap system-prompt template for harnesses that don't auto-load Markdown. |
| Cursor / Cline / Continue / Aider / Windsurf | [`CURSOR.md`](CURSOR.md) | Point IDE agent at `AGENTS.md` as the rules file. |
| Any other LLM | [`AGENTS.md`](AGENTS.md) | No configuration needed — read the file + memory index and proceed. |

**MCP-aware chat apps** — connect the Strategy Mosaic MCP server (per your vendor's connector). The memory and skills reference MCP tools by standard name: `get_projects`, `get_models` (older servers: `get_mosaic_models`), `get_semantics`, `query`; the server lists and resolves **certified** content only — Mosaic models plus governed classic cubes, reports and datasets. A tenant also runs a separate Agent MCP server. Scripts can call either with the same single sign-on through [`strategy_mcp.py`](skills/strategy-platform/scripts/strategy_mcp.py). Without MCP, every tool has a REST fallback documented in [`AGENTS.md`](AGENTS.md).

## Typical tasks

### Build a new Mosaic model from warehouse tables

```
Build a mosaic model. Instance: <your datasource name>
Schema: <schema>
Tables: <T1>, <T2>, <T3>
```

For multi-DB builds (e.g., Postgres + Snowflake), route through [`skills/strategy-data-modeling/SKILL.md`](skills/strategy-data-modeling/SKILL.md) first — declare conformed dims, classify tables, pick the topology before hitting REST. Case-mismatch FKs (`<entity>_id` vs `<ENTITY>_ID`) and semantically-same-but-differently-named FKs (`primary_<entity>_id` vs `<entity>_id`) silently break auto-conformance unless you pass `--conformance-map` or `--fk-map` to `build`. See [`memory/feedback_mosaic_relationship_wiring.md`](memory/feedback_mosaic_relationship_wiring.md) for the six-step recipe.

End-to-end chain in one session — one Python process, or separate commands with `MSTR_REUSE_SESSION=1` (avoids session-cap trips — see [`memory/feedback_build_mosaic_session_leak.md`](memory/feedback_build_mosaic_session_leak.md)):

```bash
python3 skills/build-mosaic-model/scripts/build_mosaic.py build-from-config --config model-spec.yaml
# or
python3 skills/build-mosaic-model/scripts/build_mosaic.py build \
  --name "Sales Mosaic" \
  --source "Snowflake Prod:SALES:CUSTOMER,ORDER,LINEITEM" \
  --dictionary /tmp/sales.dict.json \
  --conformance-map /tmp/sales.conformance.json
```

### Post-build relationship wiring with pre-flight validation

For multi-DB or mixed-case builds where auto-conformance leaves orphan FKs:

```bash
python3 skills/build-mosaic-model/scripts/build_mosaic.py wire-relationships \
  --model-id <model_id> \
  --hints /tmp/fk-hints.json \
  --dry-run
```

Validates step-3 (self-reference → `8004ccdb`) and step-5 (relationship_table prerequisite → `8004ccc7`) before issuing any PUT; skips PUTs that would fail and reports which ones need attribute merges first.

### Publish an in-memory Mosaic model (same process as build)

```bash
python3 skills/build-mosaic-model/scripts/build_mosaic.py publish --model-id <model_id> --skip-classify
```

`--skip-classify` bypasses the `GET /api/objects/{id}?type=3` surface check when you already know the target is a Mosaic model — saves one project-scoped call against the session cap when chaining build → publish.

### Validate a model's numbers

File adapter (dump rows from any comparator, then diff):

```bash
python3 skills/build-mosaic-model/scripts/strategy_validate_models.py \
  --model-file /tmp/model_rows.csv \
  --reference-file /tmp/reference_rows.csv \
  --key region,nation --measures revenue,orders \
  --out /tmp/validation.json
```

Live Mosaic-to-Mosaic (via Trino, no external files):

```bash
python3 skills/build-mosaic-model/scripts/strategy_validate_models.py \
  --model "<new_model>" --reference-mosaic "<reference_model>" \
  --query 'SELECT "region (region name)", SUM("revenue") FROM %s GROUP BY 1' \
  --key 'region (region name)' --measures revenue \
  --out /tmp/validation.json
```

See [`skills/strategy-validation/SKILL.md`](skills/strategy-validation/SKILL.md) and [`memory/reference_strategy_data_validation.md`](memory/reference_strategy_data_validation.md) for the 5-query minimum suite, the 10-check design suite, comparator-source decision matrix, and failure-triage mapped to Kimball root causes.

### Inspect every Mosaic model in a project

```bash
python3 skills/build-mosaic-model/scripts/strategy_mosaic_inventory.py --workers 12
```

Writes structured JSON to a new private (0600) temp file unless `--out` is given. Portfolio rollups + per-model attributes, metrics, relationships, security filters, external-data-model links.

### Mine a classic project for Mosaic candidates

```bash
python3 skills/build-mosaic-model/scripts/strategy_semantic_mine.py --mode top-down --report "Revenue Report"
python3 skills/build-mosaic-model/scripts/strategy_semantic_mine.py --mode reverse --table LU_PRODUCT
```

See [`memory/reference_strategy_legacy_to_mosaic_mining.md`](memory/reference_strategy_legacy_to_mosaic_mining.md) — it's the start-here hub for classic → Mosaic migrations (4-step sequence: mining → field-study → blueprint/clone decision → build).

### Automate anything else — admin, subscriptions, reports, migrations, monitoring, agents

Every one of the ~1,700 REST operations is reachable through one spec-validated tool, and each area has an owning skill with its workflows (`memory/reference_strategy_api_surface.md` maps them):

```bash
API="python3 skills/strategy-platform/scripts/strategy_api.py"
$API tags                                   # every API area and the skill that owns it
$API ops --tag strategy-distribution        # e.g. everything about subscriptions and schedules
$API describe createSubscription            # parameters, auto-filled headers, body skeleton
$API call createSubscription --body @sub.json          # dry run: prints the request
$API call createSubscription --body @sub.json --yes    # sends it
```

`call` validates parameters and body against the tenant's spec, fills the project / changeset / `Prefer` headers, and refuses to send a write without `--yes`. Promote a workflow to a typed helper when it becomes common, risky, multi-step, or needs strict verification or cleanup.

## Memory, conventions, and security

Durable knowledge lives in `memory/` — [`memory/MEMORY.md`](memory/MEMORY.md) is the one-line-per-file index, and each file carries `type:` frontmatter (`user` / `project` / `reference` / `feedback`). The operating rules agents follow — Kimball-first planning, changesets as the unit of write, one session per chain, error-code-grep-first, the consumer-grade-naming ship bar, and the generalization/scrub rules — are maintained in [`AGENTS.md`](AGENTS.md) → "Operating rules", not here.

Security posture for humans: no hardcoded credentials, tenant IDs, real company or person names, or industry-specific content anywhere in durable text (`.env` is gitignored, `.env.example` is the template); raw tenant payloads go to private temp files or `captures/<date>-<topic>/`, never into memory files. Secrets come from environment variables or the OS keychain — never command-line flags, which other local processes can read. See [`memory/feedback_generalize_durable_artifacts.md`](memory/feedback_generalize_durable_artifacts.md) for the scrub checklist.

## Testing and CI

The source-SQL helpers reject identifiers/literals whose backslashes or control characters depend on a warehouse's escape mode, and catalog paging accepts a nonnegative integer page and a bounded integer page size. Generated-SQL sites carry individually reviewed B608 annotations; the Bandit rule stays enabled globally and the escaping boundaries have adversarial tests.

The suite is offline and hermetic: no tenant, no network beyond loopback, no Keychain.

```bash
python3 -m unittest discover -s tests          # the whole suite (529 tests, about 15 s)
python3 tests/tools/check.py                   # the CI gates: tests, ruff, mypy, bandit, coverage
python3 tests/tools/check.py --online deps     # OSV audit of every package in uv.lock
python3 tests/live/smoke.py --project "<p>"    # read-only checks against YOUR tenant (signs in with auto)
```

| Layer | What it proves | Where |
|---|---|---|
| Unit | parsing, validation and payload building, module by module | `tests/test_*.py` |
| Fake tenant | the real CLIs end to end against an in-process Strategy REST stand-in with fault injection: every sign-in logged out, request budgets, changesets committed or discarded on every failure path, secrets never printed, transient 503s absorbed | `tests/fake_tenant.py`, `tests/test_integration_fake_tenant.py` |
| Transport | retries only where repeating is safe, default timeouts, cross-origin redirects refused — on real sockets | `tests/test_transport_resilience.py` |
| Contract | every REST call written in a script exists in the spec (catch-all routes don't count); every skill cites real operationIds | `tests/test_rest_contract.py`, `tests/test_skill_operations.py` |
| Hygiene | no secrets, home paths, e-mail addresses or real tenant hosts in tracked files; every script's `--help` runs | `tests/test_repo_hygiene.py`, `tests/test_script_help.py` |
| Budgets | complexity regressions in spec indexing, lookups and parsers | `tests/test_performance_budgets.py` |
| Live smoke | sign-in, session, projects, spec drift + contract against the live spec, a validated read, a search, MCP discovery, logout really ends the session | `tests/live/smoke.py` (self-tested against the fake tenant) |

`tests/_hermetic.py`, imported first by every test module, removes `MSTR_*` and proxy variables and points the secret store at a throw-away folder, so the suite behaves the same on every machine and never touches your Keychain.

GitHub Actions (`.github/workflows/`):
- **tests** — the suite on Python 3.9, 3.10, 3.12, 3.13 and 3.14, macOS, and macOS's own `/usr/bin/python3`, with dependencies installed hash-checked from `uv.lock`; once more under a hostile environment; then ruff, mypy, bandit and the platform-core coverage gate. Runs on every push and weekly.
- **security** — OSV audit of every locked package, weekly and whenever the lock changes. Accepted advisories are reviewed and dated in `tests/tools/osv_audit.py`.
- **live-smoke** — manual only, behind a `strategy-live` environment with required reviewers and a read-only service account's API token (setup in the workflow file). It prints step names and timings, never hosts, names or tokens.

## Contributing

1. **New endpoint or workflow** → prove the hook with `strategy_api.py ops --search` / `describe` and a read-only `call`; record the workflow in the owning skill's `SKILL.md`; write a typed helper in that skill's `scripts/` only when it deserves one ([`skills/strategy-platform/SKILL.md`](skills/strategy-platform/SKILL.md), "When to write a typed helper"). Mosaic build helpers are subcommands of [`skills/build-mosaic-model/scripts/build_mosaic.py`](skills/build-mosaic-model/scripts/build_mosaic.py), indexed in [`memory/reference_mosaic_build_skill.md`](memory/reference_mosaic_build_skill.md).
2. **New durable knowledge** → add a memory file with `name` / `description` / `type` frontmatter, then point to it from [`memory/MEMORY.md`](memory/MEMORY.md). Cite code by function/subcommand name, never line numbers. New error codes MUST add a row to [`memory/reference_strategy_error_codes.md`](memory/reference_strategy_error_codes.md).
3. **New platform surface or known gap** → update [`memory/reference_strategy_automation_coverage.md`](memory/reference_strategy_automation_coverage.md) and [`memory/reference_strategy_task_catalog.md`](memory/reference_strategy_task_catalog.md).
4. **New skill surface** → new directory under `skills/` with a `SKILL.md`; add routing in [`skills/strategy-automation/SKILL.md`](skills/strategy-automation/SKILL.md) so other sessions find it. Skills must stay one-way (classify → plan → build → verify).
5. **Dated tenant-specific content** goes under `captures/<YYYY-MM-DD>-<topic>/` (older folders use `<YYYYMMDD>-<topic>`), not in memory.
6. **Do not commit** tenant IDs, usernames, passwords, personal names, or industry-specific terminology (see generalization rule above).

## License

MIT — see [LICENSE](LICENSE).
