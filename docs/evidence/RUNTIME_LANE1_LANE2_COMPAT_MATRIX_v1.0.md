# Lane 1 ↔ Lane 2 — Semantic API/Schema Compatibility Matrix v1.0

Read-only analysis. No Lane-2 files were modified. Frozen SHAs:
- Lane 1: HEAD of `delivery/runtime-folder-grant` (this delivery).
- Lane 2: `1ed19f0914cce6d074aae6cf378d81f05bc6d283`.
- Common base (`git merge-base`): `ca89219ae0925767f306ac9874c540813e0fd34e`.

**File overlap is 0** (Lane 1 changed `youtab_runtime/*.py` contract modules; Lane 2
added only `youtab_runtime/enterprise/*`), and `git merge-tree` reports **0 textual
conflicts**. That is necessary but **not sufficient**: Lane 2's `enterprise/*`
connector *consumes* the shared contracts Lane 1 changed (effect_authorization ×5
files, effect_ledger ×7, approval ×3, worker_lease ×3, grant_fs ×0 real uses).
This matrix is the semantic check. In the integrated tree the shared modules are
Lane 1's versions (Lane 2 did not modify them), so the question is whether Lane 1's
contract still satisfies Lane 2's usage.

Classes: **COMPATIBLE** · **ADAPTER_REQUIRED** · **BLOCKING**.

| # | Contract surface | Lane 2 usage | Lane 1 change since base | Class |
|---|---|---|---|---|
| 1 | `EffectAuthorization` schema/fields | constructs + verifies; `schema_version` v1 | `operation` enum widened (+`delete`,`move`); added **optional** `proposal_id`, `request_digest` (default `None`); `schema_version` unchanged | **COMPATIBLE** (purely additive; Lane 2's 3-op usage is a subset) |
| 2 | `operation` values (write/delete/move) | emits/handles `read`/`write`/`create` only | added `delete`,`move` | **COMPATIBLE** (additive; Lane 2 never emits the new values) |
| 3 | request/effect digest (`approval.compute_effect_digest`, `content_digest`) | `from youtab_runtime import approval` | `approval.py` **unchanged** since base | **COMPATIBLE** |
| 4 | approval consume semantics (`reserve_and_consume_authorization`) | via `approval` | `approval.py` **unchanged** | **COMPATIBLE** |
| 5 | workspace/tenant/principal binding (`effect_ledger.get_effect`, `list_effects`, `begin_effect`, `try_claim`, `compute_effect_id`, `mark_committed`, `mark_unknown`) | uses the **principal-scoped** functions | Lane 1 only **added** `get_effect_in_workspace` / `list_effects_in_workspace`; existing signatures unchanged | **COMPATIBLE** (optional hardening: Lane 2 could adopt the workspace-scoped lookups) |
| 6 | receipt schema (`EffectRecord`) | consumes records | fields unchanged | **COMPATIBLE** |
| 7 | ledger state transitions (`run_states.EFFECT_TRANSITIONS`) | `mark_committed`/`mark_unknown`/`try_claim` | `run_states.py` **not changed** by Lane 1 | **COMPATIBLE** |
| 8 | lease ops (`LeaseError`, `assert_lease_holder`, `lease_detail`, `renew_lease`, `sweep_expired_leases`) | used across `enterprise/*` | **unchanged** by Lane 1 (only `EffectEvidence`/`reconcile` touched) | **COMPATIBLE** |
| 9 | **reconciliation records — `worker_lease.EffectEvidence` + `worker_lease.reconcile_to_terminal`** | Lane 2 uses `_lease.EffectEvidence(...)` (1×) and `_lease.reconcile_to_terminal(...)` (1×) with the **old** field-equality contract | Lane 1 **removed** `worker_lease.EffectEvidence` and changed `reconcile_to_terminal` to require a **verified** `youtab_runtime.effect_evidence.ReconciliationEvidence` (new signature: `reconcile_to_terminal(effect_id, principal, evidence: ReconciliationEvidence, *, workspace_id, now, max_age_seconds=3600, live_provider_keys=None, recompute_reference=None, db_path=None)`) | **ADAPTER_REQUIRED / BLOCKING** |
| 10 | `grant_fs` | one docstring reference only, no import/call | delete/move ops + handle-final-path added | **COMPATIBLE** (no real dependency) |

## The one blocking row (#9) — precise adapter

Lane 2's reconciliation was written against the pre-Lane-1 `worker_lease.EffectEvidence`
(a field-equality dataclass) and the old `reconcile_to_terminal`. Lane 1's
correction (non-forgeable evidence, ADR-0005 / `effect_evidence.py`) removed that
dataclass and requires a signed (LIVE) or independently-recomputed (REFERENCE)
`ReconciliationEvidence`. In the integrated tree, `worker_lease.EffectEvidence`
does not exist and `reconcile_to_terminal` rejects the old call shape.

**Master-Integrator adapter (do NOT apply here; Lane 2 is frozen):** migrate the
Lane-2 `enterprise/*` reconciliation callsite to build a
`youtab_runtime.effect_evidence.ReconciliationEvidence` (its deterministic
REFERENCE providers can supply a `recompute_reference`, or its Gateway adapter a
signed LIVE evidence) and call the new `reconcile_to_terminal` signature. This is a
bounded, additive change to Lane-2 code only — no Lane-1 change is required, and no
Lane-1 security property is relaxed. Until applied, the integrated build is
**NO-GO** on the reconciliation path.

## Summary

- **9 of 10 rows COMPATIBLE** (additive widening, unchanged signatures, or new
  functions alongside the old).
- **1 row ADAPTER_REQUIRED/BLOCKING** — Lane-2 reconciliation must adopt Lane-1's
  non-forgeable evidence contract. This is the concrete integration gate that a
  zero-file-overlap / zero-textual-conflict check does not reveal.
