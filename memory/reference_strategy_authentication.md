---
name: Strategy authentication for automation — every sign-in mechanism, verified contracts, SSO without passwords
description: How scripts sign in to Strategy REST and the Mosaic MCP server. Login-mode numbers, the exact request contracts (verified 2026-10-05 against a Strategy ONE cloud tenant's OpenAPI spec and mstrio-py master), the browser single-sign-on handoff in strategy_auth.py, the MCP OAuth flow in strategy_mcp.py, API tokens, logout, and the tenant settings each method needs.
type: reference
tags: [auth, sso, saml, oidc, api-token, identity-token, oauth, mcp, logout, session-cap]
---

## Which method to use

| Situation | Method | Tool |
|---|---|---|
| Tenant signs users in through SAML/OIDC/any IdP and you have no Strategy password | `sso` (browser handoff) | `strategy_auth.py login --method sso`, then any script with `--auth-method auto` |
| Same, but runs must not open a browser each time | `sso` once, then `--save-api-token` | API token in the OS secret store; scripts use loginMode 4096 |
| Standard or LDAP account with a password | `password` / `ldap` | `MSTR_USER` + `MSTR_PASSWORD` (+ `MSTR_LOGIN_MODE=16` for LDAP) |
| CI / service account | `api-token` | `MSTR_API_TOKEN` from the CI secret store |
| Another app already holds a session | `identity-token` | `MSTR_DELEGATE_IDENTITY_TOKEN` |
| Governed SQL or semantics for an SSO-only account (no REST) | MCP OAuth | `strategy_mcp.py login`, then `query` / `call` |
| Tenant with OIDC login mode and a native client that allows a loopback redirect | `oidc` (not yet exercised live) | `--auth-method oidc` |
| Nothing else works | borrowed session | `MSTR_AUTH_TOKEN` + `MSTR_SESSION_COOKIE` (+ `MSTR_INGRESS_COOKIE`) from DevTools |

`--auth-method auto` (the default everywhere) picks: `MSTR_API_TOKEN` → `MSTR_USER`+`MSTR_PASSWORD` → `MSTR_DELEGATE_IDENTITY_TOKEN` → cached browser session → saved API token → `sso`. Run `python3 skills/strategy-platform/scripts/strategy_auth.py methods` to see what a tenant enables and what this machine would use.

## Login modes (`loginMode`, EnumDSSXMLAuthModes)

| Mode | Name | How a script uses it |
|---|---|---|
| 1 | Standard | `POST /api/auth/login {username, password, loginMode: 1}` |
| 8 | Anonymous (guest) | `{loginMode: 8}` — only when the tenant enables it |
| 16 | LDAP | `{username, password, loginMode: 16}` |
| 4096 | API token | `{loginMode: 4096, username: "<api token>"}` — the token goes in **`username`** (mstrio-py master does the same) |
| 1048576 | SAML | browser only → use the `sso` handoff |
| 4194304 | OIDC | browser, or `POST /api/auth/oidc/token` with IdP tokens (`oidc` method) |
| -1 | identity token | `POST /api/auth/delegate` only |

`GET /api/config/authModes` (no auth) lists the enabled modes, e.g. `{"modes": [1048576, 1]}` = SAML + Standard. Older notes in this repo said "8 LDAP, 16 SAML, 4096 identity token" — wrong; corrected 2026-10-05.

## Verified contracts

- `POST /api/auth/login` → `204` + `X-MSTR-AuthToken` header + session cookies. Every later call needs the token **and** the cookies (multi-node tenants route by cookie).
- `POST /api/auth/logout` is the **only** logout. `DELETE /api/auth/login` does not exist (404). Until 2026-10-05 the scripts here used it, so every run left its session open until the idle timeout — the real cause of most "maximum number of interactive sessions" (8004cb0a) hits in `feedback_build_mosaic_session_leak.md`.
- `GET /api/sessions` → `200` while the session lives, `401 ERR009` after. Cheap liveness probe; not project-scoped.
- `POST /api/auth/apiTokens {lifeTimeInMinutes?, userId?}` → `201 {apiToken, createTime, expireTime}`. A user has **one** API token: creating a new one replaces it. `GET` returns its metadata (no secret); `DELETE` revokes it and ends every session that used it.
- `POST /api/auth/identityToken` → identity token in the `X-MSTR-IdentityToken` header. `POST /api/v2/auth/identityToken {codeChallenge, codeChallengeMethod: "S256"}` → `201 {identityToken}` in the body, bound to a PKCE challenge.
- `POST /api/auth/delegate {loginMode: -1, identityToken, codeVerifier?}` → `204` + a new web session that **shares the IServer session** of the session that minted the token. Never log a delegated session out from a script — it ends the original (browser) session too.
- `GET /api/auth/token` turns the browser's `iSession` cookie into an `X-MSTR-AuthToken` header. Browser-only (needs the cookie).
- `POST /api/auth/oidc/token {access_token, id_token, refresh_token?, token_type, expires_in}` (query `clientId`) → session, when OIDC mode 4194304 is on. `GET /api/config/oidc/native` → `{iams: [{issuer, clientId, nativeClientId, scopes, ...}]}` or `ERR001 OIDC not available`.
- The Modeling identity token (`X-MSTR-IdentityToken` header on Mosaic writes) is a separate concern: see `feedback_mosaic_identity_token_privilege_downgrade.md`.

## Browser single sign-on handoff (`strategy_auth.py`, method `sso`)

1. The script makes a PKCE verifier/challenge and a random `state`, and serves one page on `http://127.0.0.1:8753/` (`MSTR_SSO_PORT`).
2. The page calls `GET {base}/api/auth/token` with the browser's cookies. No session → a "Sign in with SSO" button opens `{base}/app` in a popup (the tenant's own IdP flow) and polls.
3. With a session the page shows the user's name and asks **Allow / Cancel**. Nothing happens without the click.
4. On Allow: `POST {base}/api/v2/auth/identityToken {codeChallenge}` → identity token → `POST /callback` on the loopback page (same origin, with `state`).
5. The script calls `POST /api/auth/delegate {identityToken, codeVerifier}` and has its own session. A stolen identity token is useless without the verifier, which never leaves the script.

**Session reuse for scripts with a password or API token:** `MSTR_REUSE_SESSION=1` (honoured by every script that signs in through `strategy_auth.py` — `build_mosaic.py`, the `_client.py` scripts, `strategy_api.py` — or `strategy_api.py call --reuse-session`) caches the session in the same secret store, one slot per tenant and identity, and skips the logout, so a chain of commands shares one session (fewer sign-ins, and instances/jobs survive between steps). `strategy_auth.py logout` ends and forgets it.

Guards: loopback-only bind, `Host` and `Origin` checks (DNS rebinding), constant-time `state` check, nonce CSP, no token in the URL, browser storage or logs. The delegated session is cached in the OS secret store (macOS Keychain via `security -i` on stdin, libsecret `secret-tool`, else a `0600` file) so later commands reuse it; `strategy_auth.py logout` ends and forgets it.

Tenant requirements: CORS must allow the page origin (`http://127.0.0.1:8753`) with credentials and expose `X-MSTR-AuthToken`; the Library cookie must be `SameSite=None; Secure`. Browsers that block third-party cookies (Safari by default, Firefox's Total Cookie Protection, any incognito window) can't see the Library session from the page: use Chrome/Edge (`MSTR_SSO_BROWSER=chrome`), or sign in once and `--save-api-token`. Web apps can use the same mechanism directly: call `GET {base}/api/auth/token` with `credentials: 'include'` on load, open `{base}/app` in a popup when it answers 401 and poll, keep the token in memory only (never storage, URLs or logs), and clear caches on sign-out. The same CORS and cookie requirements apply.

**Tenant CORS hygiene.** A Library that echoes any `Origin` back with `Access-Control-Allow-Credentials: true` lets any website a signed-in user visits read their session token. `strategy_auth.py methods` checks for it and warns; the fix is an explicit origin allowlist on the tenant.

## Mosaic MCP server OAuth (`strategy_mcp.py`) — how the MCP connector signs in

- Two MCP servers: **Mosaic** at `{host}/collaboration/mcp/mosaic` (models, plus certified classic cubes / reports / datasets) and **Agents** at `{host}/collaboration/mcp/agent`. An unauthenticated call to either answers `401` with `WWW-Authenticate: Bearer resource_metadata="{host}/collaboration/.well-known/oauth-protected-resource/mcp/<server>"`. The host-root `/.well-known/oauth-protected-resource` names only the Agent server, so `strategy_mcp.py` targets Mosaic explicitly (`--server agent` for the other).
- Authorization server `{host}/collaboration`: authorization code + PKCE S256, `refresh_token`, **dynamic client registration** (`/register`), `client_secret_post`, scopes `openid profile email offline_access mcp:stream`, revocation endpoint. The script registers itself once (redirect `http://127.0.0.1:8753/oauth/callback`), sends the `resource` indicator, and keeps client + tokens in the secret store.
- MCP transport: streamable HTTP JSON-RPC (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`), `Mcp-Session-Id` and `MCP-Protocol-Version` headers, replies as JSON or `text/event-stream`. Tools: `get_projects`, `get_models` (certified content only — Mosaic models plus classic `Other Model` cubes / reports / datasets), `get_semantics`, `query {project, query}`.
- Each token is for one MCP resource only: Strategy REST ignores `Authorization: Bearer` (same `401 ERR009` with or without it, verified 2026-10-05). REST scripts use the `sso` handoff instead — same IdP, same user.
- Mix-up defences (2026-10-05 review): the advertised `resource` must be on the same origin as the MCP URL asked for, a `WWW-Authenticate` metadata pointer must be same-origin too (parsed, not a string prefix), authorization-server endpoints must be https, and the stored client + tokens are bound to the issuer that minted them — a look-alike server naming the real resource gets a fresh browser login, never the stored refresh token or client secret. Tokens are keyed by the MCP URL asked for. SSE replies are decoded as UTF-8 (servers omit the charset), and only a JSON-RPC *response* with the request's id counts as the reply.

## Session and transport rules (all scripts)

- Every request goes through `strategy_auth.SafeSession`: a redirect to another origin is refused outright (a 307 would resend a password or identity token, and dict cookies would follow), 15 s / `MSTR_HTTP_TIMEOUT` timeouts, `MSTR_HTTP_RETRIES` retries for connection failures and GET/HEAD 502/503/504 only. `check_base` refuses plain http (localhost aside), credentials in the URL and host names with characters that could break a header or the consent page's CSP.
- `MSTR_REUSE_SESSION=1` slots are per user (password) or per API token (a hash of it, never the token), so a second identity never inherits the first one's session; `strategy_auth.py logout` ends them.
- A cached session is forgotten only when `GET /api/sessions` says 401/403; a 5xx or network blip keeps it for the next command.
- An explicit `--auth-method sso` / `MSTR_AUTH_METHOD=sso` reuses a cached browser session too (before 2026-11-18 only
  `auto` read the cache, so every command opened the browser; seen on Windows, where each step then waited for a new
  Allow). `MSTR_SESSION_CACHE=0` forces a fresh sign-in. A refused cached session is now reported ("[auth] the cached
  session was refused (HTTP n, saved m min ago)"), so a run shows whether the cache was missing or refused.
  `strategy_auth.py methods` reports `configured_method` and, under `auto_would_use`, "cached" only when a session is
  cached (it said "cached" whenever caching was on).
- `auto` never falls through to a browser sign-in where nobody can answer it (`CI`, `MSTR_NO_BROWSER=1`, Linux without a display) — it fails fast naming `MSTR_API_TOKEN` / `MSTR_USER` + `MSTR_PASSWORD`. `MSTR_LOGIN_MODE=4096` with the token in `MSTR_USER` still works.
- The file secret store (no Keychain / libsecret) is locked across read-modify-write, so parallel commands don't drop each other's entries.

## Library OAuth2 authorization server (Strategy ONE, Managed Cloud Enterprise)

**Live finding 2026-10-07 (Strategy ONE Cloud tenant, SAML + Standard):** the `sso` cookie handoff does not work here. A signed-in browser gets `GET /api/auth/token` → `401 ERR003 "Access token not provided"` even first-party: the web app's session is an OAuth access token held by the app, not a cookie session. With `Authorization: Bearer <anything>` the same call answers `ERR009` instead, i.e. the endpoint exchanges a Library OAuth access token for an `X-MSTR-AuthToken`. The tenant's OpenID metadata offers authorization code + PKCE, `token_endpoint_auth_method none`, refresh tokens — but no dynamic registration. Next step: an admin registers a NATIVE client (public, PKCE, redirect `http://127.0.0.1:8753/oauth/callback`) under `/api/mstrServices/library/oauth2/settings`; scripts then do code + PKCE → access token → `GET /api/auth/token` with the bearer. Until then use a saved API token (`strategy_auth.py login --method password --save-api-token`, once).

`{base}/.well-known/openid-configuration` and `/oauth-authorization-server` describe `{base}/oauth2/authorize|token|revoke|introspect|userinfo|jwks`: authorization code (PKCE S256), client credentials, refresh, token exchange, DPoP. Clients (`WEB`, `SPA`, `NATIVE`, with redirect URIs) are registered by an admin through `GET/PUT /api/mstrServices/library/oauth2/settings` (AdministerEnvironment privilege); there is no dynamic registration. Product help says REST accepts the resulting bearer tokens; not verified here — the cloud tenant ignored `Authorization: Bearer` on REST. Revisit when a client is registered.

## Other mechanisms (not wired into the scripts)

- Trusted authentication (64) through a reverse proxy / Library trust relationship (`/api/mstrServices/library/iServer/trustRelationship`).
- JWT login for embedding (`GET/PUT /api/mstrClients/auth/jwt`: issuer, audience, keys/JWKS, claim map).
- SCIM 2.0 bearer tokens (`/api/mstrServices/library/scim/bearer`) authenticate the SCIM API only.
- The Trino-compatible SQL endpoint uses HTTP basic auth with a Strategy password (`strategy_validate_models.py`); SSO-only accounts query through the MCP `query` tool instead.
