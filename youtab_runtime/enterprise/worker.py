"""Enterprise reference/vendor worker subprocess entrypoint.

Run as ``python -m youtab_runtime.enterprise.worker``. It reads ONE typed JSON
request from stdin, dispatches it to the deterministic reference provider, and
writes ONE typed JSON response to stdout. It is intentionally minimal and
**stateless**: it holds no ledger, mints no receipt and no approval. The Runtime
parent (``worker_boundary`` + ``connector``) owns admission, the effect ledger,
receipt-minting and reconciliation; anything this process prints other than the
typed ``{ok, result}`` envelope is ignored by the parent, so a compromised
worker cannot forge a receipt.

Request  (stdin, one JSON object):
    {"operation_id": str, "workspace": str, "business_key": str,
     "payload": {...}, "request_digest": str, "provenance": "REFERENCE"|"LIVE",
     "deadline_epoch": float}
Response (stdout, one JSON object):
    {"ok": true, "result": {...}}   |   {"ok": false, "error": "..."}
"""

from __future__ import annotations

import json
import sys
import time

from youtab_runtime.enterprise import providers


def _handle(req: dict) -> dict:
    operation_id = req["operation_id"]
    provenance = req.get("provenance", providers.PROVENANCE_REFERENCE)
    # A reference worker only ever produces REFERENCE provenance; it refuses to
    # be asked to emit LIVE output (no reference->vendor substitution here).
    if provenance != providers.PROVENANCE_REFERENCE:
        return {"ok": False, "error": "reference worker refuses non-REFERENCE provenance"}
    deadline = req.get("deadline_epoch")
    if isinstance(deadline, (int, float)) and time.time() > deadline:
        return {"ok": False, "error": "deadline exceeded before execution"}
    result = providers.execute(
        operation_id,
        workspace=req["workspace"],
        business_key=req["business_key"],
        payload=req.get("payload", {}),
        request_digest=req["request_digest"],
        provenance=provenance,
    )
    return {"ok": True, "result": result}


def main() -> int:
    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
        if not isinstance(req, dict):
            raise ValueError("request must be a JSON object")
        resp = _handle(req)
    except Exception as exc:  # noqa: BLE001 - report as typed error, never crash-format
        resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(resp))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
