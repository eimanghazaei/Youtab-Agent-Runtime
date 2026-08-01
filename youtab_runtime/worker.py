"""Stdin/stdout admission worker; deliberately exposes no public listener."""

from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from .contracts import BrainCommandEnvelope
from .policy import AuthorityBoundary
from .security import redact_secrets


def main() -> int:
    parser = argparse.ArgumentParser(description="Youtab managed task admission worker")
    parser.add_argument("--public-key", required=True)
    args = parser.parse_args()
    boundary = AuthorityBoundary()
    line = sys.stdin.readline()
    if not line:
        print(json.dumps({"accepted": False, "error": "missing command envelope"}))
        return 2
    try:
        envelope = BrainCommandEnvelope.model_validate_json(line)
        boundary.admit(envelope, args.public_key)
    except (ValidationError, ValueError) as exc:
        print(json.dumps({"accepted": False, "error": redact_secrets(str(exc))}))
        return 2
    print(
        json.dumps(
            {
                "accepted": True,
                "schema_version": envelope.schema_version,
                "command_id": envelope.command_id,
                "task_id": envelope.task_id,
                "tenant_id": envelope.tenant_id,
                "trace_id": envelope.trace_id,
                "runtime_authority": False,
                "public_listener": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
