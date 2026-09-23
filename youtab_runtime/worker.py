"""Offline grant-signature validator for the LOCAL-STANDALONE trust mode.

WAVE-30H R3 — this is deliberately NOT the admission/authority path. It used to
call ``AuthorityBoundary.admit()`` and then dispatch nothing: an "admission
worker" that minted an authority context (and burned the grant nonce) yet
executed no task and conferred no execution authority — a dead-end that
misrepresented where authority lives.

Managed execution authority now flows ONLY through
``youtab_runtime.managed_execution.admit_managed_run`` at the runtime ingress
(``/api/runtime/v1`` create/cancel/retry), which seals an unforgeable
:class:`~youtab_runtime.admission.AdmittedCommand`, and is re-sealed in the
worker via ``re_admit_worker_grant``; tool decisions are gated by
``AuthorityBoundary.decide_tool`` which accepts ONLY that sealed context.

This module is retained solely as an OFFLINE diagnostic for the
``local-standalone`` trust mode: it VALIDATES a grant's schema + Ed25519
signature and reports validity. It does not admit, does not consume the
single-use nonce, does not open a listener, and confers no execution authority.
"""

from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from .contracts import BrainCommandEnvelope, BrainCommandEnvelopeV2
from .security import redact_secrets


def _validate(line: str, public_key: str) -> dict:
    """Validate the grant's schema + signature only. Never admits."""
    payload = json.loads(line)
    schema = payload.get("schema_version") if isinstance(payload, dict) else None
    model = (
        BrainCommandEnvelopeV2
        if schema == "youtab.agent-command.v2"
        else BrainCommandEnvelope
    )
    envelope = model.model_validate(payload)
    # Signature/expiry VALIDATION (offline) — NOT admission. This does not touch
    # the replay store, mint an AdmittedCommand, or authorize any execution.
    envelope.verify(public_key)
    return {
        "accepted": True,
        "schema_version": envelope.schema_version,
        "command_id": envelope.command_id,
        "task_id": envelope.task_id,
        "tenant_id": envelope.tenant_id,
        "trace_id": envelope.trace_id,
        # Explicit: this tool grants no authority and is not the admission path.
        "runtime_authority": False,
        "public_listener": False,
        "admission_path": False,
        "role": "local-standalone-offline-validation",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Youtab LOCAL-STANDALONE offline grant validator. Validates a grant's "
            "schema + Ed25519 signature only; it does NOT admit, dispatch, or "
            "confer execution authority (see managed_execution for the real path)."
        )
    )
    parser.add_argument("--public-key", required=True)
    args = parser.parse_args()
    line = sys.stdin.readline()
    if not line:
        print(json.dumps({"accepted": False, "error": "missing command envelope"}))
        return 2
    try:
        result = _validate(line, args.public_key)
    except (ValidationError, ValueError) as exc:
        print(json.dumps({"accepted": False, "error": redact_secrets(str(exc))}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
