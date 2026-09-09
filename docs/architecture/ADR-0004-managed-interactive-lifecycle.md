# ADR-0004 — Managed interactive-lifecycle contract (clarification, approval-continuation, pause/resume)

**Status:** Implemented — control plane (question/answer, approval/decision, pause/resume endpoints + status projection + redaction) and the production-worker pause/checkpoint/resume consumption are both wired (2026-09-09c, fail-closed correction). The production `youtab chat` worker (`agent.conversation_loop`) cooperatively observes a `run_pause` at each TURN BOUNDARY and, once the pause is ACCEPTED, **STOPS fail-closed no matter what** — it never continues model/tool/side-effect execution after an accepted pause:
- valid checkpoint → persist `run_checkpoint` (the conversation already contains every completed tool call + result), block the task, stop → status `paused`, resumable; a `/resume`-dispatched fresh worker restores that exact conversation and continues, **no tool re-executed**.
- checkpoint cannot be built / is oversized / fails integrity / cannot be written → persist a durable `run_pause_failed`, block, stop → status `pause_failed`: **non-completed, non-cancelled, NOT resumable** until an explicit authorized recovery action. `/resume` refuses it (`pause_failed_requires_recovery`).

Status projection distinguishes: `pause_requested` (recorded, worker not yet acknowledged), `paused` (valid checkpoint), `pause_failed` (no restorable checkpoint), plus resumed / cancelled / terminal. Cancel always wins over any pause state. A non-managed run never touches this path. **Validation status:** hermetic unit + contract + failure-mode tests green (`tests/youtab_runtime/test_managed_pause_resume.py`, `test_run_control.py`); live-model and Linux-CI validation are PENDING (separate gates). No merge/deploy/canary authorized.
**Scope:** Youtab-Agent-Runtime managed `create_run` plane + AI OS connector + worker agent loop
**Related:** `ADR-0002-simorgh-admission-execution-contract.md` (esp. DP4 Effect Gate = proposal-as-continuation over the event cursor), `ADR-0003-extensible-tool-registry.md`, `AGENT_RUNTIME_CONNECTOR_CONTRACT.md` (DRAFT / Not Canon)

> This ADR records a real gap surfaced by the WAVE-30H Phase-A managed-lifecycle proof and proposes an additive, non-breaking contract to close it. It wires nothing. No merge, deploy, canary, or benchmark is authorized by this document.

---

## Context — what Phase A proved, and the gap

The WAVE-30H Phase-A work proved, on **real integration evidence** (non-mock: a real FastAPI TestClient over the actual runtime router + a real dispatched worker subprocess, and a separate real cross-process subprocess re-admission test), the managed lifecycle for the scopes the plane already supports:

| scope | proven | evidence |
|---|---|---|
| A1 ingress identity/tenant/session binding | ✅ | `test_managed_lifecycle_http_e2e` (task tenant/user), `_load_owned_task` |
| A2 Simorgh grant mint → admit → persist, no drift | ✅ | admit+persist asserted; forged/expired/replayed/cross-tenant/missing rejected |
| A3 progress/result/error event delivery | ✅ | ordered events over the `/events` cursor |
| A6 **cancel** | ✅ | grant-gated cancel → terminal |
| A7 reconnect + event replay | ✅ | resumable `after` cursor, ordered, idempotent reads |
| A8 exactly-once, ordering, idempotency | ✅ | monotonic ids; single worker run; replayed grant rejected |
| A9 secret redaction of user-visible output | ✅ | shared `redaction` policy, `run_observer` wiring, E2E no-leak |
| A10 cross-tenant isolation | ✅ | grant identity mismatch + read isolation 404 + memory cross-tenant suite |
| A11 failure recovery, no duplicate execution | ✅ (partial) | exactly-once worker run; bounded worker attempts (`test_runtime_api` canary) |
| A12 full real E2E | ✅ | headers → grant → admit → persist → dispatched worker re-admits → events → complete |

**The gap.** Three requested scopes are **not** first-class operations on the managed `create_run` plane today:

- **A4 — clarification:** the agent cannot ask the *user* a question mid-run and resume the **same run** on the answer. There is no question event kind flowing agent→user, no answer endpoint, and no worker-side resume-on-answer.
- **A5 — approval-continuation:** interactive approve/deny **over the run plane** does not exist. Approval exists only at the **tool layer** — effectful tools are blocked pending the Brain Effect Gate (proven: the subprocess test shows a `write` tool returns a block requiring the gate) — but there is no create_run-plane request→approve/deny→controlled-continuation handshake.
- **pause/resume (part of A6):** only `cancel` exists; there is no pause signal the worker honours, nor a resume.

These require a **new public contract** (new event kinds + new endpoints + a worker resume state machine + a connector change). Per governance that is an Owner architecture decision, not a unilateral implementation — hence this ADR.

## Decision required (Owner)

1. **DP-A — Build the interactive-lifecycle contract now, or defer?** If build: stage 1 = clarification + approval-continuation (they share one mechanism); stage 2 = pause/resume.
2. **DP-B — Transport.** Reuse ADR-0002 **DP4 (proposal-as-continuation over the existing event cursor)** for both clarification and approval? (Recommended — it inherits ordering, resumability, redaction and tenant isolation already proven.)
3. **DP-C — Answer/decision authority.** Must a user answer / approval decision itself be **Simorgh-grant-bound** (same fail-closed identity/tenant/replay controls as `create_run`/`cancel`), or is transport HMAC + ownership sufficient? (Recommended: grant-bound, to keep one authority model.)

## Proposed design (additive, non-breaking — only if DP-A = build)

**Event kinds (agent → user, over the existing cursor):**
- `run_question` `{question_id, prompt, schema?}` — agent needs user input; run enters `awaiting_input` (a **non-terminal** status; the cursor already carries `status`).
- `run_approval_request` `{approval_id, action, effect_digest}` — agent needs approval for an effect (ties to the ADR-0002 Effect Gate).

**Endpoints (user → run; grant-gated exactly like `cancel`, see DP-C):**
- `POST /runs/{id}/answer` `{question_id, answer}` → appends `run_answer`, clears `awaiting_input`, worker resumes the **same** run.
- `POST /runs/{id}/approve` / `/decision` `{approval_id, decision: approve|deny}` → appends `run_approval_decision`; approve continues, deny fails the effect closed (never auto-approves).
- `POST /runs/{id}/pause` and `/resume` (stage 2) → append control signals the worker honours at its loop boundary (same shape as the existing cancel signal).

**Worker agent loop:** at a question/approval point the worker persists the request event and **blocks** (bounded by the grant deadline/budget), polling for the matching decision/answer event; on arrival it consumes it **exactly once** (idempotent by `question_id`/`approval_id`) and resumes. No duplicate execution: the answer/decision is a single consumed event, ordered on the same monotonic cursor.

**Invariants to preserve (all already proven for the existing plane):** tenant/user ownership on every new endpoint (404 cross-tenant), monotonic ordering + resumable replay, redaction of all new user-visible payloads, grant-bound authority (DP-C), exactly-once consumption, fail-closed on deny/timeout, and **no capability reduction** (ADR-0003).

**Cross-repo:** the AI OS connector must relay `run_question`/`run_approval_request` to the user and POST the answer/decision back — a coordinated AI OS Draft-PR change, paired with the Runtime branch, as in ADR-0002 §3.

## Options

- **Option 1 — Build stage 1 (clarification + approval) now**, pause/resume in stage 2. Closes A4 + A5 with one mechanism; smallest contract that satisfies the directive. *Recommended if the Owner wants A fully closed.*
- **Option 2 — Defer all three**; accept Phase A as "9/12 proven, 3 ADR-gated" and proceed to Phase C now. *Recommended if latency (Phase C) is the priority and interactive lifecycle can follow.*
- **Option 3 — Build all three now** (stage 1 + pause/resume together). Largest surface; only if pause/resume is needed immediately.

## Consequences

Additive only — no existing managed-run behaviour changes; a run that never asks a question or needs approval is unaffected. New endpoints are grant-gated and fail-closed. The work spans two repos (coordinated Draft PRs) and needs its own tests (real dispatched-worker E2E for answer/approve/deny/timeout/duplicate-decision, cross-tenant, redaction) before it can be called complete — consistent with "not complete on unit tests or mocks."

## Owner decision log

_(empty — awaiting decision on DP-A / DP-B / DP-C)_
