# PR #44 — WAVE-30H Batch5 remediation (Codex review 5187358362)

Authoritative evidence for the bounded remediation of the four confirmed blockers in
Codex exact-SHA review
[5187358362](https://github.com/eimanghazaei/Youtab-Agent-Runtime/pull/44#pullrequestreview-5187358362).
Markdown is the authoritative evidence (no PDF).

- **Candidate SHA (Batch5 blockers):** commit `ac3fce0ec84cb11e5a33e88a63b1b92b529360fe` (parent `812750e388496c9fbf17b9c1fc0cd9d1c57c5801`) — pushed and CI-green on PR #44.
- **Follow-up (continuous E2E proof):** a subsequent test-and-evidence-only commit (parent `ac3fce0ec…`) adds `tests/youtab_runtime/test_managed_continuous_e2e.py` and the "Continuous managed-runtime E2E" section below. No production code changed; reported to the Owner for exact-SHA push authorization.
- **Branch:** `feat/wave30h-codex-5186275558-remediation` (PR #44)
- **Base:** `fix/wave12-authz-gate-token-provider-binding`
- **PR #41:** untouched, frozen at `3ec256787`.

## Root-cause closure matrix

| # | Blocker | Root cause | Fix | Proof |
|---|---|---|---|---|
| **1** | Reused pre-admission snapshot can expire before final admission | `load_verified_preadmission()` verified time only at grant re-admission; `establish_managed_admission()` reused the admitted object; `verify_proof()` checks only the HMAC | `establish_managed_admission()` re-validates `envelope.expires_at` / `reasoning.deadline_at` vs now **before** attaching `agent._admitted_command`; no reload, no nonce re-burn | `test_establish_refuses_grant_expired_during_startup` (past-expiry snapshot ⇒ refused, authority not attached) |
| **2** | `auto` accepted as a concrete pre-credential route | binding + route could both be literal `auto`; strict gate passed; resolution then picked/minted a concrete provider | `_enforce_effective_binding` rejects `provider == "auto"`; new pre-credential `assert_bound_provider_admissible()` refuses a non-concrete bound provider, an `auto`/missing requested provider, or a requested≠bound provider — **before** credential resolution | `test_bound_provider_admissible_*` units; `test_cli_unpinned_auto_refused_before_credential_resolution` (E2E: `credresolve_count == 0`) |
| **3** | Route checked ≠ route used; duplicate credential resolution | CLI checked a hand-built plan, then `_ensure_runtime_credentials()` ran, then `_init_agent()` resolved **again**; no comparison of the actual resolved identity; local aliases planned as `ollama` but resolved as `custom` | **Deleted the duplicate `plan_runtime_route`.** Resolve once; `_init_agent(credentials_already_resolved=True)` does not re-resolve. Compare the **actual resolved** identity (`authoritative_provider()` canonicalizes the `custom` transport back to the bound alias) against the binding **before** client construction. `_enforce_effective_binding` also canonicalizes | `test_authoritative_provider_*`; `test_cli_canonical_ollama_engine_bound_run_proceeds_through_managed_path` (`credresolve_count == 1`, alias `custom`→`ollama` matches bound); vertex/youtab drift E2E (zero mint/refresh) |
| **4** | Pure planner rejected valid built-in endpoint overrides | the deleted planner's config-base-url branch accepted only `auto`/custom aliases, substituting the registry default for e.g. Anthropic-on-Azure | Planner removed; the actual resolver's output is compared, so a supported built-in override resolves to the configured URL and matches its binding | `test_builtin_anthropic_azure_override_resolves_and_matches_binding` |

## Files changed

| File | Change |
|---|---|
| `youtab_agent_cli/runtime_provider.py` | **Deleted** `plan_runtime_route` (the duplicate resolver) |
| `youtab_agent_cli/worker_admission.py` | `authoritative_provider()`; `assert_bound_provider_admissible()` (pre-credential); strict `assert_route_matches_binding` fed the actual resolved route; reject `auto` bound provider; canonicalize provider in `_enforce_effective_binding`; expiry recheck in `establish_managed_admission` |
| `youtab_agent_cli/cli_agent_setup_mixin.py` | `_init_agent(credentials_already_resolved=…)` — resolve exactly once |
| `cli.py` | `chat -q` and `-Q`: single snapshot load → pre-credential provider gate → resolve once → compare actual resolved identity before construction → reuse snapshot at final admission |
| `tests/youtab_runtime/test_managed_admission_ordering.py` | Removed planner tests; added authoritative/admissible/built-in-override/expiry units |
| `tests/youtab_runtime/test_managed_cli_e2e.py` | Canonical ollama positive E2E; resolver call **counter**; auto-refusal; pre-credential provider-gate sentinel; kept all prior adversarial cases |

## Exact commands & results (venv `.venv-qual`, Windows)

| Command | Result |
|---|---|
| `pytest tests/youtab_runtime` | **1299 passed** |
| `pytest tests/benchmark -m benchmark` | **159 passed** |
| `pytest tests/cli` | **735 passed, 6 skipped** |
| `pytest tests/youtab_runtime/test_managed_admission_ordering.py` | **23 passed** |
| `pytest tests/youtab_runtime/test_managed_cli_e2e.py` | **12 passed** |
| `pytest tests/youtab_runtime/test_managed_continuous_e2e.py` (added — the ONE continuous E2E) | **2 passed** |
| focused managed suites together (cli_e2e + binding_e2e + admission_ordering) | **45 passed** |
| token-auth-security (5 files) | **95 passed** |
| provider-resolution (2 files) | **64 passed** |
| `ruff check` (6 changed files) | clean |
| secret gate on `git archive HEAD` | `passed: true`, `policy_errors: []` |

## Focused CLI scenario matrix — `tests/youtab_runtime/test_managed_cli_e2e.py`

These are the **focused, single-transition** shipped-CLI checks (both `chat -q` and
`-Q`). Each runs the real worker CLI subprocess and proves one admission/ordering
property in isolation. The positive row proves the ordered valid path *up to and
including* one loopback inference dial (single verified pre-admission load →
pre-credential provider gate → exactly-once credential resolution → actual-resolved-
identity compare → real client construction → final admission → loopback dial); it
does **not** by itself drive a kanban tool call or assert terminal task completion.
Terminal completion of the whole lifecycle is proven separately by the continuous
E2E below — this matrix does not claim it.

| Scenario | preadmit | provider-gate | credresolve count | client | admit | net | rc |
|---|---|---|---|---|---|---|---|
| canonical ollama, matching route (valid) | ✓ | ✓ | **1** | ✓ | ✓ | loopback only | 0 |
| unpinned `auto` | ✓ | ✓ | **0** | ✗ | ✗ | ✗ | 3 |
| Vertex drift | ✓ | ✓ | **0** (no mint) | ✗ | ✗ | ✗ | 3 |
| Youtab drift | ✓ | ✓ | **0** (no refresh) | ✗ | ✗ | ✗ | 3 |
| missing / tampered / forged / expired grant, cross-run / -ws / -tenant | ✓ | — | **0** | ✗ | ✗ | ✗ | 3 |
| grant expired during startup (unit) | n/a | n/a | n/a | n/a | refused before authority attached | — | — |

## Continuous managed-runtime E2E — `tests/youtab_runtime/test_managed_continuous_e2e.py`

The single, uninterrupted end-to-end proof (added in the PR #44 final batch). Unlike
the focused checks and unlike the binding-E2E happy path (whose stub worker calls
`kb.complete_task` directly, bypassing the tool-authority gate), this drives the
**whole lifecycle in one flow** with the Simorgh trust mode set to `managed` (the
admission + tool-authority gates ACTIVE, never inert):

    runtime preflight (real ingress)
      → create_run persistence (real Ed25519 grant + HMAC + create_task_ex atomic
        binding/grant/manifest/mode + row-pinned provider_override/model_override)
        → PRODUCTION worker-invocation construction via the REAL
          `youtab_agent_cli.kanban_db.build_worker_invocation`, executed UNCHANGED
          → the shipped worker CLI (`chat -q "work kanban task <id>"`, and the `-Q`
            goal-mode variant build_worker_invocation appends)
            → single verified pre-admission load → pre-credential provider gate →
              credential resolution EXACTLY ONCE → actual-resolved-identity compare →
              real shipped provider client construction → FINAL managed admission
              → loopback inference → a REAL `kanban_complete` tool call admitted by
                the tool_executor authority gate (effect-class `none`, in the frozen
                manifest) → the card reaches a TERMINAL completed state, observed via
                the real events API.

Two tests (both **passed**): `test_continuous_managed_e2e_chat_q` (non-goal) and
`test_continuous_managed_e2e_chat_Q` (goal-mode). Each asserts, on the SAME identity
produced by the real preflight/create (nothing manually seeded on the positive path):

| Assertion | Result |
|---|---|
| single verified pre-admission load ran | ✓ |
| pre-credential provider gate ran | ✓ |
| credential resolution occurred **exactly once** (`credresolve_count == 1`) | ✓ |
| a real shipped provider client was constructed | ✓ |
| **final managed admission ran and succeeded** | ✓ |
| no Vertex mint / Youtab refresh (no cloud drift) | ✓ |
| **every socket destination dialed was loopback** (`127.0.0.1`) | ✓ |
| run and task reached the **terminal `completed`** state | ✓ |

Honest boundary — REAL vs substituted:
- **REAL:** the FastAPI ingress + token_auth + RuntimeServiceProvider; the create/
  preflight endpoints and `create_task_ex` atomic persistence; the dispatcher and its
  `build_worker_invocation`-built worker command (executed unchanged); the shipped CLI
  worker subprocess; `worker_admission` (single-snapshot pre-admission, pre-credential
  provider gate, resolve-once, route-vs-binding compare, final admission); the frozen
  capability manifest and the `tool_executor` authority gate; and the real
  `kanban_complete` tool → terminal `complete_task` transition.
- **SUBSTITUTED (and why):** only the model "brain" — a hermetic loopback
  OpenAI/Ollama-compatible responder on `127.0.0.1` returns a scripted `kanban_complete`
  tool call (and a `"done"` verdict for the goal-mode judge). No paid provider, no cloud,
  no live model. The (provider, model, endpoint) substrate is one consistent local
  `ollama`/`qwen:test`/loopback identity so preflight and create bind the SAME digest
  offline and the worker's ACTUAL resolved route matches the persisted binding exactly.
- **Test precondition (not a production behaviour change):** the test calls
  `tools.registry.discover_builtin_tools()` in the ingress process before create — the
  same discovery the production runtime/gateway process performs at import
  (`model_tools.py:197`). It populates the tool registry so the ingress freezes a
  non-empty capability manifest that authorizes `kanban_complete`, exactly as a real
  runtime process does. No production code was modified for this batch.

## No paid/cloud benchmark

No paid provider, metered model, or cloud benchmark was executed. The positive E2E
uses an in-process **loopback** OpenAI/Ollama-compatible responder on `127.0.0.1`; the
test asserts every socket destination dialed was loopback.

## Remaining operational steps before benchmark

1. Owner exact-SHA push authorization for the candidate SHA.
2. Independent Codex exact-SHA review of the pushed SHA.
3. Owner-gated benchmark preflight/create against a reachable engine (not part of this change).
