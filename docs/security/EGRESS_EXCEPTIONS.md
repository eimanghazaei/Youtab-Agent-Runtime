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

## Allowlist categories (source: `security/egress_allowlist.json`)

Counts are per unique `(file, symbol)` — the gate's granularity — over 242 raw
call sites (117 unique keys):

| Category | Keys | Meaning | Residual risk |
|---|---:|---|---|
| `fixed_destination_infra` | 89 | Destination is a hardcoded provider/platform API host, an operator-config/env `base_url`, a first-party managed gateway (origin-validated), a local daemon/sidecar/loopback, or a provider-API-response delivery URL. Not influenced by agent output or end-user message content. | SSRF: none (attacker cannot choose the host). Audit-journaling gap only. LOW. |
| `dev_tooling` | 20 | Reachable only from CLI / setup / onboarding / diagnostics paths, not from an agent run serving requests. Hardcoded or operator-config hosts. | LOW. |
| `untrusted_destination_guarded` | 5 | Destination can be influenced by agent output / inbound message / operator-entered URL, **and is SSRF-guarded** (see fixes below). | LOW–MEDIUM, mitigated. |
| `sdk_internal` | 3 | A URL-less httpx transport handed to a vendor SDK (OpenAI/Anthropic/Azure/Gemini); the SDK owns the request. | See out-of-process residual. |

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

## Non-httpx adapter limitation (documented)

The `requests` / `urllib` / `aiohttp` / `websockets` audited adapters SSRF-check
the destination at **authorize** time (the classifier resolves DNS and takes the
most-restrictive answer), then the library dials the socket. This is not the
connect-time IP pin the httpx audited client provides, so a sub-second DNS-
rebinding resolver is a residual for those libraries. Callers needing rebinding-
proof egress should use the httpx audited client.
