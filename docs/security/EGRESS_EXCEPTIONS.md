# Egress boundary — coverage scope and exception ledger (WAVE-27)

## Precise coverage claim

"Universal egress coverage" is claimed **only for the repository-controlled,
in-process egress boundary**:

* Outbound **httpx** clients are built through the audited factory
  (`youtab_runtime.egress_guard_http` — SSRF-pinned at connect + authorize /
  attempt / outcome journaling). The shared SSRF factory
  `tools.url_safety.create_ssrf_safe_*` routes through it in **observe** mode
  while a run context is active (`youtab_runtime.egress_context`); SSRF
  enforcement is identical either way.
* Outbound **requests / urllib / aiohttp / websockets** have request-level
  audited adapters in `youtab_runtime.egress_adapters` (authorize-before-connect,
  fail-closed, journaled) — the approved construction path for new/high-risk code.
* A CI static gate — **`tools/egress_policy_lint.py`, run by the required
  `python-security` job** — bans raw construction of any of those clients in
  production code outside the audited adapters. Every pre-existing raw site is
  enumerated, with a per-entry reason, in **`security/egress_allowlist.json`**
  (the machine-checked companion to this document). A new un-allowlisted site,
  or a stale entry, fails the gate.

This claim does **not** extend to egress an in-process boundary cannot wrap:
vendor-SDK-internal transports and subprocess / sandbox / remote-environment
egress. Those are enumerated below as `PENDING_OWNER_ACTION` and are **not**
counted as verified. The literal "every byte audited" flag
(`UNIVERSAL_EGRESS_COVERAGE` in `tests/tools/test_egress_boundary_enumeration.py`)
therefore remains `False` by design.

## Allowlist keying — per-call-site fingerprint (WAVE-28 §6.5)

Each allowlist entry is keyed on `(file, symbol, fingerprint)`, where the
fingerprint is a sha256 of the call site's normalized AST (line/col attributes
stripped). This closes the WAVE-27 gap where an approved `(file, symbol)` could
be *mutated* into an unsafe site — a new destination, an added
`follow_redirects=True`, a widened kwarg, or an extra verb in the same file — while
the coarse gate still passed. Now any such change alters the fingerprint, so the
old entry goes **stale** and the mutated site is **un-allowlisted**; either way
the gate fails until the change is re-reviewed and re-baselined. An entry that
omits a fingerprint is treated as unenforceable and reported (fail-closed). The
mutation matrix is proven by `tests/tools/test_egress_boundary_enumeration.py`.

## Allowlist categories (source: `security/egress_allowlist.json`)

Counts are per unique `(file, symbol, fingerprint)` — the gate's granularity — over
290 raw call sites (231 fingerprinted keys; identical-fingerprint duplicates carry
an `occurrences` count). The gate bans raw construction of
`httpx.Client`/`AsyncClient` **and** the httpx module-level convenience verbs
(`httpx.get`/`post`/`put`/`patch`/`delete`/`head`/`options`/`request`/`stream`),
plus `requests.*`, `aiohttp.ClientSession`, `urllib.request.urlopen` and
`websockets.connect`:

| Category | Keys | Meaning | Residual risk |
|---|---:|---|---|
| `fixed_destination_infra` | 154 | Destination is a hardcoded provider/platform API host, an operator-config/env `base_url`, a first-party managed gateway (origin-validated), a local daemon/sidecar/loopback, or a provider-API-response delivery URL. Not influenced by agent output or end-user message content. | SSRF: none (attacker cannot choose the host). Audit-journaling gap only. LOW. |
| `dev_tooling` | 55 | Reachable only from CLI / setup / onboarding / diagnostics / skill-management paths, not from an agent run serving requests. Hardcoded or operator-config hosts. | LOW. |
| `untrusted_destination_guarded` | 18 | Destination can be influenced by agent output / inbound message / operator-entered URL, **and is SSRF-guarded** (see fixes below). | LOW–MEDIUM, mitigated. |
| `sdk_internal` | 4 | A URL-less httpx transport handed to a vendor SDK (OpenAI/Anthropic/Azure/Gemini); the SDK owns the request. | See out-of-process residual. |

The WAVE-27 `skills_hub` manual-redirect follow-up is **closed in WAVE-28 §6.3**:
every config/manifest/remote-catalog-influenced `follow_redirects=True` fetch in
`tools/skills_hub.py`, `youtab_agent_cli/skills_hub.py`, and `agent/pet/store.py`
now routes through `tools.url_safety.create_ssrf_safe_client` (connect-time IP pin,
per-hop re-validation), so those raw sites are gone from the allowlist.

## WAVE-27 untrusted-destination SSRF fixes

The destination-trust classification of all 242 sites found four sites that
fetched an agent/user/operator-influenced URL raw. All four are fixed:

1. `plugins/image_gen/openai/__init__.py` `_load_image_bytes` — agent/user
   `image_url` / `reference_image_urls` → now fetched via the connect-time
   SSRF-pinning client (`create_ssrf_safe_client`; metadata/loopback/private
   blocked, redirects re-validated). No longer a raw site.
2. `gateway/relay/media.py` `RelayMediaClient.download` — inbound-message media
   URLs (`event.media_urls`) → `is_safe_url` guard on the non-relay branch.
3. `youtab_agent_cli/web_server.py` `validate_custom_endpoint` — operator-entered
   custom endpoint URL → cloud-metadata floor (`is_always_blocked_url`); private/
   self-hosted provider validation still allowed; behind dashboard auth.
4. `youtab_agent_cli/web_server.py` `validate_provider_credential`
   (`OPENAI_BASE_URL`) — same metadata floor.

Regression tests: `tests/security/test_wave27_ssrf_fixes.py`.

## Out-of-process residual — PENDING_OWNER_ACTION (not verified, not PASS)

An in-process boundary cannot wrap these; they require network-policy / egress-
proxy enforcement at the infrastructure layer, and/or live-provider verification:

* **Vendor SDK internal transports** (`sdk_internal` entries:
  `run_agent.py`, `agent/azure_identity_adapter.py`,
  `agent/gemini_native_adapter.py`, plus the OpenAI/Anthropic/litellm SDKs used
  elsewhere). Where an SDK accepts a custom httpx client, the audited client
  should be injected and verified with a live provider. `boto3`/`urllib3`-based
  SDKs cannot take the audited httpx transport and need network policy.
* **Subprocess / sandbox / remote-environment egress** (`tools/code_execution_tool.py`,
  `tools/terminal_tool.py`, skill scripts, ssh/singularity/file-sync remote envs).
  These run outside the Python process entirely; only an egress proxy / network
  policy can audit them.
* **Run-context observe wiring in the real model worker.** The ambient
  `egress_run_context` is adopted by the benchmark harness and is available for
  the real agent worker; end-to-end run-scoped observe auditing in the production
  model worker requires a live provider to exercise and is `PENDING_OWNER_ACTION`.

## WAVE-27 LOW/MEDIUM follow-ups — CLOSED in WAVE-28

All four repository-side follow-ups from the WAVE-27 reviews are now fixed:

* **`tools/skills_hub.py` / `youtab_agent_cli/skills_hub.py` raw redirect-following**
  — CLOSED (§6.3). Every catalog/manifest-influenced `follow_redirects=True` fetch
  now routes through `tools.url_safety.create_ssrf_safe_client` (connect-time IP
  pin, per-hop re-validation); the write-scoped GitHub publish flow uses the
  pinning client with `follow_redirects=False` so its token can't cross an origin.
  Tests: `tests/tools/test_skills_hub_ssrf_wave28.py`.
* **`agent/pet/store.py` redirect-follow** — CLOSED (§6.3). Thumbnail/download/JSON
  fetches route through the pinning client, so a redirect *off* `petdex.dev` to a
  private/metadata IP is blocked at the hop.
* **Windows `.env` writer window** (`youtab_agent_cli/config`) — CLOSED (§6.1). All
  three writers (`save_env_value`, `remove_env_value`, `sanitize_env_file`) route
  through a born-owner-only temp (`windows_acl.secure_write_secret_file`): the temp
  is created empty, its owner+SYSTEM-only protected DACL applied+verified before
  any secret byte, fail-closed. No pre-tighten window. Tests:
  `tests/youtab_agent_cli/test_env_writer_acl.py`.
* **`youtab_agent_cli/web_server.py` validate endpoints** — CLOSED (§6.2). The
  `/models` probes in `validate_custom_endpoint` / `validate_provider_credential`
  use `create_ssrf_safe_client` (connect-time pin, per-hop re-validation), so the
  DNS-rebinding TOCTOU between the resolve-time metadata floor and the connect is
  closed; the WAVE-27 floor is kept as defense-in-depth and self-hosted providers
  stay reachable. Tests: `tests/security/test_web_server_validate_ssrf_wave28.py`.

## Real-worker run-context wiring (WAVE-28 §6.4)

`egress_run_context` is now wired into the real run path (`run_conversation` in
`agent/conversation_loop.py`, resolving run/tenant/user identity from the worker
spawn env), proven by `tests/youtab_runtime/test_worker_egress_context_wave28.py`
(attribution, async-task propagation, no cross-run leakage, cancel/exception
cleanup, redacted audit). The single remaining sub-path — a genuine live-provider
HTTP hop inside the ambient context — needs a reachable provider and stays
`PENDING_OWNER_ACTION`.

## Non-httpx adapter limitation (documented)

The `requests` / `urllib` / `aiohttp` / `websockets` audited adapters SSRF-check
the destination at **authorize** time (the classifier resolves DNS and takes the
most-restrictive answer), then the library dials the socket. This is not the
connect-time IP pin the httpx audited client provides, so a sub-second DNS-
rebinding resolver is a residual for those libraries. Callers needing rebinding-
proof egress should use the httpx audited client.
