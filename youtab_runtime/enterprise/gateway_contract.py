"""Loader + fail-closed validator for the machine-readable Gateway contract.

The versioned JSON Schemas under ``docs/lane2/contracts`` are the machine-
readable form of the human contract in ``docs/lane2/GATEWAY_LIVE_CONTRACT.md``.
Runtime contract tests parse these artifacts through this module rather than
re-describing the shapes in code, so the schemas are the single source of truth.

Validation prefers the locked ``jsonschema`` library (draft 2020-12) when it is
importable. Because the standalone/CI qualification interpreter may not have the
locked wheel installed, this module falls back to a strict, self-contained
structural validator (required keys, types, enums, patterns, ``$ref``) covering
exactly the JSON Schema keywords the contract artifacts use. No new dependency is
introduced by either path. Any structural violation raises fail-closed.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, NoReturn

__all__ = [
    "GatewayContractError",
    "CONTRACTS_DIR",
    "SCHEMA_NAMES",
    "USING_JSONSCHEMA",
    "load_schema",
    "validate_against",
]


class GatewayContractError(ValueError):
    """A fail-closed contract-artifact load or validation failure."""


# youtab_runtime/enterprise/gateway_contract.py -> repo root is two parents up
# from the enterprise package directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = _REPO_ROOT / "docs" / "lane2" / "contracts"

#: Logical schema name -> file name under CONTRACTS_DIR.
SCHEMA_NAMES: dict[str, str] = {
    "gateway_manifest": "gateway_manifest.schema.json",
    "capability_discovery": "capability_discovery.schema.json",
    "operation_envelope": "operation_envelope.schema.json",
    "reconciliation_evidence": "reconciliation_evidence.schema.json",
    "health": "health.schema.json",
}

try:  # Prefer the locked library when the interpreter has it installed.
    from jsonschema import Draft202012Validator as _Draft202012Validator
    from referencing import Registry as _Registry, Resource as _Resource

    USING_JSONSCHEMA = True
except Exception:  # noqa: BLE001 - fall back to the strict stdlib validator
    _Draft202012Validator = None  # type: ignore[assignment]
    _Registry = None  # type: ignore[assignment]
    _Resource = None  # type: ignore[assignment]
    USING_JSONSCHEMA = False


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    """Read one contract JSON Schema from disk and return it as a dict."""
    try:
        file_name = SCHEMA_NAMES[name]
    except KeyError:
        raise GatewayContractError(f"unknown contract schema {name!r}") from None
    path = CONTRACTS_DIR / file_name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GatewayContractError(f"cannot read schema {name!r}: {exc}") from None
    try:
        schema = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GatewayContractError(f"schema {name!r} is not valid JSON: {exc}") from None
    if not isinstance(schema, dict):
        raise GatewayContractError(f"schema {name!r} must be a JSON object")
    return schema


@lru_cache(maxsize=None)
def _schema_by_id() -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for name in SCHEMA_NAMES:
        schema = load_schema(name)
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise GatewayContractError(f"schema {name!r} missing a string $id")
        by_id[schema_id] = schema
    return by_id


# --------------------------------------------------------------------------- #
# jsonschema path                                                             #
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=None)
def _registry() -> Any:
    resources = [
        (schema_id, _Resource.from_contents(schema))
        for schema_id, schema in _schema_by_id().items()
    ]
    return _Registry().with_resources(resources)


@lru_cache(maxsize=None)
def _jsonschema_validator(name: str) -> Any:
    schema = load_schema(name)
    _Draft202012Validator.check_schema(schema)
    return _Draft202012Validator(schema, registry=_registry())


def _validate_jsonschema(name: str, instance: Any) -> None:
    validator = _jsonschema_validator(name)
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.path) or "<root>"
        raise GatewayContractError(
            f"{name} contract violation at {location}: {first.message}"
        )


# --------------------------------------------------------------------------- #
# strict stdlib fallback (covers only the keywords the artifacts use)         #
# --------------------------------------------------------------------------- #
_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
}


def _fail(path: str, message: str) -> NoReturn:
    raise GatewayContractError(f"contract violation at {path or '<root>'}: {message}")


def _type_ok(kind: str, value: Any) -> bool:
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    py = _JSON_TYPES.get(kind)
    if py is None:
        _fail("", f"unsupported schema type {kind!r}")
    return isinstance(value, py)


def _resolve(schema: dict) -> dict:
    ref = schema.get("$ref")
    if ref is None:
        return schema
    if ref.startswith("#/$defs/"):
        # Local $defs resolution against the manifest schema (the only schema
        # that uses local $defs).
        root = _schema_by_id()["https://youtab.io/contracts/gateway_manifest.schema.json"]
        target: Any = root
        for part in ref.lstrip("#/").split("/"):
            target = target[part]
        return target
    by_id = _schema_by_id()
    if ref in by_id:
        return by_id[ref]
    _fail("", f"unresolvable $ref {ref!r}")
    return {}  # pragma: no cover


def _validate_node(schema: dict, value: Any, path: str) -> None:
    schema = _resolve(schema)

    kind = schema.get("type")
    if isinstance(kind, str) and not _type_ok(kind, value):
        _fail(path, f"expected type {kind}")

    if "enum" in schema and value not in schema["enum"]:
        _fail(path, f"{value!r} not in enum {schema['enum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            _fail(path, "string shorter than minLength")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            _fail(path, f"does not match pattern {pattern}")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            _fail(path, "array shorter than minItems")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(value):
                _validate_node(item_schema, item, f"{path}/{i}")

    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                _fail(path, f"missing required property {req!r}")
        if "minProperties" in schema and len(value) < schema["minProperties"]:
            _fail(path, "object has fewer than minProperties")
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, sub in value.items():
            if key in props:
                _validate_node(props[key], sub, f"{path}/{key}")
            elif isinstance(additional, dict):
                _validate_node(additional, sub, f"{path}/{key}")
            elif additional is False:
                _fail(path, f"additional property {key!r} not allowed")


def _validate_stdlib(name: str, instance: Any) -> None:
    _validate_node(load_schema(name), instance, "")


def validate_against(name: str, instance: Any) -> None:
    """Validate ``instance`` against schema ``name``; raise fail-closed on any
    violation. Returns ``None`` on success."""
    if name not in SCHEMA_NAMES:
        raise GatewayContractError(f"unknown contract schema {name!r}")
    if USING_JSONSCHEMA:
        _validate_jsonschema(name, instance)
    else:
        _validate_stdlib(name, instance)
