"""WAVE-30H Phase-A — canonical managed interactive-lifecycle control contract.

ADR-0004. This module is the single source of truth for the managed control
plane's event vocabulary and state derivation, shared by the HTTP runtime router
(which appends control events and projects status) and by a worker on the real
managed dispatch path (which honours the signals and checkpoints/resumes).

Control contract (all flow over the existing ordered, resumable event cursor):

    agent → user   : run_question, run_approval_request
    user → run     : run_answer, run_approval_decision, run_pause, run_resume
    worker-persisted: run_checkpoint   (the resumable state for a REAL pause)

Derived non-terminal statuses: ``awaiting_input`` (an unanswered question),
``awaiting_approval`` (an undecided approval), ``paused`` (a pause not yet
resumed). Terminal product statuses (completed/cancelled) always win.

Everything is derived from the persisted, monotonically-ordered event log, so it
is replay-consistent: the same events always yield the same status, and a paused
run resumes from its last checkpoint — never a restart.
"""
from __future__ import annotations

import json
from typing import Any, Iterable, List, Optional

# ── event kinds (canonical) ──────────────────────────────────────────────────
QUESTION = "run_question"
ANSWER = "run_answer"
APPROVAL_REQUEST = "run_approval_request"
APPROVAL_DECISION = "run_approval_decision"
PAUSE = "run_pause"
RESUME = "run_resume"
CHECKPOINT = "run_checkpoint"

CONTROL_EVENT_KINDS = frozenset(
    {QUESTION, ANSWER, APPROVAL_REQUEST, APPROVAL_DECISION, PAUSE, RESUME, CHECKPOINT}
)

# ── derived non-terminal statuses ─────────────────────────────────────────────
AWAITING_INPUT = "awaiting_input"
AWAITING_APPROVAL = "awaiting_approval"
PAUSED = "paused"
INTERACTIVE_STATUSES = frozenset({AWAITING_INPUT, AWAITING_APPROVAL, PAUSED})

# decision values
APPROVE = "approve"
DENY = "deny"


def _payload(e: Any) -> dict:
    p = getattr(e, "payload", None)
    return p if isinstance(p, dict) else {}


def interactive_status(events: Iterable[Any]) -> Optional[str]:
    """Derive the single interactive status from the ordered event log.

    The MOST-RECENT still-open control signal wins (by monotonic event id), so a
    pause taken while awaiting input reports ``paused`` until resumed, then the
    question is again the open signal. Returns ``None`` when nothing is open.
    """
    open_q: dict[str, int] = {}
    answered: set[str] = set()
    open_a: dict[str, int] = {}
    decided: set[str] = set()
    last_pause: Optional[int] = None
    last_resume: Optional[int] = None

    for e in events:
        k = getattr(e, "kind", None)
        p = _payload(e)
        eid = getattr(e, "id", 0)
        if k == QUESTION and p.get("question_id"):
            open_q[p["question_id"]] = eid
        elif k == ANSWER and p.get("question_id"):
            answered.add(p["question_id"])
        elif k == APPROVAL_REQUEST and p.get("approval_id"):
            open_a[p["approval_id"]] = eid
        elif k == APPROVAL_DECISION and p.get("approval_id"):
            decided.add(p["approval_id"])
        elif k == PAUSE:
            last_pause = eid if last_pause is None else max(last_pause, eid)
        elif k == RESUME:
            last_resume = eid if last_resume is None else max(last_resume, eid)

    candidates: list[tuple[int, str]] = []
    for qid, eid in open_q.items():
        if qid not in answered:
            candidates.append((eid, AWAITING_INPUT))
    for aid, eid in open_a.items():
        if aid not in decided:
            candidates.append((eid, AWAITING_APPROVAL))
    if last_pause is not None and (last_resume is None or last_pause > last_resume):
        candidates.append((last_pause, PAUSED))

    if not candidates:
        return None
    return max(candidates, key=lambda c: c[0])[1]


def is_paused(events: Iterable[Any]) -> bool:
    return interactive_status(events) == PAUSED


def answer_for(events: Iterable[Any], question_id: str) -> Optional[Any]:
    """The answer payload for a question id, or None if still unanswered.

    Exactly-once by construction: a worker consumes the FIRST answer for an id;
    later duplicates do not re-trigger work (the id is already answered)."""
    for e in events:
        if getattr(e, "kind", None) == ANSWER:
            p = _payload(e)
            if p.get("question_id") == question_id:
                return p.get("answer")
    return None


def decision_for(events: Iterable[Any], approval_id: str) -> Optional[str]:
    """``approve``/``deny`` for an approval id, or None if undecided."""
    for e in events:
        if getattr(e, "kind", None) == APPROVAL_DECISION:
            p = _payload(e)
            if p.get("approval_id") == approval_id:
                d = str(p.get("decision") or "").strip().lower()
                if d in (APPROVE, DENY):
                    return d
    return None


def pause_requested_after(events: Iterable[Any], since_event_id: int) -> bool:
    """True when a pause was requested that is not yet resumed and is newer than
    ``since_event_id`` — the worker's cue to checkpoint and stop at a boundary."""
    last_pause = None
    last_resume = None
    for e in events:
        k = getattr(e, "kind", None)
        eid = getattr(e, "id", 0)
        if k == PAUSE:
            last_pause = eid if last_pause is None else max(last_pause, eid)
        elif k == RESUME:
            last_resume = eid if last_resume is None else max(last_resume, eid)
    if last_pause is None or last_pause <= since_event_id:
        return False
    return last_resume is None or last_pause > last_resume


def latest_checkpoint(events: Iterable[Any]) -> Optional[dict]:
    """The most recent persisted checkpoint state (ordered last-wins), or None."""
    state = None
    for e in events:
        if getattr(e, "kind", None) == CHECKPOINT:
            p = _payload(e)
            if isinstance(p.get("state"), dict):
                state = p["state"]
    return state


# ── conversation checkpoint (real production worker pause/resume) ─────────────
#
# The managed ``youtab chat`` worker checkpoints its RESUMABLE state — the
# conversation message list — at a turn boundary. The list already contains
# every completed tool call AND its result, so resuming replays those messages
# to the model and it continues from there: no tool is re-invoked and no side
# effect is duplicated (the results are already present). The checkpoint is
# taken BEFORE the next model call, so at most the un-started next turn is lost.

CHECKPOINT_SCHEMA = 1
# A conservative cap: a conversation too large to persist safely is not paused
# (the run simply continues to completion — fail-open, never corrupt/half-pause).
_MAX_CHECKPOINT_BYTES = 4_000_000


def conversation_checkpoint_state(
    messages: Iterable[Any], *, iteration: Optional[int] = None
) -> Optional[dict]:
    """Build the CHECKPOINT ``state`` dict for a conversation, or None if the
    messages cannot be JSON-serialized within the size cap (fail-open: the caller
    must then NOT pause, so the run continues rather than blocking un-resumably)."""
    try:
        blob = json.dumps(list(messages), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    if len(blob.encode("utf-8", "surrogatepass")) > _MAX_CHECKPOINT_BYTES:
        return None
    return {"v": CHECKPOINT_SCHEMA, "messages_json": blob, "iteration": iteration}


def conversation_from_checkpoint(events: Iterable[Any]) -> Optional[List[Any]]:
    """Restore the checkpointed conversation message list from the latest
    checkpoint, or None when there is no (valid) conversation checkpoint."""
    state = latest_checkpoint(events)
    if not isinstance(state, dict):
        return None
    blob = state.get("messages_json")
    if not isinstance(blob, str):
        return None
    try:
        msgs = json.loads(blob)
    except (TypeError, ValueError):
        return None
    return msgs if isinstance(msgs, list) else None
