# Enterprise Memory — Pause Handoff (canonical)

Single canonical record before pausing Memory product edits. Docs-only. Not a bundle.
This is a **dependency pause**, not completion or abandonment.

## 1. Exact identity

| Field | Value |
|---|---|
| Repository | `git@github.com:eimanghazaei/Youtab-Agent-Runtime.git` |
| Branch | `feat/runtime-enterprise-memory-v1` |
| Integration base | `c7650a1b920224283ba3a59ca054d6625e07a5f3` (origin/main) |
| **Frozen foundation SHA** | `3e9a3dff6b86f342e1fbb0d510f08397b790b168` |
| Corrected Memory HEAD (prior) | `5bf8e3964aef83aafcd7324d569b34944a3ecc6d` |
| **Latest candidate HEAD (qualification-closure)** | `1a514e26721714667b8184e2dc97745e56aa9158` (tree `6f7e52c1`) |
| Worktree | clean; author=Eiman; zero attribution trailers |

### Qualification-closure update (Codex gaps 1–3 + perf gate)
- Final `tests/youtab_runtime` in a PRIVATE temp = **659 passed / 0 failed, exit 0**; ruff + ty clean.
- Gap 1: failure requires a shared system-TEMP `youtab-test-home-*` (`tests/conftest.py:62`); deterministic
  GREEN in a fully private TEMP (26×3). The specific foreign deleter is UNCONFIRMED (no deletion trace) —
  private-TEMP proves effective isolation, not attribution. Not a Memory-test defect; repo `conftest.py`
  hardening is an OPEN repo/Integrator item. Run overlapping suites with a private per-session TEMP.
- Gap 2: outbox `converged_through()` is a 0-indexed cursor position, not a count; 10000→10000 acks, zero loss.
- Perf: cache PUT is now O(1) (`b9a4adf1`; 31→5.2ms @100, 125→5.1ms @600), correctness preserved.
- Memory candidate: qualification GO; product/live NO-GO (R1–R9 open).
Integrate the LATEST candidate `1a514e26` (it carries the O(1) cache fix + accounting/qualification tests).

Do NOT push/PR/merge/deploy/amend/rebase/squash.

## 2. Implemented and tested (inert, default-disabled)

All under `youtab_runtime/memory/` (+ `youtab_runtime/continuity/`), additive, not imported by
the live agent loop:

- **Scoped encrypted cache** — `cache.py` + `keystore.py` (AES-256-GCM, per-record nonce,
  scope+schema AAD, DPAPI/Provisioned key store, atomic write, TTL, tombstone/erasure, quarantine,
  bounded size, KEK rotation, fail-closed/disabled-no-keystore).
- **Persistent SQLite outbox** — `outbox_sqlite.py` (WAL, transactional fenced claim, cross-process
  durability) + the fail-closed **`OutboxConflict`** correction.
- **MemoryClaim lifecycle** — `claim.py` (PENDING/VALIDATED/SUPERSEDED/REJECTED/TOMBSTONED,
  TrustSource, no self-validation of AI_INFERRED).
- **Placement verifier** — `placement.py` (Ed25519, allowlist, issuer, expiry, envelope cross-check,
  single-process replay) → `VerifiedPlacement`.
- **Disabled retrieval pipeline** — `retrieval.py` (default-disabled → `LIVE_MEMORY_UNAVAILABLE`,
  bounded injection, artifact references, no fallback).
- **Shadow token budgeting** — `shadow.py` (parallel budgeted prompt, never sent, redacted metrics).
- Also foundational: `scope.py`, `router.py`, `bus.py`, `budget.py`, `tokenizer.py`,
  `simorgh_client.py` (disabled), `outbox.py` (in-memory).

### Test evidence (Python 3.12.10; pytest 9.0.2 / ruff 0.15.10 / ty 0.0.21)

| Run | Command | Result | Exit |
|---|---|---|---|
| Full suite, run 1 (at foundation candidate) | `pytest tests/youtab_runtime -q --basetemp=<uniq1>` | **647 passed**, 1 warning | 0 |
| Full suite, run 2 (serial, isolated basetemp) | `pytest tests/youtab_runtime -q --basetemp=<uniq2>` | **647 passed**, 1 warning | 0 |
| Correction run (after `OutboxConflict`) | `pytest tests/youtab_runtime/test_outbox_sqlite.py -q --basetemp=<uniq>` | **17 passed** | 0 |
| Lint (new packages) | `ruff check youtab_runtime/memory youtab_runtime/continuity` | All checks passed | 0 |
| Types (new packages) | `ty check youtab_runtime/memory youtab_runtime/continuity` | All checks passed | 0 |

Distinction: the two **647** runs qualified the foundation (`3e9a3dff`); the **17** run is the
later targeted qualification of the `OutboxConflict` correction (in `5bf8e396`). The full 647 suite
has NOT been re-run at `5bf8e396` (correction is additive + targeted-green; a full re-run belongs to
the Integrator). Every overlapping pytest run MUST use a unique `--basetemp` (see
`TEST_INTERFERENCE_ANALYSIS.md`).

## 3. NOT integrated / unchanged

NOT integrated (fail-closed / disabled):
- Live Simorgh memory transport (client `enabled=False`, no MemoryBus endpoint, no transport shipped).
- Gateway signer / trusted key registry / revocation.
- Durable **cross-process** replay protection (placement replay is single-process only).
- Production retrieval / prompt wiring (retrieval + token budgeting are shadow/disabled).
- Durable Execution checkpoint integration (`a990839b` EXCLUDED, leaf, Durable-owned).
- Linux/macOS native default key stores (`default_keystore()` → None off-Windows → cache DISABLED).

Unchanged (verified): storage limits **2200/1375 UNCHANGED**; **production prompt behavior UNCHANGED**
(`agent_init.py` / `system_prompt.py` untouched).

## 4. Why paused

The required Gateway and Durable Execution interfaces and authority decisions are unavailable.
Proceeding to live wiring would require guessing security contracts (signer format, key registry,
revocation, durable replay, checkpoint/resume ownership). That is unacceptable for a memory-authority
boundary. **This is a dependency pause, not completion or abandonment.** ADR-0006 remains PROPOSED.

## 5. Restart conditions & owners (each OPEN)

| # | Owner | Required artifact / SHA | Acceptance test |
|---|---|---|---|
| R1 | **Gateway** | Signed `youtab.scope-placement.v1` signer + implementation at an exact Gateway SHA (per `gateway_signed_scope_placement.schema.json`) | `PlacementVerifier` accepts a Gateway-signed token; forged/expired/cross-tenant rejected |
| R2 | **Gateway** | Trusted key registry (key_id→pubkey) + distribution + revocation, at an exact SHA | unknown/revoked key_id refused; rotation round-trip |
| R3 | **Gateway** | Durable cross-process replay interface (shared replay store) | replay of a used nonce rejected ACROSS processes/restart |
| R4 | **Durable Execution** | Checkpoint/resume interface: `run_id`, opaque `resume_point_ref`, `committed_effect_refs`, checkpoint digest, terminal status — at an exact SHA (branch `feat/runtime-durable-execution-v1`, latest observed `50a420e7…`, a moving target) | Memory can cite a resume point without owning lease/journal; committed effects not re-proposed |
| R5 | **Durable Execution** | Ownership decision on `a990839b` (adopt / adapt / drop) | if dropped, Memory branch compiles/tests unchanged (leaf) |
| R6 | **Integrator** | Forward-integrate corrected Memory HEAD `5bf8e396` onto `c7650a1b`; forward-integrate Lanes (older base `13f79aa6`) and re-qualify | cross-repo suite green; zero-overlap confirmed |
| R7 | **Owner** | ADR-0006 exact-content ratification; OQ-1 confirmation | ratified ADR present in-tree before any enablement |
| R8 | **Platform** | Linux/macOS key store (libsecret/Keychain) or provisioned KEK wiring | enterprise cache enabled with real at-rest key on Linux |
| R9 | **Owner/Perf** | Latency/scale/recall benchmark harness | p50/p95, recall@k before token-budgeted injection is enabled |

## 6. Integration caution

`5bf8e3964aef83aafcd7324d569b34944a3ecc6d` is the **corrected Memory candidate**. Integrating only
the frozen `3e9a3dff…` would **omit the `OutboxConflict` fail-closed fix**. Integrate `5bf8e396`.
Do **not** count any Memory capability as LIVE or production-ready — everything is inert, disabled,
or shadow-only.

## 7. Final classification

```
ENTERPRISE MEMORY FOUNDATION — IMPLEMENTED_NOT_INTEGRATED
LIVE SIMORGH MEMORY — NOT VERIFIED (server transport NOT INTEGRATED, fail-closed)
TOKEN-BUDGETED PROMPT — SHADOW_ONLY
LONG-RUNNING DURABILITY — OWNED BY DURABLE EXECUTION
PRODUCT NO-GO
```

Memory product edits are PAUSED pending R1–R9.
