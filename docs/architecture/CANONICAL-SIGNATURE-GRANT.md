# Canonical signature & execution-grant specification — v2

**Status:** Normative · **Owner-authorized:** 2026-09-07 (WAVE-30H, R1/R3)
**Shared by:** `youtab-ai-os` (gateway connector) and `Youtab-Agent-Runtime` (engine verifier)
**Related:** `ADR-0002-simorgh-admission-execution-contract.md`, `ADR-0001-authentication-and-trust-boundary.md`

This is the **single canonical contract** for the two authentication layers on the managed
Agent Runtime path. Both repositories MUST implement it byte-for-byte; the golden vectors in
§4 are the cross-repo conformance oracle. A change to either layer's field set, order,
separator, or digest rule is a **breaking protocol change** and MUST bump the version literal
and add new golden vectors — never silently alter an existing version.

There are **two independent layers** (ADR-0002 DP1); they compose, neither substitutes the other:

| Layer | Question it answers | Key model | Spec |
|---|---|---|---|
| **Transport signature** (HMAC-SHA256) | *Did the trusted connector send this exact request, fresh and unreplayed?* | symmetric shared secret | §2 |
| **Execution grant** (Ed25519) | *Did Simorgh authorize THIS objective/scope/budget for this principal?* | asymmetric; engine holds public key only | §3 |

---

## 0. The defect this version closes (R1)

At baseline (Runtime `f0950c264`, AI OS `3c4bcb4f`) the two sides disagreed on the transport
canonical string:

- The **engine verifier** signed **8 fields** — `method, path, tenant, user, timestamp, nonce, body_sha256, correlation_id` (canonical *v1*).
- The **gateway connector** signed **7 fields** — it **omitted `correlation_id`** (`services/gateway/app/agents/runtime_connector.py::_sign_headers`, joined `method|path|tenant|user|ts|nonce|body_sha256` only).

Result: **every** signed mutation (`create_run`/`cancel`/`retry`) produced a signature over 7
fields while the engine verified over 8 — the digests never matched and managed mutations were
rejected `bad_signature`. This spec supersedes both with **canonical v2**, which additionally
binds `protocol_version`, `workspace`, and `key_id` per the Owner's mandated field set, and is
implemented identically on both sides. `correlation_id` MUST remain a signed field; unsigned
mutation requests MUST NOT be accepted.

---

## 1. Versioning & negotiation

- The transport version literal is **`youtab.runtime-sig.v2`**; the grant `schema_version` is **`youtab.agent-command.v2`**.
- The version literal is **field 1** of each canonical string / the first key of the grant, so a
  verifier can select the right builder before computing anything.
- The request carries `X-Youtab-Runtime-Proto: youtab.runtime-sig.v2`. A verifier MAY accept a
  set of known versions during a rotation window (e.g. accept both `v1` and `v2`), selecting the
  builder by the header; it MUST reject an unknown/absent version fail-closed.
- `key_id` selects **which** shared secret (transport) or **which** Ed25519 public key (grant),
  enabling overlap-based rotation without a flag day.

---

## 2. Transport signature (HMAC-SHA256) — canonical v2

### 2.1 Canonical string

Exactly these **11** components, in this order, joined with a single `"\n"` (U+000A), no
trailing newline:

| # | Component | Source | Notes |
|---|---|---|---|
| 1 | `protocol_version` | literal | `youtab.runtime-sig.v2` |
| 2 | `method` | HTTP verb, upper-cased | e.g. `POST` |
| 3 | `path` | request path only | no query string; exact, e.g. `/api/runtime/v1/runs` |
| 4 | `tenant_id` | gateway-verified identity | |
| 5 | `workspace_id` | gateway-verified identity | `-` (single hyphen) if the request is not workspace-scoped |
| 6 | `user_id` | gateway-verified identity | |
| 7 | `timestamp` | unix seconds, decimal string | signer's clock at signing |
| 8 | `nonce` | unique single-use token | `n-<uuid4hex>` recommended |
| 9 | `body_sha256` | lowercase hex `sha256(raw_wire_body_bytes)` | empty body ⇒ `sha256(b"")` |
| 10 | `correlation_id` | per-request correlation | MUST match `^[A-Za-z0-9_.:-]{8,128}$` |
| 11 | `key_id` | shared-secret id | selects the HMAC secret |

**Body digest rule:** the digest is over the **exact bytes on the wire**. The signer MUST
serialize the JSON body once with compact separators `(",", ":")` and send *those same bytes*
(`content=body_bytes`), so signed bytes == wire bytes. The verifier hashes the raw received body.

### 2.2 Signature & headers

- `signature = hex( HMAC_SHA256(secret, canonical.encode("utf-8")) )` — lowercase hex.
- Headers on the request:
  - `X-Youtab-Runtime-Proto: youtab.runtime-sig.v2`
  - `X-Youtab-Runtime-Timestamp: <field 7>`
  - `X-Youtab-Runtime-Nonce: <field 8>`
  - `X-Youtab-Correlation-Id: <field 10>`
  - `X-Youtab-Runtime-KeyId: <field 11>`
  - `X-Youtab-Runtime-Signature: <hex>`
- `tenant_id`/`workspace_id`/`user_id` travel on their existing identity headers.

### 2.3 Verification (fail-closed order)

1. secret missing/short (`< 43` chars) ⇒ `secret_unavailable` (503).
2. any of timestamp/nonce/signature/correlation/key_id header missing ⇒ `missing_signature` (401)
   (missing correlation ⇒ `missing_correlation` 401; malformed ⇒ `invalid_correlation` 400).
3. unknown `protocol_version` ⇒ `unsupported_proto` (400).
4. `|now - timestamp| > window` (default 300s) ⇒ `expired` (401).
5. `hmac.compare_digest(expected, presented)` false ⇒ `bad_signature` (401).
6. **only then** atomically `claim(nonce)`; loser ⇒ `replayed` (409). (See R2 / `runtime_command_auth.py`.)

A bad-signature or expired probe MUST NOT consume a nonce.

---

## 3. Execution grant (Ed25519) — `youtab.agent-command.v2`

The grant is the canonical source of **execution authority** (ADR-0002 DP1). It is minted and
signed **only** by Simorgh; the engine holds only the public key and can never forge one.

### 3.1 Field set (superset of v1 `BrainCommandEnvelope`)

Bound fields (all present ⇒ `extra="forbid"`, frozen):

`schema_version` (`youtab.agent-command.v2`), `issuer` (`youtab-one-brain`), `audience`
(`youtab-agent-runtime`), `protocol_version`, `command_id`, `task_id`, `root_run_id`,
`parent_task_id?`, `attempt`, `tenant_id`, `workspace_id`, `user_id`, `membership_generation`,
`authorization_epoch`, `agent_id`, `engine_id`, `trace_id`, `nonce`, `objective`,
`allowed_toolsets`, `allowed_memory_scopes`, `allowed_artifact_scopes`, `effect_proposal_scopes`,
`reasoning` (shared budget — see §3.3), `issued_at`, `expires_at`, `key_id`, `signature`.

New in v2 vs v1: `protocol_version`, `root_run_id`, `attempt`, `workspace_id`,
`membership_generation`, `authorization_epoch`, `agent_id`, `engine_id`,
`allowed_artifact_scopes`, and the budget extensions in §3.3.

### 3.2 Canonical payload & signature

- `canonical_payload = json.dumps(model_dump(mode="json", exclude={"signature"}), ensure_ascii=False, sort_keys=True, separators=(",",":")).encode("utf-8")`.
- `signature = base64( Ed25519_sign(brain_private_key, canonical_payload) )`.
- Verification: decode `key_id`→public key via the engine's key resolver; check
  `issued_at <= now < expires_at` and `now < reasoning.deadline_at`; `public_key.verify(sig, canonical_payload)`.
- Admission additionally: forbid authority-bearing toolsets / sovereign memory scopes; atomic
  **durable** replay `claim((tenant_id, nonce))` (ADR-0002 DP7); revalidate `authorization_epoch`
  and `deadline` at each operation boundary (R4/R5).

### 3.3 Shared budget (one execution tree — R5)

`reasoning` binds the whole tree, never per child:
`max_iterations`, `max_spawn_depth`, `max_concurrent_agents`, `max_total_tokens`,
`max_cost_micros` (monetary), `max_retries`, `deadline_at`. Delegated agents inherit the
**remaining** root budget; a child never receives a fresh unlimited budget. Enforcement is
unconditional for managed runs (independent of any benchmark env var).

---

## 4. Golden vectors (cross-repo conformance oracle)

Both repos MUST assert their implementation reproduces these exactly. Values are **TEST ONLY**.

### 4.1 Transport HMAC v2 — non-empty body

```
secret        = "test-secret-do-not-use-in-prod-000000000000000"   # len 46
protocol      = "youtab.runtime-sig.v2"
method        = "POST"
path          = "/api/runtime/v1/runs"
tenant_id     = "tenant-alpha"
workspace_id  = "ws-default"
user_id       = "user-eiman"
timestamp     = "1700000000"
nonce         = "n-0123456789abcdef0123456789abcdef"
correlation   = "cid-0123456789abcdef"
key_id        = "svc-hmac-2026a"
body_wire     = {"agent":"default","task":"hello"}      # compact separators
body_sha256   = 085006d1410a7ca5659c47f7503927c49ddbbff4de3b456164ba247258a6bf94
=> HMAC_SHA256 = d2f948992594ac90d88cf16b2feacbfb9eee20629d4a9c3affc4af08c6d573c0
```

### 4.2 Transport HMAC v2 — empty body (cancel)

```
method="POST"  path="/api/runtime/v1/runs/run-123/cancel"
nonce="n-ffffffffffffffffffffffffffffffff"  correlation="cid-ffffffffffffffff"
body_wire=""   body_sha256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
(other fields as §4.1)
=> HMAC_SHA256 = ffa3e9a2c7616bccdbbea361850f204dbae602173ef189cc267d87d6562d2234
```

### 4.3 Execution grant Ed25519 v2

```
ed25519_private_seed = 32 bytes of 0x01          # TEST ONLY
ed25519_public_key_b64 = iojj3XQJ8ZX9UtstPLpdcspnCb8dlBIb83SIAbQPb1w=
canonical_sha256       = 7e1ce8c8d98969a171f496cc8f95a0c15843510fb4b1049809b005b75606dea9
signature_b64          = n02NH3hhhfLNtIUWwJlsEOn5ECpcAtfcQ/kv8aq1ibPpp2MiWsaqKa2/H6Snpia6Wsnzn+gkW4IyiaIIfYv4Dg==
```
The exact signed payload dict that produces `canonical_sha256` is committed as a fixture in both
repos (`tests/youtab_runtime/golden_grant_v2.json` on the engine side); any field/ordering drift
changes the sha256 and fails the conformance test. These values are produced and self-verified by
`BrainCommandEnvelopeV2` in `youtab_runtime/contracts.py`.

---

## 5. Cross-repo test matrix (R1 / R10)

Each repo carries the golden vectors above plus these negative cases (all fail-closed):
valid create/cancel/retry · wrong key (`key_id` maps to a different secret/pubkey) ·
tampered body/path/method/identity/correlation · version mismatch · timestamp expiry ·
key-rotation overlap (two `key_id`s valid simultaneously). The engine additionally proves
nonce single-use across threads/processes/restart (R2). A live cross-service E2E (gateway↔engine)
is the Checkpoint-C integration proof and runs in the locked environment / CI, not here.

---

## 6. Migration

1. Land v2 builders on both sides behind the version header; keep v1 acceptance for one rotation
   window ONLY if a working v1 pair exists (it does not — v1 was broken by the 7/8 split), so the
   Runtime MAY accept `{v1,v2}` transiently but the gateway emits **v2 only**.
2. Coordinate the exact SHA pair once both green (Checkpoint B).
3. Retire v1 acceptance after the gateway is confirmed v2-only.
