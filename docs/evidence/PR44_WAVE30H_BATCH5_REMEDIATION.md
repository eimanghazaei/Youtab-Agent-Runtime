# PR #44 — WAVE-30H Batch5 remediation (Codex review 5187358362)

Authoritative evidence for the bounded remediation of the four confirmed blockers in
Codex exact-SHA review
[5187358362](https://github.com/eimanghazaei/Youtab-Agent-Runtime/pull/44#pullrequestreview-5187358362).
Markdown is the authoritative evidence (no PDF).

- **Candidate SHA:** the single commit that adds this file (parent `812750e388496c9fbf17b9c1fc0cd9d1c57c5801`); reported to the Owner for exact-SHA push authorization.
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
| token-auth-security (5 files) | **95 passed** |
| provider-resolution (2 files) | **64 passed** |
| `ruff check` (6 changed files) | clean |
| secret gate on `git archive HEAD` | `passed: true`, `policy_errors: []` |

## CI / E2E scenario matrix (shipped CLI, both `chat -q` and `-Q`)

| Scenario | preadmit | provider-gate | credresolve count | client | admit | net | rc |
|---|---|---|---|---|---|---|---|
| canonical ollama, matching route (valid) | ✓ | ✓ | **1** | ✓ | ✓ | loopback only | 0 |
| unpinned `auto` | ✓ | ✓ | **0** | ✗ | ✗ | ✗ | 3 |
| Vertex drift | ✓ | ✓ | **0** (no mint) | ✗ | ✗ | ✗ | 3 |
| Youtab drift | ✓ | ✓ | **0** (no refresh) | ✗ | ✗ | ✗ | 3 |
| missing / tampered / forged / expired grant, cross-run / -ws / -tenant | ✓ | — | **0** | ✗ | ✗ | ✗ | 3 |
| grant expired during startup (unit) | n/a | n/a | n/a | n/a | refused before authority attached | — | — |

## No paid/cloud benchmark

No paid provider, metered model, or cloud benchmark was executed. The positive E2E
uses an in-process **loopback** OpenAI/Ollama-compatible responder on `127.0.0.1`; the
test asserts every socket destination dialed was loopback.

## Remaining operational steps before benchmark

1. Owner exact-SHA push authorization for the candidate SHA.
2. Independent Codex exact-SHA review of the pushed SHA.
3. Owner-gated benchmark preflight/create against a reachable engine (not part of this change).
