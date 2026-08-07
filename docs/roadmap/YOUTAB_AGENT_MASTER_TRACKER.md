# Youtab Agent Runtime — Master Tracker

The single authoritative record of what is done, what is running, and what is
waiting. A row is only ever marked VERIFIED with evidence that can be re-run;
"it looked right" is not a status.

Queued work is never deleted to tidy this file. An item that stops being
relevant is closed with a reason, not removed — a tracker that forgets is worse
than no tracker, because it reads as complete.

## Position

| | |
|---|---|
| Repository | `eimanghazaei/Youtab-Agent-Runtime` |
| Branch | `feat/youtab-agent-runtime-on-main` |
| PR | **#10** (OPEN, draft) |
| Base | `main@cc4cab2f592e60a197e796506de9168f74baf3ea` |
| Last CI-verified SHA | **`210c173eadccb214e76e842af5cc3480c7e3fe49`** — see below |
| PR head | this file's own commit, one docs-only commit ahead of it |
| **Deployed SHA (protected pre-production)** | **`64b32afb68dc022fc463c72d6e024054fd816e4c`** |

The deployed SHA is tracked separately from the PR head on purpose. They are
not the same thing and have not been the same thing for the whole of this
branch; conflating them is how a "green PR" gets described as shipped.

The PR head is named by description rather than by SHA, because a commit that
records the verified SHA necessarily advances past it. Writing a literal SHA
there would make this table wrong the instant it was written. The verified SHA
below is a fact about a completed run; the head is a moving target.

**No merge and no deployment are authorized.**

## Governed runtime versions

Source of truth is `uv.lock`, which is what the image installs via
`uv sync --frozen` (`Dockerfile:270`). Nothing else may state a version.

| Package | Locked | Enforced by |
|---|---|---|
| fastapi | 0.133.1 | `tests/youtab_runtime/test_framework_version_matches_lock.py` |
| starlette | 1.3.1 | same |
| uvicorn | 0.41.0 | same |

Required CI installs `-e '.[dev,web]'`; the `web` extra carries the same pins.
`.[dev]` alone pins Starlette but not FastAPI, and the core dependency is the
range `fastapi>=0.104.0,<1`, so that path resolved freely and walked a
different router than production builds.

## Workstreams

| # | Workstream | Status | Evidence / return point |
|---|---|---|---|
| 1 | Truthful gateway lifecycle contract | **VERIFIED** | `tests/youtab_agent_cli/test_gateway_lifecycle.py` (77) |
| 2 | Six platform root-cause test fixes | **VERIFIED** | 706 passed on the previously-failing set |
| 3 | Secure completion gate (governed verification registry) | **VERIFIED** | `tests/youtab_runtime/test_completion_gate.py` (26) |
| 4 | Container control-plane isolation gate + Compose hardening | **VERIFIED** | `scripts/youtab/container_isolation_gate.py`; 9 controls; 48 tests |
| 5 | Cloudflare Access registered as auth provider, wired end to end | **VERIFIED** | 28 + 45 tests; one chain, no header trust |
| 6 | Event-stream scope + assertion-only external bind | **VERIFIED** | `events:read`; socket-level enforcement |
| 7 | CSRF + exact Origin/Referer on the external bind | **VERIFIED** | `tests/youtab_agent_cli/test_csrf_and_origin.py` (29) |
| 8 | Route inventory from the real router | **VERIFIED** | `scripts/youtab/route_inventory.py`; 294/255/7/1, 0 unrecognised |
| 9 | Dependency provenance & CI/local/production version parity | **VERIFIED** | this commit; 7 mutations RED, restored GREEN |
| 10 | **Route authorization default-deny + Router-vs-policy gate** | **ACTIVE — gate built, audit continuing** | `scripts/youtab/router_policy_gate.py`, 9 conditions each proven RED; 337 tests. Endpoint-body audit ongoing. |
| 11 | Purpose-bound WebSocket tickets | QUEUED | one ticket currently opens any socket |
| 12 | Tenant/user event filtering | QUEUED | scope gate exists; payload filtering does not |
| 13 | Governed CSRF secret-file contract | QUEUED | `YOUTAB_CSRF_SECRET_FILE`, ≥32 bytes, fail closed |
| 14 | Agent Intelligence Floor | QUEUED | needs a measured baseline before it can gate |
| 15 | Cognitive-growth & persistent-memory ADR | QUEUED | blocking prerequisite for Sandbox/Workspace |
| 16 | Secret Broker & Provider Proxy | QUEUED | ADR first |
| 17 | Capability engine & execution sandbox | QUEUED | ADR first |
| 18 | Safe snapshot/rollback service | QUEUED | kit engine rejected: deletes post-snapshot user files |
| 19 | Workspace ingress & Code-to-Agent connection | QUEUED | after sandbox + workspace |
| 20 | File Reader sub-agent | QUEUED | read-only, workspace-scoped |
| 21 | Governed dependency/search/debug egress | QUEUED | default-deny with task profiles |
| 22 | Branding · `USER_REACHABLE → 0` | QUEUED | classifier already at 0 blocking |
| 23 | Kanban → Orchestrator Board migration | QUEUED | product workstream |
| 24 | Frontend Agent launcher PR #72 | **PARKED** | separate repository |
| 25 | Runtime PR #8 | **EVIDENCE ONLY** | untouched; `972f48be7` |

## Known constraints

- **CSRF replay store is single-process.** In-memory nonces; single-use holds
  within one process only. Horizontal scaling stays unqualified until a shared
  atomic store exists.
- **Cookie-session binds have no CSRF coverage.** The gate is scoped to the
  external Access bind. They were never weakened — they were never covered.
- **Docker daemon unavailable locally.** Image build, digest, SBOM and
  container-escape tests are CI/VPS work.
- **`/assets` is conditional.** It exists only when the SPA is built, so route
  qualification must handle both states rather than assume one.

## Verified green checkpoint

| | |
|---|---|
| SHA | **`210c173eadccb214e76e842af5cc3480c7e3fe49`** (current PR #10 head) |
| Workflow run | `31149976292` (run 48) |
| `python-security` | **success** — gates step ran 11m49s |
| `javascript` | **success** |
| Youtab gates | **12 of 12 PASS**, including `router-policy` and `branding` |

The prior checkpoint `62a221f39` is also green on its own run (`31117452085`):
545 files, 4282 tests passed, 0 failed, with `test_router_policy_gate.py` (14),
`test_authorization_is_fail_closed.py` (137) and `test_route_authz_registry.py`
(35) named in the log, so "the gate suites ran" is checkable rather than
asserted. `210c173ea` differs from it by this file alone.

`fa28c6d13` is recorded as **not** green: its run failed the branding gate,
because the comment added to explain the codex rebrand inconsistency reproduced
the retired brand token while describing it. It is the SHA that looks like the
finish line and is not.

Run 48 was triggered by `workflow_dispatch` rather than `pull_request`, because
the `pull_request` run for this SHA (`31119158285`) wedged during a GitHub
Actions incident: five attempts produced no gate result — two died at
`Failed to resolve action download info`, three never had a runner allocated
(`runner_id: 0`, zero steps) — and the run then refused both `cancel`
("not yet queued") and `rerun` ("already running"). The dispatched run executes
the identical tree at the identical SHA; it may not tick PR #10's *required*
check boxes, which are produced by the `pull_request` event.

**No merge and no deployment were performed or authorized.**

## Workstream 10 — exact position

Authorization is **fail-closed**. `authorize()` refuses any route no table
describes, for every principal including the Owner. Enforcement is
`authz.required_scope`, consulted most-specific first: exact path, then
parameterised pattern, then longest prefix, then the SPA catch-all for paths
outside the application's own roots.

| | |
|---|---|
| HTTP route+method pairs resolving to a decision | **294 of 294** (built) / 293 of 293 (unbuilt) |
| Unmapped HTTP routes | **0** |
| WebSockets enforcing a scope at the upgrade | **7 of 7** |
| Public routes | 16 — 14 login/shell, 2 credential-bearing |
| `authorize()` default-deny | **ACTIVE** |
| Router-vs-policy CI gate | **BUILT** — 9 conditions, each proven RED |
| Endpoint-body audit | **IN PROGRESS** — the remaining work on this row |

Classification started **by cluster**, on Owner direction, to close the surface
before reading 250 function bodies. That was the right order — it stopped
`/api/pty`, arbitrary file write and the system-prompt endpoint being open for
as long as the reading took — but it was explicitly a first pass. The
endpoint-body audit is now correcting it route by route, and the table of
corrections below is what that pass has produced so far.

Longest prefix wins, and exact rules beat prefixes, so any route can be
narrowed without reordering the table.

### Fail-closed must not mean fail-empty

`NORMAL_USER` held no scopes and an unrostered identity resolved to nothing.
Harmless while unguarded meant reachable; after the flip it would have locked
every ordinary customer out of their own installation. `USER_CAPABILITIES` now
names what they already had and is granted to the normal-user baseline. It
contains no provider, credential, engine, tenant, deployment or ops scope.

### WebSocket enforcement, per socket

| socket | scope | before |
|---|---|---|
| `/api/pty` | `session:write` | **no check** |
| `/api/console` | `session:write` | **no check** |
| `/api/ws` | `session:write` | **no check** |
| `/api/audio/speak-stream` | `session:read` | **no check** |
| `/api/events` | `events:read` | **no check** — see below |
| `/api/plugins/kanban/events` | `plugin:use` | credential only |
| `/api/pub` | `events:read` | already enforced, unchanged |

`/api/events` carried an `events:read` entry in `ROUTE_SCOPES` and had no check
at its socket. Starlette's HTTP middleware does not run on an upgrade, so that
scope governed only the transport nobody reads the stream over. The
`_ws_scope_ok(ws, EVENTS_READ)` call that existed was in `pub_ws`.

### Findings from the previous checkpoint — all six resolved

1. **`/api/status` probe contract — RESTORED.** Withdrawing it broke NAS
   `fly-provider.ts getInstanceRuntimeStatus`, whose cookie-less fetch is the
   portal's sole liveness signal. Public again; what makes that safe is the
   payload, and `test_status_withholds_host_detail_in_gated_mode` is the
   assertion doing the work. The same mistake had been made four more times —
   `/api/config/defaults`, `/api/config/schema`, `/api/dashboard/themes` and
   `/api/dashboard/plugins` are on the middlewares' allowlist and are the SPA's
   pre-login bootstrap; guarding them by cluster broke the login screen's own
   rendering. **The policy and `PUBLIC_API_PATHS` now agree**, closing the
   contradiction the previous checkpoint left open.
2. **`GET /api/profiles` payload — MASKED.** `model`, `provider`, `path` and
   `has_env` are stripped for a caller without `provider:read`; `name`,
   `description` and `skill_count` survive, so the picker still works. Keys are
   omitted rather than blanked — a blanked `provider` reads as "none
   configured", which is an assertion the masker is not entitled to make.
3. **`/api/cron/fire` and the MCP OAuth callback — VERIFIED.** The Chronos
   verifier refuses without a JWKS, refuses without an audience, rejects
   symmetric algorithms, requires `exp`/`aud`, and requires a `cron_fire`
   purpose claim. The callback matches single-use flow state with
   `secrets.compare_digest` and 404s when nothing matches. Public at the gate
   is correct: the credential is the boundary in both cases.
4. **Normal-user baseline — three corrections.** Found by reading bodies, all
   in clusters whose other routes are genuine user capability:
   `/api/analytics/models` (selects `model, billing_provider`) → `provider:read`;
   `/api/portal` (reports each feature's `current_provider`) → `provider:read`;
   `/api/ssh/ownership` (returns `sshOwnerNonce`, a live secret) → `ops:manage`.
   `/api/analytics/usage` deliberately stays `ui:read`. The `/api/ops` cluster
   is asserted clean against the router, and `USER_CAPABILITIES` is asserted to
   intersect no privileged scope.
5. **API docs policy — DECIDED: operator-only.** `/docs`, `/redoc`,
   `/openapi.json` and `/docs/oauth2-redirect` are held at `ops:manage`, with
   tests that an anonymous caller and an ordinary user are both refused and an
   operator is allowed. Removing them outright would require the routes never
   to be registered, which is an app-construction change; `ops:manage` gives
   the same externally-visible result and keeps the inventory checkable.
6. **CI scope — WIDENED.** The gate runs all of `tests/youtab_agent_cli`, not
   nineteen named files. The narrowing cost real coverage: the flip shipped two
   rules written as fixed-segment patterns against `:path` routes
   (`/dashboard-plugins/{plugin}/{file:path}` and the MCP callback), and both
   403'd every real request. Nothing in the selected files touched them; three
   files outside the selection caught it on the first full run. Per-file
   subprocess isolation is what makes the widening viable.

### Router-vs-policy gate — BUILT

`scripts/youtab/router_policy_gate.py`, wired into `run_all_gates.sh` as the
`router-policy` gate. It walks the real router and compares it against
`authz`, failing closed on nine conditions: missing classification, stale
entry, duplicate entry, unknown method, unknown route object, ambiguous
parameter normalisation, missing WebSocket, missing mount, unjustified public.
An inconclusive run fails too — a router that will not import fails the gate
rather than passing it.

Each of the nine is proven to turn it red, against the real policy tables
rather than a fixture, then restored
(`tests/youtab_runtime/test_router_policy_gate.py`, 14 tests).

It found two things on its first run: eleven public routes with no written
justification anywhere, and `/api/auth/csrf` classified public when its handler
raises 401 without a session and neither middleware allowlist admits it.

### Endpoint-body audit — findings so far

Method: sweep every route the normal-user baseline reaches for bodies that
touch provider bindings, credentials, engine identifiers or host paths
(233 reachable, 100 flagged), then open each match. Clusters read in full:
`/api/plugins` (48), `/api/profiles` (15), `/api/git` (19), `/api/sessions`
(14), `/api/ops` (15), `/api/fs` (6), `/api/files` (7), `/api/memory` (8), the
pre-existing privileged surface (31), and both allowlists.

**Corrected out of the user baseline** — each found by reading the body, each
in a cluster whose other routes are genuine user capability:

| route | was | now | why |
|---|---|---|---|
| `PUT /api/tools/toolsets/{}/env` | `tool:manage` | `credential:write` | writes API keys into `.env` via `save_env_value` |
| `GET /api/tools/toolsets/{}/config` | `tool:manage` | `credential:read` | provider matrix + per-key `is_set` |
| `GET /api/tools/toolsets/{}/models` | `tool:manage` | `provider:read` | backend model catalogue |
| `PUT /api/tools/toolsets/{}/model` | `tool:manage` | `engine:select` | persists an engine binding |
| `GET /api/analytics/models` | `ui:read` | `provider:read` | selects `model, billing_provider` |
| `GET /api/portal` | `ui:read` | `provider:read` | reports each feature's `current_provider` |
| `GET /api/ssh/ownership` | `repo:read` | `ops:manage` | returns `sshOwnerNonce`, a live secret |
| `GET /api/auth/csrf` | `public` | `authenticated` | handler 401s without a session |

**Three routes write an engine binding**: `POST /api/model/set`,
`PUT /api/profiles/{}/model`, `PUT /api/tools/toolsets/{}/model`. Each was
found separately and each would have been a bypass of `engine:select` alone;
all three are now pinned to it in one assertion.

**`/api/fs` reached the credential store.** `_fs_path` resolves any absolute
host path and applied no sensitive-path guard, so `fs:read` — in the user
baseline — read `.env` without `credential:read`. It now uses the guard
`/api/files` already had (`.env` variants, canonical credential basenames,
`mcp-tokens/` and `pairing/`). Ordinary project files are untouched and
`fs:read`/`fs:write` remain user capability.

**Deliberately left as user capability, with the reason pinned**:
`/api/memory/providers/{}/config` also reports `is_set`, but memory providers
are user-installed plugins holding the user's own keys, and neither payload
builder ever returns a secret value (`kind == "secret"` is blanked on both the
declared and undeclared paths). That masking is now asserted with a real secret
in the input.

### Cluster audit — all clusters now read

`/api/cron` (13), `/api/messaging` (11), `/api/mcp`, `/api/config` (6),
`/auth/native` (3), `/api/pairing` (4), `/api/webhooks` (5), `/api/learning`
(4), `/api/curator` (3), `/api/skills` (12), `/api/dashboard/agent-plugins`
(4). Two defects found, both fixed here.

**1. `/api/pairing/approve` let a normal user authorize another identity.**
One path, two authorization semantics, both at `device:manage`, which was in
the normal-user baseline:

| branch | proof of identity | correct bar |
|---|---|---|
| `code` | DM'd to whoever asked to pair, never returned by any endpoint (`list_pending` hashes it) — possession *is* the proof | `device:pair:self` |
| `request_id` | handed to anyone who can read `GET /api/pairing`; proves nothing about the caller | `device:manage` |

`device:manage` left the user baseline and now belongs to Tenant Admin,
Superadmin and Owner; `device:pair:self` replaces it there so self-pairing is
preserved. Listing, revoking and clearing the pending queue all act on someone
else's access and are `device:manage`. The route table matches path and method
and cannot see which branch a body selects, so the handler raises the bar
itself for the request-id branch — *before* touching the store, so a refusal
never reveals whether a guessed id exists.

**2. `/api/config/raw` was the widest bypass on the surface.** `config.yaml`
holds the engine bindings — `model.default`, `model.provider`, `mcp_servers`,
custom endpoint base URLs. `GET` returned it verbatim plus its absolute host
path at `config:read`; `PUT` replaced it wholesale (`merge_existing=False`) at
`config:write`. Both in the user baseline, so a normal user could set
`model.provider` by writing YAML and defeat `engine:select`, `provider:write`
and the custom-endpoint controls in one request without touching any route
those scopes guard. Now `provider:read` / `provider:write`.

That makes **four** routes able to write an engine binding — `/api/model/set`,
`/api/profiles/{}/model`, `/api/tools/toolsets/{}/model`, `/api/config/raw` —
each found separately, each a bypass alone. All four are pinned in one
assertion so a fifth cannot be added quietly beside them.

**3. `PUT /api/config` was the fifth writer, and it was already there.** The
assertion above said four and called that complete. Opening the structured
endpoint — the item this row listed as outstanding — found the fifth sitting
beside them at `config:write`. `ConfigUpdate.config` is an unconstrained
`dict`; `_denormalize_config_from_web` only reconstructs `model` when it
arrives as a *string*, so a dict passes through untouched; `_deep_merge` has no
allowlist. `{"config": {"model": {"provider": "..."}}}` therefore wrote the
engine binding at an ordinary user scope.

It could not be closed the way `/api/config/raw` was. This is what the
dashboard Config page saves through, so raising the route's scope would have
cost every signed-in person the ability to change their own theme. Two facts
from the frontend decided the shape: `ConfigPage.tsx` PUTs the *entire* config
it loaded, and `ReasoningPicker.tsx` read-modify-writes the whole document to
change one key. So the handler compares the payload against what is stored and
refuses only a request that **moves** `model`, `providers`, `custom_providers`,
`fallback_providers` or `mcp_servers` — presence is not change. An ordinary
save carries `model` untouched and still succeeds; a mutation is refused before
`save_config`, leaving the file untouched.

The same endpoint was also disclosing credentials. `GET /api/config` returned
`config.yaml` with only `_`-prefixed keys stripped, which at `config:read` — an
ordinary user scope — handed over the eighteen `auxiliary.*.api_key` fields,
`delegation.api_key`, the `providers`/`custom_providers` keys, and
`dashboard.basic_auth.password`, the credential guarding that same dashboard.
`GET /api/env` next door has always returned only `redact_key(value)` and an
`is_set` flag, and `youtab config` runs `redact_config_value` for exactly this
reason; the HTTP path was the one exception. It now runs the same redactor.

Masking alone would have been a worse bug than the leak. Because the dashboard
read-modify-writes the whole document, an untouched secret comes back as its
own mask, and persisting that would replace every credential with asterisks the
first time anyone changed a theme. `PUT` therefore drops a credential value
that equals the mask of what is stored — an unchanged field rather than an edit
— and the merge keeps the real secret. A value someone actually typed never
equals the mask of the previous one, so real edits still land.

Not changed, and recorded as an Owner decision rather than settled quietly:
`command_allowlist`, `approvals`, `hooks_auto_accept`, `security` and
`code_execution` are all writable at `config:write`. They are execution-policy
knobs for the caller's own agent in their own workspace, and withdrawing them
would reduce agent autonomy rather than blast radius, which is the one thing
this workstream is not permitted to do.

Cleared after reading, with the property that clears them asserted rather than
assumed: `/api/cron/fire` (fail-closed JWT verifier), the MCP OAuth callback
(single-use state, `compare_digest`), `/auth/native/token` (PKCE
`SHA256(verifier) == challenge`, single-use code), `/api/messaging/platforms`
(`redact_key(value)`, never the raw token), `/api/memory/providers/{}/config`
(secret fields blanked on both payload paths), `/api/mcp/oauth/flows/{}`
(snapshot carries no token), `/api/learning` (the user's own skills and memory),
`/api/skills` (agent capability, including `hub/install`), and
`/api/dashboard/agent-plugins` (plugin lifecycle).

### Still outstanding on this row

- `GET`/`PUT /api/config` — **closed**, see defect 3 above. The endpoint keeps
  `config:read`/`config:write`; the engine-binding half is guarded in the
  handler and the credential half is redacted.
- The `_SECRET_CONFIG_KEYS` redactor is exact-match on key name. It catches
  `api_key`, `token`, `password` and the rest of the usual shapes, so a future
  credential stored under an unusual key name would not be masked. Worth a
  sweep if new credential-bearing config keys are added.
- The registry (`route_authz_registry.py`) remains the body-verified subset and
  is deliberately smaller than the enforced table: **127 classified, 0 stale**.

## Next permitted slice

**Complete route authorization registry → enforce explicit default-deny → add
Router-vs-policy CI gate.**

Classification must come from reading endpoint bodies and their data and
mutation authority. Not from path prefixes, route names, UI visibility or
comments. No broad authenticated baseline. Unknown, stale, duplicate or
malformed entries fail closed, and a newly added route makes CI red until it is
classified.

Start with `python scripts/youtab/route_inventory.py --json inventory.json
--by-prefix`. The privileged clusters already carry scopes and are the anchor;
`/api/plugins` at 47 route+methods is the largest unclassified cluster.
