"""Deterministic enterprise reference providers with exact operation IDs.

Review v1.0 item 6/7/9. These Owner-authorized **reference** providers stand in
for unavailable external vendors so the governed spine can be proven end-to-end
without a paid vendor or any network. They are labelled ``REFERENCE`` everywhere,
never minted as ``LIVE``, and use the same worker transport a vendor executor
would. Each output is a pure deterministic function of the normalized request.

Operation semantics:
  * ``*.read`` / ``cad.geometry.inspect`` / ``cad.dfm.check`` / ``cad.fea.run``
    — non-mutating computation (zero business effect);
  * ``*.preview`` — a projection that computes what *would* change and creates
    **zero** business effect;
  * ``*.commit`` — produces the single deterministic result whose digest the
    connector binds to exactly one committed effect;
  * ``*.reconcile`` — recomputes the expected committed-outcome digest for a
    prior effect so the connector can **query/verify** it; it performs no create
    or update.

The provider layer is pure and stateless — it never touches the effect ledger,
never mints approvals, and runs inside the worker subprocess. Comparison,
persistence and receipt-minting are the connector's job.
"""

from __future__ import annotations

from typing import Any, Mapping

from youtab_runtime.enterprise import cad
from youtab_runtime.enterprise.manifest import canonical_json, digest_of

__all__ = [
    "ProviderError",
    "OPERATIONS",
    "operation_class",
    "request_schema_for",
    "response_schema_for",
    "execute",
    "PROVENANCE_REFERENCE",
]

PROVENANCE_REFERENCE = "REFERENCE"


class ProviderError(ValueError):
    """A fail-closed reference-provider error (unknown op / bad input)."""


def _digest(*parts: Any) -> str:
    return digest_of(list(parts))


# operation_id -> (op_class, request_schema, response_schema)
def _std(domain: str) -> dict[str, tuple[str, dict, dict]]:
    read_op = f"{domain}." + {
        "crm": "contact.read",
        "erp": "inventory.read",
        "sap": "business_object.read",
    }[domain]
    prev_op = f"{domain}." + {
        "crm": "contact.update.preview",
        "erp": "order.preview",
        "sap": "business_object.update.preview",
    }[domain]
    commit_op = f"{domain}." + {
        "crm": "contact.update.commit",
        "erp": "order.commit",
        "sap": "business_object.update.commit",
    }[domain]
    recon_op = f"{domain}.effect.reconcile"
    return {
        read_op: (
            "read",
            {"business_key": "str"},
            {"record_id": "str", "found": "bool", "provenance": "str",
             "result_digest": "str"},
        ),
        prev_op: (
            "preview",
            {"business_key": "str", "field": "str", "value": "str"},
            {"record_id": "str", "preview_digest": "str", "would_change": "bool",
             "provenance": "str"},
        ),
        commit_op: (
            "commit",
            {"business_key": "str", "field": "str", "value": "str"},
            {"record_id": "str", "revision": "str", "provenance": "str",
             "result_digest": "str"},
        ),
        recon_op: (
            "reconcile",
            {"checked_operation_id": "str", "business_key": "str",
             "original_request_digest": "str"},
            {"expected_digest": "str", "provenance": "str"},
        ),
    }


OPERATIONS: dict[str, tuple[str, dict, dict]] = {}
for _d in ("crm", "erp", "sap"):
    OPERATIONS.update(_std(_d))
OPERATIONS.update(
    {
        "cad.geometry.inspect": (
            "read",
            {"geometry": "dict"},
            {"area": "float", "perimeter": "float", "volume": "float",
             "surface_area": "float", "min_feature": "float", "kind": "str",
             "solver": "str", "solver_version": "str", "provenance": "str"},
        ),
        "cad.dfm.check": (
            "read",
            {"geometry": "dict", "min_feature_size": "float"},
            {"rule": "str", "threshold": "float", "measured_min_feature": "float",
             "passed": "bool", "solver": "str", "solver_version": "str",
             "provenance": "str"},
        ),
        "cad.fea.run": (
            "read",
            {"model": "dict"},
            {"tip_displacement": "float", "analytical_tip_displacement": "float",
             "relative_error": "float", "max_axial_stress": "float",
             "elements": "int", "converged": "bool", "solver": "str",
             "solver_version": "str", "provenance": "str"},
        ),
        "cad.effect.reconcile": (
            "reconcile",
            {"checked_operation_id": "str", "business_key": "str",
             "original_request_digest": "str"},
            {"expected_digest": "str", "provenance": "str"},
        ),
    }
)


def operation_class(operation_id: str) -> str:
    try:
        return OPERATIONS[operation_id][0]
    except KeyError:
        raise ProviderError(f"unknown operation {operation_id!r}") from None


def request_schema_for(operation_id: str) -> dict:
    try:
        return dict(OPERATIONS[operation_id][1])
    except KeyError:
        raise ProviderError(f"unknown operation {operation_id!r}") from None


def response_schema_for(operation_id: str) -> dict:
    try:
        return dict(OPERATIONS[operation_id][2])
    except KeyError:
        raise ProviderError(f"unknown operation {operation_id!r}") from None


def _cad_defaults(base: dict) -> dict:
    """CAD outputs vary by op; fill the union response schema with 0.0 defaults."""
    filled = {
        "area": 0.0, "perimeter": 0.0, "volume": 0.0, "surface_area": 0.0,
        "min_feature": 0.0, "kind": "", "solver": "", "solver_version": "",
    }
    filled.update(base)
    return filled


def execute(
    operation_id: str,
    *,
    workspace: str,
    business_key: str,
    payload: Mapping[str, Any],
    request_digest: str,
    provenance: str = PROVENANCE_REFERENCE,
) -> dict:
    """Run a reference operation purely and deterministically (no I/O)."""
    if operation_id not in OPERATIONS:
        raise ProviderError(f"unknown operation {operation_id!r}")
    op_class = OPERATIONS[operation_id][0]

    if operation_id == "cad.geometry.inspect":
        out = _cad_defaults(cad.inspect_geometry(payload["geometry"]))
        out["provenance"] = provenance
        return out
    if operation_id == "cad.dfm.check":
        out = cad.dfm_check(
            payload["geometry"], min_feature_size=payload["min_feature_size"]
        )
        out["provenance"] = provenance
        return out
    if operation_id == "cad.fea.run":
        out = dict(cad.fea_run(payload["model"]))
        out["provenance"] = provenance
        return out

    record_id = _digest("record", operation_id.rsplit(".", 1)[0], workspace, business_key)
    if op_class == "read":
        return {
            "record_id": record_id,
            "found": True,
            "provenance": provenance,
            "result_digest": _digest("read", operation_id, workspace, business_key, request_digest),
        }
    if op_class == "preview":
        # Zero business effect: only a projection of what would change.
        return {
            "record_id": record_id,
            "preview_digest": _digest("preview", operation_id, workspace, business_key, request_digest),
            "would_change": True,
            "provenance": provenance,
        }
    if op_class == "commit":
        base = operation_id  # commit op id
        result_digest = _digest("commit", base, workspace, business_key, request_digest)
        return {
            "record_id": record_id,
            "revision": _digest("rev", result_digest)[:12],
            "provenance": provenance,
            "result_digest": result_digest,
        }
    if op_class == "reconcile":
        checked = payload["checked_operation_id"]
        expected = _digest(
            "commit", checked, workspace, payload["business_key"],
            payload["original_request_digest"],
        )
        return {"expected_digest": expected, "provenance": provenance}
    raise ProviderError(f"unhandled op class {op_class!r}")  # pragma: no cover


def request_digest_of(operation_id: str, workspace: str, business_key: str,
                      payload: Mapping[str, Any]) -> str:
    """The canonical request digest bound into approvals, effects and receipts."""
    return digest_of(
        {
            "operation_id": operation_id,
            "workspace": workspace,
            "business_key": business_key,
            "payload": canonical_json(dict(payload)),
        }
    )
