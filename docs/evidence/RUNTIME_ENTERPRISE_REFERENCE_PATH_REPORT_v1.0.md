# Runtime Enterprise Reference Path — Canonical Evidence Report v1.0

Lane 2 (Runtime, vendor-neutral enterprise capability connector). This report is
docs-only evidence; it changes no product/test/dependency file.

## 1. Executive verdict

- **REFERENCE ENTERPRISE PATH VERIFIED_NOT_REVIEWED** — the governed enterprise
  path (signed-authority verification, canonical effect ledger, lease-bound
  worker execution, receipts, evidence-bound reconciliation) is implemented and
  tested against the real current Lane-1 authority, deterministic REFERENCE
  providers, and real worker subprocesses.
- **LIVE VENDOR PATH NOT YET VERIFIED.**
- No claim of LIVE Simorgh authorization, LIVE Gateway adapter execution, or LIVE
  CRM/ERP/SAP/CAD vendor execution. Those require approved exact SHAs and real
  credentials and remain fail-closed.

## 2. Immutable identities

- Base Lane-1 SHA: `ca89219ae0925767f306ac9874c540813e0fd34e`
- Frozen Lane-2 product integration SHA (immutable): `1ed19f0914cce6d074aae6cf378d81f05bc6d283`
- Frozen Lane-2 product **tree** SHA: `6abffdd459b12c9781685d6281f0cbfcb313c1b2`
- Product branch: `integration/runtime-lane1-lane2-v2` (worktree `C:\Users\eiman\worktrees\rt-integration-v2`) — frozen, not modified by this report.
- Evidence branch: `evidence/runtime-enterprise-reference-v1` (worktree `C:\Users\eiman\worktrees\rt-evidence`), based on the frozen product SHA.
- Report/evidence commit SHA: returned externally in the session response (a commit cannot contain its own SHA).

## 3. Full commit list (Lane-1 base → frozen Lane-2 SHA)

Range `ca89219ae0925767f306ac9874c540813e0fd34e..1ed19f0914cce6d074aae6cf378d81f05bc6d283`, newest first:

```
1ed19f0914cce6d074aae6cf378d81f05bc6d283  test(runtime): production=True EffectAuthorization path + consume-before-begin recovery (integration v2)
029308335962a1dc34fc4e0be6e61ffd9b9d4ea8  fix(runtime): deterministic recovery for consume-before-begin failure (integration v2)
500cd590c15b3a4d6cd392831356646062644083  fix(runtime): commit-class effects are always authorization-gated (integration v2)
ce7bb62024a8d32b67e4a44d391675da0ac51b6a  feat(runtime): machine-readable Gateway contract artifacts + parser (integration v2)
749fa556b84d5f88cdc91cd7bd9e6ebf4dbff307  harden(runtime): worker process-tree containment + lease heartbeat/renewal (integration v2)
9fd3ccccf85fb090ca79d68f77727d668e4fc976  feat(runtime): bind Lane-2 connector to current signed-authority Lane-1 model (integration v2)
972c14baaa7eb019cf915984126a12c3d32e8b35  harden(runtime): bound worker request/response sizes; document tree-kill posture (integration)
482eedfa87d07601baa1a9e056e332a0d7c5bd58  refactor(runtime): vendor-neutral guardrails — reference role + library-backed CAD (integration)
466e8581991e45a672061d80bdffb33386695e0c  style(runtime): normalize lane1_connector.py to LF line endings (integration)
36b9fb219a517565a5aa36e00fc960ff4f1a2ad9  docs(runtime): machine-readable Gateway LIVE binding contract (integration)
dc8616dea2eb2a49feecc40987615e488b3d10c6  feat(runtime): Lane-1-integrated governed connector + adversarial proofs (integration)
611bfafaa5e0b9b79f7d65c7828627ed6c0f91a9  feat(runtime): numpy-backed bounded FEA cross-checked vs reference (integration)
722586b45135656cc866c30d5d3654868e1263b3  feat(runtime): Ed25519 production manifest verification (integration)
0ca98bb98d57166bc6ab5e9f2a39f0ff933f76a5  feat(runtime): concrete Lane-1 authority adapter (integration)
f3ccdf91daef63d7edad7a314f3d2eb89984efb4  feat(runtime): governed enterprise connector + adversarial proofs (Lane-2)
4d9c57003f5d19f7dc6d8166328f27c1e8cef3e0  feat(runtime): enterprise reference providers + worker subprocess transport (Lane-2)
000537260ad094038c31ca476a594369eb08ad07  feat(runtime): real bounded CAD geometry/DFM/FEA computation (Lane-2)
4dd45664ca99536f65e0af3c68355a8a7d50b437  feat(runtime): typed Lane-1 authority boundary + idempotency policy (Lane-2)
b57dfd597bca37e11baf2c1e60055eecf416b2b5  feat(runtime): signed typed enterprise capability manifest (Lane-2)
```

Note: the first 13 (oldest) commits were cherry-picked additively from the prior
integration branch onto the current Lane-1 base; the 6 newest are the v2
authority rebind + closure work. All authored/committed by Eiman with zero
machine-assistant attribution or trailers.

## 4. Changed-file inventory (grouped)

All changes are additive; **no Lane-1-owned file was modified**. Scope is
`youtab_runtime/enterprise/`, `tests/youtab_runtime/enterprise/`, `docs/lane2/`.

- **Manifest / signing**
  - `youtab_runtime/enterprise/manifest.py` — typed capability manifest + HMAC test verifier + typed schema/contract primitives.
  - `youtab_runtime/enterprise/manifest_crypto.py` — production Ed25519 signer/verifier, `PublicKeyRegistry` (public keys + revocation), algorithm/issuer/key/expiry/tamper/copied-sig/algorithm-confusion/scope checks.
- **Lane-1 authority adapter**
  - `youtab_runtime/enterprise/authority.py` — typed authority Protocol types + `RuntimeIdempotencyPolicy` (no private ledger coupling).
  - `youtab_runtime/enterprise/lane1_adapter.py` — binds the current Lane-1 signed-authority model: `consume_authorization` → `approval.reserve_and_consume_authorization`; canonical workspace; worker lease; evidence-bound `reconcile_to_terminal`.
- **Connector**
  - `youtab_runtime/enterprise/connector.py` — foundation governed connector (authority-boundary abstraction).
  - `youtab_runtime/enterprise/lane1_connector.py` — production connector on the real Lane-1 spine (verify+consume, begin_effect+try_claim under lease, holder+state-gated settle, query-only reconcile, commit-class invariant, orphan recovery).
- **Worker / subprocess**
  - `youtab_runtime/enterprise/worker.py` — stateless subprocess entrypoint (typed `{ok,result}` envelope; mints no receipt/authority).
  - `youtab_runtime/enterprise/worker_boundary.py` — Popen + Windows Job Object / POSIX process-group; timeout tree-kill; heartbeat + atomic lease renewal; bounded framed I/O; scrubbed env.
- **Reference providers**
  - `youtab_runtime/enterprise/reference_providers.py` — deterministic REFERENCE providers with exact operation ids and read/preview/commit/reconcile semantics; `ENVIRONMENT = "reference"`.
- **CAD/DFM/FEA reference computation**
  - `youtab_runtime/enterprise/cad.py` — hand-written deterministic ORACLE only (geometry/DFM/1-D FEA); not a framework.
  - `youtab_runtime/enterprise/cad_lib.py` — numpy-backed bounded FEA (library adapter) + oracle cross-check within documented tolerance.
- **Recovery**
  - `youtab_runtime/enterprise/recovery.py` — Lane-2-owned durable `authorization_consumed_effect_not_created` recovery ledger.
- **Gateway machine-readable contracts**
  - `youtab_runtime/enterprise/gateway_contract.py` — schema loader + `validate_against` (prefers locked jsonschema; strict stdlib fallback).
  - `docs/lane2/contracts/gateway_manifest.schema.json`, `capability_discovery.schema.json`, `operation_envelope.schema.json`, `reconciliation_evidence.schema.json`, `health.schema.json`.
  - `docs/lane2/GATEWAY_LIVE_CONTRACT.md`, `docs/lane2/INTERFACE_REQUESTS_TO_MASTER.md` (human docs).
  - `youtab_runtime/enterprise/__init__.py`.
- **Tests** (`tests/youtab_runtime/enterprise/`)
  - `__init__.py`, `test_enterprise_manifest.py`, `test_manifest_crypto.py`, `test_enterprise_cad.py`, `test_cad_lib.py`, `test_enterprise_connector.py`, `test_lane1_integration.py`, `test_lane1_production_authority.py`, `test_worker_hardening.py`, `test_gateway_contract.py`.

## 5. Architecture boundary

- The Runtime is **vendor-neutral**: it owns verified capability manifests,
  tenant/principal/workspace binding, authority verification, idempotency/replay
  protection, worker execution, the canonical effect ledger, immutable receipts,
  reconciliation, and generic request/result envelopes.
- The **Gateway** owns thin vendor/tool adapters (auth/secret-store access,
  vendor API mapping, vendor schemas, rate limits/retries, webhook/poll
  reconciliation, provider idempotency, health/capability discovery, translation
  to/from canonical Runtime envelopes).
- The Runtime builds **no** CRM database, ERP inventory/order system, SAP
  business logic, vendor credential flow, vendor-specific client, CAD framework
  or production FEA framework.
- REFERENCE providers are deterministic **test/reference boundaries only**,
  labelled `REFERENCE`/`ENVIRONMENT = "reference"`, and never mint authority,
  effect or receipt.
- The **Frontend must visibly distinguish REFERENCE from LIVE** (provenance is on
  every receipt); a REFERENCE result must never be presented as LIVE vendor data.

## 6. Authority and security model

- Flow: **EffectProposal → Simorgh/Brain-signed EffectAuthorization → Runtime
  verification → single-use consumption → canonical effect claim → lease-bound
  worker → settlement/reconciliation → workspace-bound receipt.**
- **Production Ed25519 verification**: authorization is an externally signed
  `EffectAuthorization`; the Runtime holds only public keys (`resolve_authority_public_key`
  / `PublicKeyRegistry`) and verifies signature + expiry.
- **The Runtime never mints authority** — `issue_approval`/`consume_approval` are
  gone from Lane-1 and are never called anywhere in the enterprise package.
- **Single-use consumption** via Lane-1 `approval.reserve_and_consume_authorization`
  (durable consumed-authorization table); replay is rejected.
- **Binding**: tenant / principal / workspace / capability / effect-digest are
  all checked before consume.
- **Idempotency / effect identity**: `effect_id = compute_effect_id(run, principal,
  operation, scope)` where scope folds workspace + capability id+version +
  operation + request digest + provider + idempotency key — so changed
  payload/version/provider/workspace/tenant cannot reuse an effect.
- **Lease + heartbeat**: claim records a lease; settlement asserts holder + state;
  heartbeat performs atomic holder-only renewal; expiry sweep dead-letters at a
  bounded attempt ceiling.
- **Canonical receipt** is minted by the Runtime from ledger state (never from
  worker output).
- **Evidence-bound reconciliation**: an ambiguous effect is moved terminal only
  with a verifiable Lane-1 `EffectEvidence` (effect id + workspace + operation
  digest + freshness); forged/mismatched/stale evidence is refused.
- **Cross-workspace receipt isolation**: same tenant+user in a different
  workspace cannot read a receipt (workspace folded into effect identity +
  `assert_workspace`).

## 7. Production-mode proof

`tests/youtab_runtime/enterprise/test_lane1_production_authority.py`:

- Integrated **`production=True`** path: adapter `production=True`, manifest
  verified by the production Ed25519 `CryptoManifestVerifier`, signed
  `EffectAuthorization`, real Lane-1 consume, canonical ledger, real subprocess
  worker, workspace-bound receipt.
- **Ephemeral non-test Ed25519 keypair** (non-test issuer `brain-effect-authority`,
  non-test key id) loaded through the real production authority registry; the
  **private key exists only in-process and is never committed**.
- Negatives on the same production path: **test issuer rejected** at
  `production=True`; **unknown key**; **revoked (rotated-out) key**; **wrong
  workspace**; **wrong request digest**; **expired**; **copied** (bound to a
  different effect); **replay** (single-use second consume). All fail closed with
  zero effect.

## 8. Worker proof

`tests/youtab_runtime/enterprise/test_worker_hardening.py` (+ connector suites):

- **Real subprocess** execution (`sys.executable -m youtab_runtime.enterprise.worker`).
- `shell=False`; **environment scrubbed** to an allowlist (ambient secrets not
  seen by the child); controlled cwd.
- **Bounded request/response** sizes (fail closed when exceeded); strict framed
  `{ok,result}` protocol; malformed/oversized/refusal/non-zero-exit fail closed.
- **Timeout** enforced; **Windows Job Object** (pywin32, locked;
  `CREATE_NEW_PROCESS_GROUP` + `taskkill /T` fallback) / **POSIX process-group**
  (`start_new_session` + `killpg`) so timeout terminates the **complete child
  process tree**; a detached grandchild is proven **orphan count == 0**.
- **Heartbeat / atomic lease renewal**; lease loss fails closed at settlement;
  bounded restart/attempt ceiling dead-letters instead of looping.
- **Worker cannot mint or forge a receipt** — a forged top-level `receipt` in
  worker stdout is ignored; the Runtime mints the receipt from ledger state.

## 9. Reference operation matrix

All results are explicitly labelled `REFERENCE` (provenance on every receipt).

| Domain | read | preview (0 effect) | commit (exactly 1 effect) | reconcile (query/verify) |
|---|---|---|---|---|
| CRM | `crm.contact.read` | `crm.contact.update.preview` | `crm.contact.update.commit` | `crm.effect.reconcile` |
| ERP | `erp.inventory.read` | `erp.order.preview` | `erp.order.commit` | `erp.effect.reconcile` |
| SAP | `sap.business_object.read` | `sap.business_object.update.preview` | `sap.business_object.update.commit` | `sap.effect.reconcile` |
| CAD | `cad.geometry.inspect` | `cad.dfm.check` | `cad.fea.run` (governed, numpy) | `cad.effect.reconcile` |

- **Preview / read / CAD inspect+DFM** create **zero** effect.
- **Commit** (incl. governed `cad.fea.run`) creates **exactly one** effect and is
  approval-gated (commit-class invariant enforced fail-closed).
- **Reconcile** creates no new effect (query/verify only).

## 10. CAD / FEA truthfulness

- The hand-written `cad.py` solver is an **oracle/reference only** (deterministic
  geometry/DFM/1-D FEA); it is not exposed as a production framework.
- The bounded FEA proof runs through `cad_lib.py`, a **numpy-backed** adapter
  (assemble global stiffness, `numpy.linalg.solve`, `LinAlgError` → fail closed,
  hard element bounds, deterministic checksum).
- `cad_lib.cross_check_against_reference` runs both and asserts tip-displacement
  agreement within a **documented `rel_tol = 1e-6`** relative tolerance.
- This is **not** a LIVE CAD vendor/tool adapter, and **no new CAD framework was
  created**.

## 11. Consume-before-begin recovery

- If a signed authorization is single-use consumed but effect
  creation/reservation then fails, the connector records a durable, idempotent
  **`authorization_consumed_effect_not_created`** recovery row
  (`youtab_runtime/enterprise/recovery.py`), keyed by the single-use
  authorization id and bound to the **effect id + effect digest**.
- Proven (`test_consume_then_begin_failure_is_recoverable`): **no external
  execution** (worker runs only after the claim), **no committed effect**, **no
  silent double-execution**, a **stable fail-closed error**, and a retry with the
  now-consumed authorization is **refused** — the operation needs a freshly
  signed authorization or explicit reconciliation of the recovery row.
- **Master-verifier requirement (open):** confirm the recovery row is also
  **tenant/principal/workspace scoped** (v1 keys by authorization id + effect id
  + effect digest; the effect digest already binds workspace, but an explicit
  tenant/principal/workspace column is recommended), and that a **persistence
  failure of the recovery write itself remains fail-closed** (currently the
  recovery insert precedes the raised error; a store failure there should not
  mask the fail-closed refusal).

## 12. Machine-readable Gateway contract

- JSON Schemas (draft 2020-12):
  - `docs/lane2/contracts/gateway_manifest.schema.json`
  - `docs/lane2/contracts/capability_discovery.schema.json`
  - `docs/lane2/contracts/operation_envelope.schema.json`
  - `docs/lane2/contracts/reconciliation_evidence.schema.json`
  - `docs/lane2/contracts/health.schema.json`
- Parser: `youtab_runtime/enterprise/gateway_contract.py` (`load_schema`,
  `validate_against`; prefers locked `jsonschema`, strict stdlib fallback), parsed
  and enforced by `tests/youtab_runtime/enterprise/test_gateway_contract.py`.
- Contracts cover: signed manifest, capability discovery, operation
  request/response envelope, reconciliation evidence, health/availability.
- **Exact remaining Gateway dependency:** an approved Gateway SHA that serves an
  Ed25519-signed LIVE manifest matching `gateway_manifest.schema.json` (public
  verification keys provisioned into the Runtime), real vendor executors, and a
  health endpoint reporting its `gateway_sha`.

## 13. Qualification evidence

Run on the frozen product SHA `1ed19f091` (product worktree). Exit codes as observed:

| Command | Result |
|---|---|
| `PYTHONPATH=. py -3 -m pytest tests/youtab_runtime/enterprise/ -q` | **165 passed / 0 failed / 0 skipped** (exit 0) |
| `py -3 -m ruff check youtab_runtime/enterprise/ tests/youtab_runtime/enterprise/` | All checks passed (exit 0) |
| `PYTHONPATH=. py -3 -m mypy youtab_runtime/enterprise/ --ignore-missing-imports --no-error-summary` | 0 enterprise errors (exit 0) |
| `py -3 -m py_compile youtab_runtime/enterprise/*.py tests/youtab_runtime/enterprise/*.py` | OK (exit 0) |
| `git diff --check ca89219ae..1ed19f091` | clean (exit 0) |
| CRLF scan (all changed blobs) | none (LF only) |
| secret / machine-path scan (committed diff) | 0 hits |
| attribution scan (committed diff) | 0 hits; author `Eiman Ghazaei`, zero trailers |
| worker orphan check (`enterprise.worker` processes) | **0** |
| worktree status | clean |

Per-suite counts: manifest 15, manifest_crypto 20, cad 16, cad_lib 12,
connector 25, lane1_integration 24, lane1_production_authority 11, worker_hardening
17, gateway_contract 27 (total 165).

## 14. Explicit non-claims and dependencies

- **No** LIVE Simorgh authorization claim (production authority keys come from the
  Brain; tests use an ephemeral in-process key and prove the production guard).
- **No** approved Gateway SHA yet; LIVE manifests/executors are not present.
- **No** real vendor credentials.
- **No** LIVE CRM/ERP/SAP/CAD execution.
- **No** push / merge / deploy / amend / rebase / squash was performed.
- **Final Master integration is still required** against the frozen Lane-1,
  Lane-3, and approved Gateway SHAs.

## 15. Integration handoff (for the Master Integrator)

- **Frozen source SHA to consume:** `1ed19f0914cce6d074aae6cf378d81f05bc6d283`
  (tree `6abffdd459b12c9781685d6281f0cbfcb313c1b2`).
- **Order/strategy:** rebuild the final integration candidate on the final frozen
  Lane-1 SHA; apply the 19 Lane-2 commits additively (they touch only
  `youtab_runtime/enterprise/`, `tests/youtab_runtime/enterprise/`, `docs/lane2/`),
  then apply only the compatibility changes any newer Lane-1 public-API delta
  requires. Do not rebuild against intermediate Lane-1 commits.
- **Expected public interfaces consumed from Lane-1** (call-only, do not edit):
  `approval.reserve_and_consume_authorization`, `approval.compute_effect_digest`,
  `approval.content_digest`; `effect_authorization.EffectAuthorization`,
  `resolve_authority_public_key`, `load_brain_public_keys`;
  `effect_ledger.{begin_effect,try_claim,mark_committed,mark_unknown,get_effect,list_effects,compute_effect_id}`;
  `worker_lease.{lease_detail,assert_lease_holder,assert_workspace,sweep_expired_leases,reacquire_expired_lease,renew_lease,reconcile_to_terminal,EffectEvidence}`;
  `run_journal.{Principal,default_db_path}`.
- **Collision boundaries:**
  - Lane-1: none — no Lane-1 file is modified; only public APIs are called. If a
    public-API gap appears, submit an interface request (see
    `docs/lane2/INTERFACE_REQUESTS_TO_MASTER.md`), do not edit Lane-1.
  - Lane-3 (`apps/desktop/packaging/**`, Electron lifecycle, E2E): no overlap.
  - Gateway: consumes the machine-readable contracts under `docs/lane2/contracts/`.
  - Frontend: must render REFERENCE vs LIVE from receipt provenance.
- **Gates the Master must rerun after integration:** full `pytest
  tests/youtab_runtime/enterprise/`; ruff; mypy; py_compile; `git diff --check`;
  CRLF scan; secret/machine-path scan; attribution scan; worker orphan check;
  clean-worktree check; plus the open Master-verifier item in §11 (recovery-row
  tenant/principal/workspace scoping + recovery-persistence fail-closed).

---

*Evidence report only. It does not modify the frozen product/source candidate and
makes no LIVE claim.*
