# Youtab Agent Runtime — Master Tracker

The single authoritative record of what is done, what is running, and what is
waiting. A row is only ever marked VERIFIED with evidence that can be re-run;
"it looked right" is not a status.

Queued work is never deleted to tidy this file. An item that stops being
relevant is closed with a reason, not removed — a tracker that forgets is worse
than no tracker, because it reads as complete.

## Owner authorization — AR-PROD-01 engine connect (2026-08-19)

The Owner confirmed this repository is the **real Youtab Agent Runtime product
engine** and authorized connecting it end to end into the Youtab product. `youtab-ai-os /v1/agents` is the only public product API/control
plane; a **versioned `AgentRuntimeConnector`** boundary calls THIS engine for real
execution. The browser never calls this engine directly.

```text
AR-PROD-01 (engine side) AUTHORIZATION = OWNER APPROVED (2026-08-19)
ENGINE BRANCH = feat/agent-runtime-connector off origin/main 13f79aa6caae907af16c6ce87012021671319952
ROLE = execution engine — owns agents, runs, dispatcher, retries, checkpoints, tools,
  skills, sandbox execution, runtime events, artifacts. Wire the youtab_runtime
  signed-command boundary to the real engine if currently disconnected; prove the
  full command path.
AUTHORIZED = add/adjust a service-authenticated integration surface + signed-command
  ingress for the gateway connector; contracts/schemas/tests/CI/config/docs; a new
  ISOLATED Draft PR (do NOT mix into the Simorgh-focused PR #31).
NOT AUTHORIZED = merge; production deploy; direct writes to main; force-push shared
  branches; Simorgh create/copy/modify; weaken/reduce/discard existing runtime
  capabilities; real production credentials.
CROSS-REPO = youtab-frontend #106  ->  youtab-ai-os #412  ->  this engine connector PR.
```

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

**4. The workspace/platform line, drawn where the Owner asked for it.** Those
five keys were left writable pending a decision. The decision: they stay
configurable for the caller's own isolated workspace, and the settings that
reach past it do not.

| stays `config:write` — the caller's own agent | needs `ops:manage` — the platform's posture |
|---|---|
| `approvals.*` (mode, deny, timeouts, confirms) | `security.allow_private_urls` — governed egress / SSRF |
| `command_allowlist` | `security.website_blocklist.*` — governed egress |
| `hooks_auto_accept` | `security.redact_secrets` — secret protection |
| `code_execution.mode` | `security.tirith_enabled` / `_path` / `_timeout` / `_fail_open` — whether the policy engine runs, which binary is it, and whether no-verdict means yes |
| `security.acked_advisories` | `security.allow_lazy_installs` — supply chain |

Two facts decided the left column rather than taste. `approvals.mode=off`, a
permissive `command_allowlist` and `--yolo` all widen the caller's own blast
radius and none of them reaches the floor: `detect_hardline_command` runs
*before* every bypass, so `rm -rf /`, `mkfs`, `dd` to a raw device and shutdown
stay refused — asserted behaviourally, with yolo switched on. And
`code_execution.mode` only selects `project` vs `strict` — where a script runs
and with which interpreter — while env scrubbing and the tool whitelist apply
identically in both.

The same bar is applied to `PUT /api/config/raw`. `provider:write` and
`ops:manage` travel together on Superadmin and Owner, but an explicitly scoped
operator can hold one without the other, and YAML reaches every key the
structured form does.

**5. Credential coverage is now fail-closed, and it found two live leaks.**
The redactor matches leaf key names exactly — precise, but silent about a
credential named something nobody listed. A new test scans `CONFIG_SCHEMA`
with a deliberately broader heuristic and requires every hit to be either
masked or recorded in `NOT_A_SECRET` with a reason, so a new credential-shaped
field is red on arrival rather than served in the clear.

Writing it surfaced two fields `GET /api/config` was handing to any signed-in
user: `dashboard.basic_auth.password_hash` — the stored verifier for the
dashboard login, an offline-crack target — and `browser.camofox.session_key`.
Both are masked now, and each masked field is additionally proven to survive
the read-modify-write round trip rather than persisting its own mask.

The eighteen false positives the heuristic also catches are listed with their
reasons: `voice.record_key` is a keyboard binding, `*_tokens` are LLM counters,
`secrets.bitwarden.access_token_env` names a variable rather than holding one.
A second test fails if any of those justifications stops matching a real field,
so a stale exemption cannot sit waiting for its name to be reused.

Cleared after reading, with the property that clears them asserted rather than
assumed: `/api/cron/fire` (fail-closed JWT verifier), the MCP OAuth callback
(single-use state, `compare_digest`), `/auth/native/token` (PKCE
`SHA256(verifier) == challenge`, single-use code), `/api/messaging/platforms`
(`redact_key(value)`, never the raw token), `/api/memory/providers/{}/config`
(secret fields blanked on both payload paths), `/api/mcp/oauth/flows/{}`
(snapshot carries no token), `/api/learning` (the user's own skills and memory),
`/api/skills` (agent capability, including `hub/install`), and
`/api/dashboard/agent-plugins` (plugin lifecycle).

### Local suite — the eleven inherited failures, fixed at root cause

They were being carried as "container artifacts". Four were real product bugs
that only a root/IPv4-only/older-git host exposed; three were tests asserting
nothing, or asserting it in a way that could not hold off the author's machine.
None was skipped, xfailed, deleted or weakened.

| # | failure | root cause |
|---|---|---|
| 1 | `test_normalize_folds_home_prefix` | **Product.** The home-fold guard required two path components, so `/root` — a real home, and the default in this project's Docker images — never folded. `/root/.ssh/authorized_keys` never became `~/.ssh/authorized_keys`, and every dangerous-command pattern anchored on `~/` stopped firing. Now folds any home except a filesystem/drive root or a directory *of* homes (`/home`, `/Users`). |
| 2–6 | `test_update_eol_churn` (5) | **Product.** `_eol_only()` compared `--name-only` against `--name-only --ignore-cr-at-eol`, but `--name-only` decides from blob ids and never runs the text comparison, so the ignore flag did nothing and the two sets were always identical. The repair checkout never ran — and the `core.autocrlf=false` pin was written anyway, producing exactly the dirty checkout the function exists to prevent. Now probes with `--numstat`, which honours the flag. |
| 7 | `test_skips_occupied_successor` | **Product.** `find_free_debug_port` required a bind on *both* loopbacks, so on an IPv4-only host every candidate failed the `::1` probe, the loop exhausted, and it returned the `preferred + 1` fallback unconditionally — handing back the occupied port it was asked to avoid. `EAFNOSUPPORT` and friends are now "not a constraint" rather than "occupied". |
| 8 | `test_defaults_to_process_environ_copy` | **Test.** Asserted nothing: one check ended in `or True`, the other compared the *ambient* `GCM_INTERACTIVE` against the value the helper forces, so it broke on any host presetting it. Replaced with the real invariant — and that needed a subprocess with a pristine env, because an env leak is idempotent, so both an in-process snapshot and an inheriting child already contain it. |
| 9 | `test_apply_refuses_to_overwrite_unreadable_config` | **Test.** Simulated an unreadable file with `chmod(0o000)`, which uid 0 ignores — so under root the file stayed readable, the migration succeeded, and the test asserted nothing while reporting green. The denial is now injected at the `open()` boundary, which holds for every uid and on Windows. |
| 10 | `test_systemd_restart_gracefully...` | **Test.** Depended on the host having a live user-systemd session; without one the real code correctly reaches for `loginctl enable-linger`, which the fake `subprocess.run` did not model. The preflight is stubbed, like its siblings already were. |
| 11 | `test_system_unit_includes_local_bin_in_path` | **Test.** Tripped the deliberate refusal to install the gateway system service as root — a control, not a bug, and separately pinned. Now names the service user via `run_as_user`, that control's own documented override. |

### `tests/tools` — 39 failures, and the coverage gap they were hiding

`tests/tools` was never in the CI gate. It is now, because the gap was not
theoretical: the `tools/approval.py` home-fold repair — without which
`/root/.ssh/authorized_keys` never folded to `~/.ssh/authorized_keys` and every
dangerous-command pattern anchored on `~/` stopped firing on a root install —
is proved by `tests/tools/test_approval.py`, and nothing in CI ran it. Nor the
file-write safety, browser secret-exfil or yolo-mode suites.

The count was 39, not the ~18 a partial run suggested. **All 39 are green.**
Thirty-eight were fixed in the previous commit; the thirty-ninth was an asset
the repository did not contain, and that asset now exists.

| cause | files | tests | verdict |
|---|---|---|---|
| Optional extra absent + lazy installs disabled | daytona, image_generation, managed_media, modal, video_surface, web_tools_config | 30 | **Test.** Each stubbed the SDK into `sys.modules`, but `lazy_deps.ensure()` decides from installed *distribution metadata* (`importlib.metadata.version`), which no `sys.modules` stub can satisfy. So the real gate ran and refused on every hardened or offline host. The install policy is not what any of them is testing. |
| `ssh`/`scp` not on PATH | ssh_environment | 5 | **Test.** `SSHEnvironment.__init__` fails fast without an OpenSSH client, which is right; these tests construct it only to inspect the argv and control-socket path it computes, and never connect. |
| `man` "present" but non-functional | execution_flag_detection | 2 | **Test guard.** Debian's minimized images — which this project's own runtime images derive from — ship `/usr/bin/man` as a shell stub that prints a notice and exits 0, ignoring every argument. `shutil.which` found it, so the guard passed, no pager was ever invoked, and a missing payload marker was reported as a failure of the *approval grammar*. The probe now asks `man -w`: real man prints a path, the stub prints prose. |
| `chmod(0o000)`/`0o500` under uid 0 | lazy_deps_durable_target | 1 | **Test.** Root ignores the mode bits, so the directory was created anyway and the assertion failed while the product behaved correctly. Now induced by a non-directory parent (ENOTDIR, every uid) plus an injected `PermissionError` for the read-only-mount case the docstring names — two tests where there was one. |
| Bundled wake-word model absent | wake_word | 1 | **Product — fixed.** The advertised default detector did not exist and could not be renamed into existence. It is now trained; see below. |

No test was skipped, xfailed, deleted or weakened to reach that. The `man`
change corrects a guard that was asking the wrong question; the assertion it
guards is untouched and still runs wherever real man exists.

### Closed: the "hey youtab" wake-word model is trained and ships

`tools/wakewords/` shipped only a `README.md`. The rebrand renamed the
*expected* filename to `hey_youtab.*`, but a text rebrand cannot rename a
trained model — the phrase is in the weights — so the binaries were dropped in
the transplant and the advertised default detector could not load at all.

That also ruled out both cheap exits. Renaming the predecessor's binaries in
would ship a detector answering to the retired brand while the docs promise
otherwise, and correcting the docs to match would put the retired brand back
and trip the branding gate. An openWakeWord model detects exactly the phrase it
was trained on, so the only way to close this was to train one.

`scripts/wakeword/` is that pipeline. It runs end to end on four CPU cores with
no GPU, and every external byte it consumes is pinned by SHA-256 in
`assets.py` — a wrong hash fails the run rather than quietly training a
different model, which is verified by mutation and not merely asserted.

**What the model is.** openWakeWord splits detection in two: a shared front end
turns audio into 96-dimensional frames every 80 ms, and a small per-phrase
classifier reads the trailing sixteen. Only the classifier is phrase-specific
and only the classifier is trained here; the front end is pinned upstream
Apache-2.0 and is an input to training as well as to inference, so the features
the model was fit on are the features it is scored on.

**Data, all redistributable.** Positives are synthesized from the
piper-sample-generator LibriTTS-R model, which mixes pairs of 904 speaker
embeddings — MIT code over a CC BY 4.0 corpus. Negatives are Google Speech
Commands v0.02 (CC BY 4.0, 105,829 clips, 2,618 speakers) plus near misses
synthesized from the same voices as the positives: `hey youtube`, `hey your
tab`, `hey you tap`, `youtab` alone, `hey` alone, and thirty more. Reverberation
is synthesized with pyroomacoustics rather than taken from a recorded corpus.
No private recording and no unlicensed dataset is used anywhere.

**Splits are disjoint by source, and verified so.** Synthesized voices are
speakers `[0, 700)` for training and `[700, 904)` for measurement — the two
sets were checked to share zero speaker embeddings. Recorded speech uses Speech
Commands' own `validation_list.txt` and `testing_list.txt`, which are
speaker-disjoint by construction. Four of the six background recordings train,
two measure. Impulse-response pools are seeded independently.

**Measured, not asserted.** `evaluate_model.py` constructs
`tools.wake_word._OpenWakeWordEngine` — the class the CLI, TUI and desktop app
construct — and streams 1280-sample frames through it, so the headline number
is the engine's own fire decision under its own three-consecutive-frame rule,
not a validation accuracy. Full results are in `tools/wakewords/MODEL_CARD.md`,
which is generated from the run's outputs and records the artifact hashes
alongside them so it cannot describe a model other than the one that shipped.

One design fault was caught before it reached a measurement rather than after:
positives were first placed with the phrase ending anywhere from 0 to 400 ms
before the window edge, and a phrase ending *at* the edge scores high on
exactly one frame — the next 80 ms pushes it out of the trailing sixteen. A
model trained that way peaks beautifully and never fires, because the engine
wants three frames in a row. Placement is now 0.16–0.64 s of trailing context,
and the evaluator measures the resulting plateau instead of trusting it.

**What it measures at.** 5,000 spoken wake words and 26,986 negatives — 15.0
hours of audio — through the engine, at the default `sensitivity` of 0.6:

| | ONNX | tflite |
|---|---|---|
| missed wake words | 6.70% | 6.70% |
| fires on background, no speech | **0.000%** (0/1,000) | 0.000% |
| fires on recorded human speech | **0.071%** — 1.29/hour | 0.071% |
| fires on deliberate near misses | 8.7% | 8.8% |

The near-miss corpus is adversarial by construction and is a sixth of the
negatives, which no real room resembles; the recorded-speech row is the one
that predicts how often an always-on listener interrupts someone. Both
backends agree to 2.7e-06.

That took two rounds, and the second was driven by a measurement rather than a
hunch. Breaking the first model's false accepts down by phrase showed the
inventory had been designed around the wrong confusion: `hey youtube` fired on
1 clip in 106, while minimal pairs on the final syllable — `hey you tap`, one
voicing feature from the wake word — fired on 88%, and `hey there` on 3.8%.
Ten thousand more clips of the thirteen phrases that actually fired took the
near-miss rate from 13.0% to 8.7%, recorded-speech false accepts from 1.89 to
1.29 per hour, and `hey there` to zero, for 2.2 points of false-reject rate.
That is the direction this product's own code already argues for: its
confirmation-frames rule is documented as "the primary lever against
unintended triggers on ambient talk."

An intermediate attempt is recorded in the pipeline because it failed
usefully: shifting the exported bias so the runtime's fixed 0.6 lands on a
chosen false-reject target *tripled* false accepts on ordinary speech, 0.105%
to 0.272%. Calibration is therefore used to compare epochs — comparing two
epochs at a fixed 0.6 compares each at a different point on its own curve — and
`--calibrate-operating-point` is off for export.

`test_bundled_hey_youtab_model_ships_on_disk` is no longer deselected, and
`run_all_gates.sh` now deselects nothing at all.
`tests/tools/test_wake_word_model_assets.py` joins it: hashes, the tensor
shapes openWakeWord reads back, backend agreement, and behaviour on committed
held-out audio spanning clean, noisy, reverberant, near-miss and
recorded-speech conditions. Both backends execute in required CI — ONNX
already reached the runner as a transitive of the branding gate's OCR engine,
and the tflite runtime is installed by a workflow step at the same pin the
`wake` extra already carries, following this workflow's own recorded reason for
keeping CI tooling out of the `dev` extra.

### Closed: the required gate now proves a first-execution pass

A repeated-suite run labelled `file-retries=0` still reported *"2 FLAKY files
(failed once, passed on retry)"*. The contradiction was real and had two
independent causes, neither of which was the flag itself:

1. **The knob never reached the runner.** `scripts/run_tests.sh` execs
   `run_tests_parallel.py` under `env -i` — an empty environment with an
   explicit opt-in whitelist. Every one of the six `YOUTAB_AGENT_TEST_*`
   variables the runner documents in its own `--help` was absent from that
   whitelist, so `YOUTAB_AGENT_TEST_FILE_RETRIES=0` was discarded at the
   boundary and the runner fell back to `_DEFAULT_FILE_RETRIES = 1`. The
   override read as accepted and did nothing. Measured directly: with the old
   whitelist the runner sees `None`, with the forwarding loop it sees `'0'`,
   and with nothing set no `YOUTAB_AGENT_TEST_*` variable enters the
   environment at all, so the isolation intent is unchanged.
2. **The required gate never asked for it.** `run_all_gates.sh` invoked
   `run_tests.sh` without `--file-retries 0`, so the gate ran with the retry
   default regardless of any environment variable. Green was reachable on a
   second attempt.

The runner never printed a `file-retries=` header, so the header in that log
came from the operator's wrapper, not from the runner — which is why the log
looked self-contradictory rather than simply wrong.

Both are fixed. `tests/youtab_runtime/test_ci_runner_contract.py` holds three
tests: the wrapper must forward every knob the runner reads, the required gate
must pass `--file-retries 0`, and `--file-retries 0` must let a first-attempt
failure fail. The first two are mutation-proved — removing the forwarded name,
and removing the flag, each turns its test RED and restoring turns it GREEN.
Each parse asserts it found something before asserting a subset relation, so a
regex that stops matching fails loudly instead of passing vacuously.

The tests live in `tests/youtab_runtime/` deliberately: required CI collects
only `tests/youtab_runtime`, `tests/youtab_agent_cli` and `tests/tools`, so a
test placed at `tests/` root would never have run.

Consequence to expect: retries are now off in the required gate, so any
genuine flake surfaces as a red check instead of a FLAKY note. That is the
intended behaviour. A red run names the file; fix it at root cause rather than
restoring the retry.

### `/api/plugins` cluster — body-read, one engine-binding bypass closed

The largest cluster still on the blanket prefix is now classified from its
handler bodies (47 route+methods + the `events` WebSocket). The two
provider-catalogue reads keep their `provider:read` correction —
`kanban/model-options` (provider→model catalogue) and `kanban/profiles` (each
profile's raw `model`/`provider`). Every other route reads or mutates only the
caller's own board, task, comment, attachment, profile *name* or achievement
and is genuinely `plugin:use`; the WebSocket enforces its scope at the upgrade.

**A sixth engine-binding writer, found and closed in the handler.** `POST
/api/plugins/kanban/tasks`, `PATCH /api/plugins/kanban/tasks/{}` and `POST
/api/plugins/kanban/tasks/bulk` each accept an optional
`model_override`/`provider_override`, which `kanban_db.set_model_override`
persists as the raw provider+model the dispatched worker runs against — the same
`engine:select` authority as `/api/model/set`, `/api/profiles/{}/model`,
`/api/tools/toolsets/{}/model` and `/api/config`. It is one optional sub-field
of an otherwise user-owned mutation, so the route table cannot express it.
Following the `PUT /api/config` precedent, the three handlers now call
`_require_engine_scope_for_override` before any DB write: a caller lacking
`engine:select`/`provider:write` who *selects* a non-empty override is refused
403, while an override-free write, an explicit clear, an empty override, and
loopback/local dev (which resolves to the Owner) are untouched. The routes stay
`plugin:use` at the table; the elevated sub-field is enforced where the table
cannot see it. Recorded in the registry as `_PLUGIN_TASK_WRITE_ENGINE_GUARDED`.

Proven by `tests/youtab_runtime/test_kanban_engine_override_authz.py`
(set-vs-clear predicate; positive/negative/fail-closed on all three handlers; a
mutation that neuters the guard line and turns a named test RED, restored GREEN)
and the classification pins in `test_plugins_authz_audit.py`. Registry count
unchanged at **127, 0 stale**; route inventory byte-identical at 294/255/7/1;
`router_policy_gate.py` PASS.

**Queued follow-up — `tests/plugins/` is not in the required gate.** This
slice's handler tests were placed in `tests/youtab_runtime/` so the gate runs
them; the `tests/plugins/` directory itself is still uncollected (the same gap
`tests/tools` had before it was added) and carries one pre-existing stale-stub
failure (`test_kanban_dashboard_plugin.py::test_ws_events_rejects_when_token_required`
stubs `web_server` without the real `_ws_scope_ok`). Bringing `tests/plugins/`
into the gate, with that one-line stub fix, is a separate slice, not done here.

### Still outstanding on this row

- `GET`/`PUT /api/config` — **closed**, see defects 3–5 above.
- `/api/plugins` (47 route+methods) — **closed**, body-read; see above.
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
--by-prefix`. The privileged clusters already carry scopes and are the anchor.
`/api/plugins` — the last large unclassified cluster — is **done** (above), so
the remaining route-authz work is folding the still-unregistered enforced routes
into the body-verified registry (127 of 294) and bringing `tests/plugins/` into
the gate.

With `/api/plugins` closed, Workstream #10 no longer holds the branch's largest
open exposure, and the next executable slice on the critical path toward the
governed-capability goals is the **Cognitive-growth & persistent-memory ADR
(#15)** — the blocking prerequisite the Sandbox (#17), Workspace ingress (#19)
and memory-sharing-with-Simorgh work all wait on. It is an architecture decision
and should be written as an ADR before any of those rows opens code.
