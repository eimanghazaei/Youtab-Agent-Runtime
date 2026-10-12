# Integration Preflight Handoff (read-only) — Enterprise Memory

Frozen candidate: `3e9a3dff6b86f342e1fbb0d510f08397b790b168` (tree `54b7f206…`), base
`c7650a1b920224283ba3a59ca054d6625e07a5f3`. No LIVE wiring; 2200/1375 unchanged.
This is an interface/compatibility handoff for the integration reviewer — not a new
versioned evidence bundle.

## 1. Ancestry & changed-file overlap

| Branch | SHA | merge-base with my HEAD | Base relation |
|---|---|---|---|
| my Memory branch | `3e9a3dff` | — | base = `c7650a1b` (origin/main tip) |
| Durable Execution `feat/runtime-durable-execution-v1` | `c0763e16` | **`c7650a1b`** | **same base as me** (clean common ancestor) |
| Lane-1 `delivery/runtime-folder-grant` | `aee138e1` | `13f79aa6` | lane base `13f79aa6` is an ANCESTOR of `c7650a1b` → **lane is behind main** |
| Lane-2 `delivery/runtime-generic-connector-v1` | `de246659` | `13f79aa6` | behind main |
| Lane-3 `delivery/desktop-sidecar-packaging` | `caaab74f` | `13f79aa6` | behind main |
| integration/runtime-lane1-lane2-v2 | `1ed19f09` | (lane lineage) | behind main |

**Changed-file overlap: ZERO** between my changed files and every Lane and the Durable
branch. No Lane/Durable branch touches `youtab_runtime/memory/` or `youtab_runtime/continuity/`.

**Exact integration base:** `c7650a1b` (origin/main). My branch and Durable both fork from it
and add only new files (no overlap). **CORRECTION (Codex):** the Lanes DO share ancestry with
`c7650a1b` (their base `13f79aa6` is an ancestor of it), so **forward integration is possible but
UNQUALIFIED** — the Lanes are simply cut from an older commit and would need forward-integration
and re-qualification; this is NOT a claim that they "cannot merge". No merge/rebase/push is
authorized here. My work does not depend on the Lanes and does not block them.

### Lane-2 reconciliation (Codex item 2)
The overlap matrix above used **`de246659`** (`delivery/runtime-generic-connector-v1`) as "Lane-2".
The delivered frozen product SHA is **`1ed19f09`** (`integration/runtime-lane1-lane2-v2`).
Verified ancestry: `de246659` is **NOT** an ancestor of `1ed19f09` — they are **distinct lineages**
(the integration branch does not contain that delivery commit). Both share base `13f79aa6` with my
HEAD. **Recomputed overlap of my changed files against the delivered `1ed19f09`: still ZERO**, and
`1ed19f09` does not touch `youtab_runtime/memory` or `continuity`. So the zero-overlap conclusion
holds against the delivered SHA. FLAG for the integration reviewer: `de246659` vs `1ed19f09`
divergence should be reconciled on the Lane side (out of Memory scope).

My source files (all additive, no overlap): `youtab_runtime/memory/{scope,claim,router,bus,
budget,tokenizer,simorgh_client,outbox,outbox_sqlite,cache,keystore,placement,retrieval,shadow}.py`,
`youtab_runtime/continuity/{__init__,checkpoint}.py`. `a990839b` (continuity) remains EXCLUDED
pending Durable owner adoption; it is a leaf (nothing in Memory imports it).

## 2. outbox_sqlite vs existing stores — authoritative-store decision

On base there is **no memory outbox / MemoryBus / Simorgh-sync store**. The existing durable
queues are different domains: `gateway/delivery_ledger.py` = chat final-response delivery;
`tools/async_delegation.py` = task/delegation completion delivery. Neither is memory transport.

- **Server mode (TARGET CONTRACT, not a live result):** Simorgh (via MemoryBus) is *intended* to
  be authoritative for organizational memory. **CORRECTION (Codex):** this is the target contract,
  NOT a live server result — **server memory transport remains NOT INTEGRATED and fail-closed**
  while the MemoryBus endpoint and Gateway signer are absent (`SimorghClientConfig.enabled=False`,
  no transport shipped).
- **Local/offline mode:** `SqliteOutbox` is a LOCAL producer-side queue of pending memory-transport
  events (promotion candidates / feedback / supersede / erasure). It is **never authoritative**, and
  it remains local/offline **pending an end-to-end synchronization proof** (no live sync exists yet).
- **No two authoritative queues** (delivery_ledger is chat, distinct). **No silent sync** (sync would
  be an explicit call through the disabled client, never implicit). **No duplicate writes**: stable
  `event_id`; a conflicting re-enqueue (same id, different digest, or a different scope partition)
  now **fails closed with `OutboxConflict`** rather than being silently ignored by `INSERT OR IGNORE`
  (identical re-enqueue stays idempotent → False); ack requires a matching payload digest;
  `convergence_cursor` advances only on ack. Simorgh remains the single promotion/validation
  authority; the Runtime only proposes.

## 3. Platform matrix (do NOT claim Linux enterprise cache works)

| Concern | Windows Desktop | Linux server | Offline mode |
|---|---|---|---|
| Key provisioning | `DpapiKeyStore` (DPAPI user scope) auto via `default_keystore()` | `default_keystore()` returns **None** → cache **DISABLED** unless an explicit `ProvisionedKeyStore` (KEK from a secret manager) is injected. **libsecret/Keychain NOT implemented.** | same as host; keystore must be reachable offline (DPAPI is; a remote KMS may not be) |
| Rotation | KEK re-wrap via `cache.rewrap(new_keystore)`; `key_version` bump | `ProvisionedKeyStore` rotation by supplying a new KEK + `rewrap` | supported if the new KEK is available offline |
| Recovery | reopen over same dir; DEK unwrapped via DPAPI | reopen with the SAME provisioned KEK; wrong/absent KEK → fail closed | reopen offline with local KEK |
| Cache-disabled behavior | rare (DPAPI present) | **default** when no `ProvisionedKeyStore` — `status()=="disabled_no_keystore"`, all ops raise `CacheDisabled`, **no plaintext** | disabled if KEK unavailable |
| Encryption at rest | AES-256-GCM, per-record nonce, DEK wrapped by DPAPI | AES-256-GCM, DEK wrapped by provisioned KEK | same |
| Erasure | `delete` / `tombstone` / `purge_scope` | same | queued erasure/tombstone via outbox on reconnect |
| Corruption handling | AEAD `InvalidTag`/JSON/decode error → quarantine, return None | same | same |

**Honest gap:** on Linux/macOS the enterprise cache is DISABLED unless a `ProvisionedKeyStore`
is explicitly wired to a real secret manager; native Keychain/libsecret wrappers are NOT built.

## 4. Placement signature contract — LIVE blockers

Present (Runtime side): `SignedScopePlacement` (`youtab.scope-placement.v1`), `PlacementVerifier`
(algorithm allowlist ed25519, issuer, issued/expiry, cross-check command/trace/tenant/principal
vs the signed envelope, Ed25519 verify, in-memory replay cache) → distinct `VerifiedPlacement`.
Retrieval consumes only `VerifiedPlacement`; **JSON-schema validation alone is not authorization.**

**Replay protection classification (Codex item 5): SINGLE-PROCESS ONLY.** The verifier's
`(tenant_id, nonce)` replay cache is in-memory and per-verifier-instance; cross-process /
cross-restart replay rejection is **NOT proven** and must be classified as unimplemented until a
durable shared replay store is integrated and tested.

**LIVE remains DISABLED until integrated with Gateway:**
- a real Gateway **signer** issuing `youtab.scope-placement.v1`;
- a **trusted key registry** (key_id → public key) with distribution;
- **revocation** + rotation wired to that registry;
- a **durable cross-process replay cache** (current cache is single-process, in-memory);
- integration tests against the real signer, including cross-process replay rejection. Gateway
  files are NOT modified here (`docs/architecture/gateway_signed_scope_placement.schema.json`).

## 5. Production call path that will replace the 2200 truncation (SHADOW-ONLY until approved)

Current (unchanged): `agent/agent_init.py:1624-1628` constructs `MemoryStore(2200,1375)` →
`agent/system_prompt.py:503-517` injects `format_for_system_prompt()` (the flat char capsule,
already storage-bounded, no re-truncation).

Planned replacement (NOT wired; behind disabled flags):
1. **Keep storage admission** at `tools/memory_tool.py` (2200/1375 unchanged) — durable capsule.
2. At the injection point (`system_prompt.py:503-517`), when `RetrievalConfig.enabled` and
   `ShadowConfig.token_budgeted_injection_enabled` are true, source the injected block from
   `RetrievalPipeline.retrieve()` instead of the flat capsule:
   - scope = `VerifiedPlacement` (tenant/org/workspace/principal/agent/run/purpose);
   - lifecycle + authorization filter (only VALIDATED, scope-matched claims);
   - **citations/provenance** carried on each `RetrievedMemory` (citation + claim provenance);
   - **bounded injection** via `budget.allocate` + `tokenizer.account`; large artifacts returned
     as references, not inlined; prompt size bounded by model budget, not corpus size;
   - **prompt-injection handling**: retrieved memory is untrusted data (AGENTS.md); trust levels +
     lifecycle gating; the model-facing block is labelled non-authoritative.
3. **Latency**: measurable via retrieval digest + token accounting; a p50/p95 retrieval benchmark
   is REQUIRED before enablement and is **not done** (excluded, below).

Enablement is gated on: ADR-0006 ratification, Gateway signer, and a latency/quality benchmark.

## 6. What the 647 tests cover / exclude

`tests/youtab_runtime` full suite = **647 passed** (two serial isolated-basetemp runs). My
additive coverage (~140 tests): scope binding & isolation; MemoryClaim shape + lifecycle
(PENDING/VALIDATED/SUPERSEDED/REJECTED/TOMBSTONED, no self-validation of AI_INFERRED); router
default-deny; MemoryBus reference/live TYPE separation + fail-closed `consume_for_live`; token
budget (derived, configurable) + tokenizer (Persian/Dutch/English/JSON/code/SAP/CAD, sublinear
prompt, pre-overflow compaction); Simorgh client (disabled, deadline/retry/breaker/digest/
redaction/no-fallback); in-memory + SQLite outbox (incl. 2 real subprocess durability tests);
encrypted cache (incl. Windows DPAPI round-trip, tamper/TTL/tombstone/rotation/bounded/disabled);
placement verification (bad-sig/expired/cross-tenant/replay/unknown-key/non-forgeable); retrieval
pipeline (disabled→LIVE_MEMORY_UNAVAILABLE, bounded injection, artifact refs); shadow (10 domain
scenarios, never-sent, redacted-metrics, huge-corpus-bounded).

**Explicitly EXCLUDED (not covered):** a real Simorgh endpoint; a real Gateway signer / key
registry / revocation; live prompt integration (shadow-only); cross-host / distributed durability
(SQLite proves LOCAL only); Linux/macOS native Keychain/libsecret key stores; durable cross-process
replay cache; scale (100K/1M-claim) + p50/p95 latency + recall/precision benchmark; LightRAG/
GraphRAG federation (authority-side, Simorgh); the checkpoint capsule integration (`a990839b`,
Durable-owned).

## Remaining blockers to LIVE / integration
- Lanes are behind main → Master must forward-integrate them onto `c7650a1b` (independent of me).
- Gateway signer + trusted key registry + revocation + durable replay cache (item 4).
- ADR-0006 exact-content ratification.
- Linux/macOS key store wrappers before enterprise cache is usable off-Windows.
- Latency/scale/recall benchmark before token-budgeted injection is enabled.
- Durable owner decision on `a990839b`.
