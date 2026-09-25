# WAVE-30H SEC-9 — pre-benchmark security remediation

Coordinated remediation of the nine pre-existing unresolved security findings on
a successor branch based exactly on the Codex-GO baseline.

## Exact-SHA manifest

| Field | Value |
|---|---|
| Repository | `eimanghazaei/Youtab-Agent-Runtime` |
| Branch | `feat/wave30h-sec9-remediation` |
| Parent (baseline) SHA | `43075c1105968881d04081a07f80f983e54d7791` (PR #44 head, CI 11/11 green, Codex GO) |
| Candidate SHA | _(assigned at commit; see final report)_ |
| Toolchain | Python 3.12.10, venv `.venv-qual`, Windows 11 |
| PR state | none opened — local candidate only, awaiting Owner exact-SHA push authorization |

No paid provider, metered model, or cloud/Cloud-Benchmark endpoint was contacted. Nothing merged, deployed, or production-activated. PR #44 (`43075c110`) and PR #41 (`3ec256787`) untouched. No branding/banner/logo/theme/asset changed.

## Outcome summary

**8 of 9 findings fully implemented + tested. 1 finding (#1) is reported as
requiring a Simorgh grant-contract decision** because no faithful fail-closed fix
is possible runtime-only without breaking every legitimate managed flow (see
Finding 1). Its root cause is confirmed and the exact enforcement is specified
pending that decision.

## Configurable-budget design (Owner rules)

- **No monetary max-cost value is hard-coded in production source.** The tree's
  `max_cost_micros` is sourced exclusively from the signed grant's
  `reasoning.max_cost_micros` (`execution_tree_budget.reasoning_to_tree_params`,
  opened at `worker_admission`), never a literal. `grep` confirms no euro/dollar/
  micro literal max-cost in the cost path (only the `1_000_000` micro-per-USD
  conversion constant, which is a unit factor, not a ceiling).
- **Pricing is resolved from verified runtime pricing data** (`usage_pricing`);
  a paid managed call with a positive ceiling but no verified pricing **fails
  closed** before the call (`tree_cost_pricing_unavailable`).
- **`max_cost_micros == 0` prohibits every paid provider call** — a verified
  local-zero engine still runs (€0); a known-paid route is refused pre-call.
- **Future Cloud Benchmark token cap is campaign/grant-configured at
  `max_total_tokens = 1_000_000`**, supplied via the signed grant reasoning — NOT
  a production constant. It covers the whole execution tree (root + agents +
  subagents + retries) because the tree is keyed on the authoritative binding
  root that every retry/child inherits (Finding 6), is debited once from canonical
  usage, and stops further provider calls fail-closed when exhausted or when
  accounting fails (Findings 2 + 3). The monetary ceiling is computed by the
  gateway from the selected provider/model pricing at launch; the runtime never
  hard-codes it. **The benchmark was NOT executed in this task.**

---

## Finding-by-finding closure matrix

### Finding 2 — Enforce monetary budget against real provider usage  *(implemented)*
- **Root cause:** the tree's `cost_micros` dimension was dead — `max_cost_micros`
  was stored but never debited; the loop debited only iterations/tokens, so a
  `max_cost_micros=0` grant still permitted unlimited paid calls.
- **Fix:** `agent/managed_budget_gate.py` — new `execution_tree_cost_precheck`
  (pre-call gate: local-zero allowed; subscription-included allowed;
  `max_cost_micros==0` refuses a known-paid route; a positive ceiling with an
  unpriceable non-local route fails closed; exhausted ceiling refuses) and
  `execution_tree_debit_cost` (post-call debit of the ACTUAL priced cost, ceil-
  rounded to micros — conservative). Wired at `conversation_loop` loop-top
  (`execution_tree_pre_iteration` → precheck) and post-call (debit).
- **Tests:** `tests/agent/test_managed_budget_gate.py` — local-zero allowed,
  zero-budget blocks known-paid, zero-budget allows unpriced/local, positive-
  budget-unpriced fails closed, exhausted blocks, cost debit records/saturates/
  ceil-rounds, non-managed no-op.

### Finding 3 — Token/cost accounting failure must fail closed  *(implemented)*
- **Root cause:** `conversation_loop.py` wrapped the tree debit in
  `except Exception: pass`, so a non-`TreeBudgetError` failure (SQLite lock, I/O,
  malformed usage) was swallowed and the loop made another provider call.
- **Fix:** the loop debit block now sets a durable `agent._tree_token_stop =
  "tree_budget_error"` on ANY exception (no swallow); `execution_tree_debit_tokens`,
  `execution_tree_pre_iteration`, and `execution_tree_debit_cost` each end in a
  broad `except → return "tree_budget_error"`. Already-spent usage is preserved by
  best-effort saturation before the stop.
- **Tests:** fault-injection — DB-lock/IO on `consume` in debit-tokens,
  pre-iteration, and debit-cost all return a durable stop (no raise, no swallow).

### Finding 8 — Uppercase scheme in local-zero pricing  *(implemented)*
- **Root cause:** `usage_pricing.is_verified_local_zero_endpoint` used a
  case-sensitive `raw.startswith("http")`, so `HTTPS://127.0.0.1` was prepended
  `http://` and parsed with host `https` → a genuine loopback endpoint was
  misclassified as non-local.
- **Fix:** case-insensitive scheme detection (`re.match(r"^[a-z...]+://", …, I)`);
  only http/https are local-zero candidates (a non-http scheme still fails closed);
  scheme-less input still prepends `http://`. All existing rejections (userinfo,
  public, malformed, 6to4/Teredo IPv6 tunnels) preserved.
- **Tests:** `tests/youtab_runtime/test_usage_pricing_local_zero.py` — uppercase/
  mixed-case loopback is local; userinfo/public/IPv6-tunnel/malformed/non-http
  still rejected; scheme-less still prepends.

### Finding 4 — Compare live tool schema hash before execution  *(implemented)*
- **Root cause:** `CapabilityBinding.authorizes` compared tool NAME only; the
  frozen `(name, schema_hash)` pair's hash half was dead at decision time, so a
  same-name tool re-registered with a changed schema (or via MCP refresh)
  inherited the old authorization.
- **Fix:** `ToolIntent` carries the live `schema_hash`; `tool_executor` resolves it
  from the live registry entry (a missing entry ⇒ `schema_hash=None`);
  `CapabilityBinding.authorizes_pair(name, schema_hash)` requires an exact frozen
  pair, treats `None` as unverifiable (deny), and — under `dynamic_inclusion` —
  authorizes only a concretely-resolved live hash (never a blanket bypass, never a
  null registration). `decide_tool` uses the pair check.
- **Tests:** `tests/youtab_runtime/test_capability_binding.py` — schema-hash drift
  denied, missing-live-hash fails closed, dynamic-inclusion requires a live hash /
  rejects null, unchanged tool still authorized; plus updated manifest/gate2 tests
  pass the live hash.
- **Residual (documented, `xfail`):** `schema_hash` covers the schema JSON only, so
  a handler-only swap with a byte-identical schema is not detected. Closing it
  means folding a handler-identity fingerprint into `schema_hash`, which changes
  the documented `schema_hash` contract (3 tests pin `entry.schema_hash ==
  schema_hash(schema)`) — out of scope for this batch; tracked here.

### Finding 5 — Reject persisted manifests without integrity hashes  *(implemented)*
- **Root cause:** `binding_from_persisted` guarded with `if persisted_hash and …`,
  so a missing/empty `manifest_hash` short-circuited to trusted; and a managed run
  with NO manifest event got a `None` binding → `decide_tool` skipped the manifest
  gate and authorized the full `"*"` envelope.
- **Fix:** `binding_from_persisted(data, *, managed=True)` requires a present,
  well-formed (64-hex), matching `manifest_hash` in managed mode (no reconstruct-
  and-reseal). `worker_admission` passes `managed=True` and, when the manifest event
  is absent, fails closed — with the SAME bounded, audited legacy-quarantine escape
  used for a missing effective binding.
- **Tests:** missing/empty/malformed/mismatched/dynamic-inclusion-tamper rejected in
  managed mode; valid round-trip accepted; worker refuses a managed run with no
  persisted manifest (`no persisted capability manifest`).

### Finding 7 — Collision-free memory namespace encoding  *(implemented)*
- **Root cause:** `MemoryNamespace.safe_parts` replaced every disallowed char with
  `_` — not injective, so `tenant/a` vs `tenant?a` (and case-fold, and NFC/NFD
  forms) collapsed to one directory, crossing tenant boundaries.
- **Fix:** `encode_namespace_component` — NFC-normalize, then a versioned
  (`v1-`), collision-resistant name: a readable lossy slug + a base32 SHA-256
  digest of the normalized bytes. The whole name is lowercase (case-fold-safe on
  Windows/macOS), can never be a reserved device name, and can never contain a
  separator or `..`. Legacy `_`-substituted dirs are orphaned/quarantined, never
  served across tenants.
- **Tests:** `tests/security/test_memory_namespace_encoding.py` — slash-vs-question,
  disallowed-char variants, case-fold, reserved device names, NFC/NFD same-dir,
  distinct-unicode, traversal contained, absolute-path contained, legacy-ambiguous
  not served; existing `tests/tools/test_memory_namespace.py` updated to the
  encoder.

### Finding 6 — Preserve execution-tree budget root across retries  *(implemented)*
- **Root cause:** the worker opened the execution-tree budget keyed on the
  **grant's** `root_run_id`. A retry re-mints its own grant with a fresh grant-root,
  so it opened a NEW tree and reset the shared ceilings — even though
  `_retry_execution_binding` inherits the parent's binding + limits.
- **Fix:** `worker_admission` anchors `open_tree` to the **effective binding's**
  `root_run_id` (inherited UNCHANGED by every retry/child via `rescope_binding`),
  so the original run and all retries/children open the SAME tree and `open_tree`'s
  no-reseed invariant preserves the original ceilings. The retry endpoint also
  refuses (422 `retry_budget_widened`, before any child/effect) a retry grant that
  widens any budget dimension or extends the deadline WINDOW vs the original grant.
- **Tests:** `tests/youtab_runtime/test_managed_binding_e2e.py` — retry rejects a
  widened `max_total_tokens`/`max_cost_micros`/`max_iterations`/`max_spawn_depth`/
  `max_concurrent_agents`/`max_retries` and an extended deadline window; equal-or-
  tighter retry allowed; existing re-scope test (child `root_run_id ==` parent)
  still green. Worker/budget suites green.

### Finding 9 — Resume event + requeue atomic  *(implemented)*
- **Root cause:** the resume endpoint persisted `run_resume` in one transaction,
  then called `unblock_task` in a SEPARATE transaction and swallowed its failure —
  leaving the run with `run_resume` recorded but still blocked while the API falsely
  returned `status=running`.
- **Fix:** extracted `_unblock_task_locked` (the `unblock_task` body without its own
  `write_txn`; public wrapper unchanged for all external callers). The resume
  endpoint now appends `run_resume` AND requeues in ONE `write_txn`: a requeue
  conflict rolls back the event and returns 409 `resume_requeue_conflict`; a DB
  failure rolls back and returns 503 `resume_unavailable_retryable`; the dispatcher
  is ticked only after commit.
- **Tests:** `tests/youtab_agent_cli/test_unblock_task_atomic.py` (equivalence +
  atomic rollback/commit) and `tests/youtab_runtime/test_managed_interactive_
  lifecycle_e2e.py` (DB-failure → 503, no `run_resume`, still paused; requeue-false
  → 409, no false `running`).

---

## Finding 1 — Bind each execution grant to the requested operation  *(root cause confirmed; requires a grant-contract decision)*

**Root cause (confirmed).** Managed ingress binds tenant/user/workspace
(`managed_execution._check_binding`) and the single-use nonce prevents re-using the
SAME grant twice, but no signed field ties a grant to a SPECIFIC run/operation:
the envelope's `task_id`/`root_run_id`/`agent_id`/`engine_id` are Simorgh-side
identifiers, and there is no `operation` field. The run's identity (`rid`) is
generated by the runtime at create, independent of the grant. Control endpoints
(cancel/pause/resume/answer/approve/retry) admit the grant and check ownership,
but do not compare any grant field to the target run.

**Why this cannot be fixed fail-closed runtime-only.** The current contract uses
INDEPENDENT per-operation grants: the create grant and each control grant carry
different `task_id`/`root_run_id` values (confirmed by the request/grant harnesses,
which mint a fresh random `task_id`/`root_run_id` per operation), and
`objective` is a description, not the verbatim task text. Enforcing
`grant.task_id == run's create-grant task_id` (or `objective == task`) fail-closed
would reject every legitimate create and control call and break the continuous
E2E. Making the runtime enforce it therefore requires the **grant minter (AI OS
`engine_delegate`, a separate repo) to first bind the operation into the signed
envelope** — a coordinated cross-repo contract change, not a runtime-only gap.

**Precise decision required (one of):**
1. **Bind the target into the signed grant** — add `operation` (create/cancel/…)
   and the target `run_id` (or a stable per-run Simorgh `task_id`/`root_run_id`) to
   `BrainCommandEnvelopeV2`, made MANDATORY in managed mode. Then the runtime
   enforces `envelope.operation == this endpoint` and `envelope.run_id == URL
   run_id` fail-closed (create binds `objective == task`).
2. **Gateway echoes the signed operation fields into the request body** so the
   runtime canonically compares them to the grant.

**Ready runtime enforcement (to enable once #1 or #2 is chosen):** persist the
create grant's `{task_id, root_run_id, agent_id, engine_id, objective}` on the run;
add `assert_grant_bound_to_request(envelope, operation, target)` called by each
managed endpoint after the target is known and before the mutation, rejecting
missing/ambiguous/mismatched fields (403 `grant_operation_mismatch`); this yields
the cross-operation, cross-task, objective-drift, agent-drift and engine-drift
guarantees the finding asks for. It is intentionally NOT enabled here because it
would fail-close every current managed flow until the minter sends the bound
fields.

---

## Adversarial / fault-injection test matrix (added or extended)

| Finding | File | Key adversarial cases |
|---|---|---|
| 2 | `tests/agent/test_managed_budget_gate.py` | zero-budget blocks known-paid; zero-budget allows unpriced/local; positive-budget unpriced fails closed; exhausted blocks; overflow saturates |
| 3 | `tests/agent/test_managed_budget_gate.py` | DB-lock / I/O on debit-tokens, pre-iteration, debit-cost → durable `tree_budget_error` (no swallow) |
| 4 | `tests/youtab_runtime/test_capability_binding.py` | schema drift, missing live hash, dynamic-inclusion requires live hash / rejects null; handler-swap residual (`xfail`) |
| 5 | `tests/youtab_agent_cli/test_capability_manifest.py`, `tests/youtab_runtime/test_managed_execution_subprocess.py` | missing/empty/malformed/mismatch/dynamic-tamper hash rejected; worker refuses no-manifest managed run |
| 6 | `tests/youtab_runtime/test_managed_binding_e2e.py` | retry widening each budget dim / deadline window → 422; equal/tighter allowed |
| 7 | `tests/security/test_memory_namespace_encoding.py` | slash/question, case-fold, NFC/NFD, reserved device, traversal, absolute-path, legacy-ambiguous cross-tenant |
| 8 | `tests/youtab_runtime/test_usage_pricing_local_zero.py` | uppercase/mixed-case loopback local; userinfo/public/IPv6-tunnel/malformed/non-http still rejected |
| 9 | `tests/youtab_agent_cli/test_unblock_task_atomic.py`, `tests/youtab_runtime/test_managed_interactive_lifecycle_e2e.py` | atomic rollback on requeue failure; 503/409 never false `running` |

## Verification (venv `.venv-qual`, Windows, no network to any provider)

| Command | Result |
|---|---|
| `pytest tests/agent/test_managed_budget_gate.py` | **27 passed** |
| `pytest tests/youtab_runtime/test_capability_binding.py` (+ manifest/gate2/policy/managed_execution/invocation) | **70 passed, 1 xfailed** |
| `pytest tests/youtab_agent_cli/test_capability_manifest.py + test_managed_execution_subprocess.py` | **31 passed** |
| `pytest tests/tools/test_memory_namespace.py + tests/security/test_memory_namespace_encoding.py + test_usage_pricing_local_zero.py` | **passed** (F7/F8) |
| `pytest tests/youtab_agent_cli/test_unblock_task_atomic.py + test_run_control + test_managed_pause_resume + interactive lifecycle e2e` | **passed** |
| `pytest tests/youtab_runtime/test_managed_execution_subprocess.py + test_execution_tree_budget.py + test_retry_execution_binding.py` | **44 passed** |
| `pytest tests/youtab_runtime/test_managed_continuous_e2e.py` (real loop, chat `-q` and `-Q`) | **2 passed** |
| `pytest tests/youtab_runtime/test_managed_cli_e2e.py + test_managed_admission_ordering.py` | **35 passed** |
| `ruff check` (all 21 changed files) | **All checks passed** |

_Full `tests/youtab_runtime`, `token-auth-security`, deterministic-benchmark, and secret/dependency gate results are recorded in the final report._

## Changed files

**Production (11):** `agent/usage_pricing.py` (F8), `tools/memory_tool.py` (F7),
`youtab_runtime/policy.py` (F4), `youtab_runtime/admission.py` (F4),
`agent/tool_executor.py` (F4), `youtab_agent_cli/capability_manifest.py` (F5),
`youtab_agent_cli/worker_admission.py` (F5, F6),
`youtab_agent_cli/kanban_db.py` (F9),
`youtab_agent_cli/web_routers/runtime.py` (F6, F9),
`agent/managed_budget_gate.py` (F2, F3), `agent/conversation_loop.py` (F2, F3).

**Tests (10):** `tests/agent/test_managed_budget_gate.py`,
`tests/tools/test_memory_namespace.py`,
`tests/security/test_memory_namespace_encoding.py` (new),
`tests/youtab_runtime/test_capability_binding.py`,
`tests/youtab_agent_cli/test_capability_manifest.py`,
`tests/youtab_runtime/test_gate2_managed_completion.py`,
`tests/youtab_runtime/test_managed_execution_subprocess.py`,
`tests/youtab_agent_cli/test_unblock_task_atomic.py` (new),
`tests/youtab_runtime/test_managed_interactive_lifecycle_e2e.py`,
`tests/youtab_runtime/test_managed_binding_e2e.py`.

**Evidence:** this document.

## Remaining operational gates
- Finding 1 needs the Owner's grant-contract decision (above) + the AI OS
  `engine_delegate` change before the runtime binding can be enabled fail-closed.
- Finding 4 handler-identity residual (documented `xfail`).
- Full CI (CodeQL ×2, benchmark-deterministic, javascript, python-security,
  token-auth-security, wake-word-backends ×3, windows-runtime-cli, windows-tools)
  runs on push after Owner exact-SHA authorization.
- No Cloud Benchmark authorization requested; the benchmark was not executed.
