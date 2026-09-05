"""Dual-track benchmark contract (WAVE-30C §2) — Track A vs Track B.

Two comparable execution tracks:

* **Track A** — the local/ECO engine (Ollama-served), zero cloud-API cost.
* **Track B** — one Owner-selected, **non-Anthropic** cloud provider, bounded by
  the €10 campaign budget.

For a comparison to be honest the two tracks must be *legitimately* comparable,
not merely asserted so. This module loads the versioned track configs and the
machine-checkable comparability spec and proves it: both tracks load the
**identical** task bank (same manifest hash + scenario-id set + family set;
sections compared as canonical JSON — order-independent, type-sensitive),
share the same output/taskbank schema versions and the same
technically-meaningful run limits, and differ **only** in an explicit per-track
section (track id, provider, model, credential source, cost model, failover).
Anything else that differs fails the check.

Pure stdlib + the schema hashing helper; importable on any host (no provider, no
network, no editable install needed), so the contract is validated in the
deterministic CI gate without any live call.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .schema import FAMILIES, SCHEMA_VERSION, sha256_norm
from .taskbank import MANIFEST_SCHEMA_VERSION, default_manifest_path, load_scenarios

TRACKS_SCHEMA_VERSION = "benchmark_tracks.v1"
COMPARABILITY_SCHEMA_VERSION = "benchmark_comparability.v1"

#: The run limits that are technically meaningful for BOTH a local and a cloud
#: run and must therefore be pinned identical across the tracks (max_cost_eur is
#: deliberately excluded — it is per-track: €0/not-applicable for local, ≤€10 for
#: cloud).
SHARED_LIMIT_KEYS = (
    "max_iterations",
    "max_requests",
    "max_retries",
    "max_concurrency",
    "max_total_tokens",
    "max_runtime_seconds",
    "failure_threshold",
)

#: Keys a track's ``provenance`` stamp carries into every benchmark record so a
#: result is self-describing about which track/provider/model produced it.
_PROVENANCE_KEYS = (
    "track",
    "execution",
    "mode",
    "provider_name",
    "model_name",
    "model_source",
    "credential_source",
    "cost_model",
)


#: A bare environment-variable placeholder, e.g. ``${YOUTAB_ECO_MODEL}``. A
#: Track A model name MUST match this exactly — no embedded default
#: (``${VAR:-gpt-4o-mini}``), no surrounding text — so a concrete model can never
#: be smuggled in past the Owner-supplied-identifier invariant.
_ENV_PLACEHOLDER = re.compile(r"\$\{[A-Z_][A-Z0-9_]*\}\Z")


def is_bare_env_placeholder(value: str) -> bool:
    """True iff ``value`` is exactly a bare ``${ENV_VAR}`` reference."""
    return bool(_ENV_PLACEHOLDER.fullmatch(value or ""))


def _canonical(obj: Any) -> str:
    """Canonical JSON text (sorted keys) so equality is order-independent AND
    type-sensitive — ``8`` and ``8.0`` (or ``True`` and ``1``) do NOT compare
    equal, closing a silent int/float/bool drift in a shared invariant."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class ComparabilityError(RuntimeError):
    """The two tracks are not legitimately comparable (a shared invariant differs,
    or a track diverges outside the allowed per-track section)."""


def tracks_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "tracks"


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_track(track_id: str) -> Dict[str, Any]:
    """Load a track config ('A' or 'B'). Raises on an unknown id or bad schema."""
    tid = (track_id or "").upper()
    fname = {"A": "track_a_eco.v1.json", "B": "track_b_cloud.v1.json"}.get(tid)
    if not fname:
        raise ComparabilityError(f"unknown track id {track_id!r} (expected 'A' or 'B')")
    data = _load_json(tracks_dir() / fname)
    if data.get("schema_version") != TRACKS_SCHEMA_VERSION:
        raise ComparabilityError(
            f"track {tid}: schema_version {data.get('schema_version')!r} "
            f"!= {TRACKS_SCHEMA_VERSION!r}"
        )
    if data.get("track") != tid:
        raise ComparabilityError(
            f"track file for {tid} declares track {data.get('track')!r}")
    for section in ("shared", "per_track"):
        if not isinstance(data.get(section), dict):
            raise ComparabilityError(f"track {tid}: missing '{section}' object")
    return data


def load_comparability() -> Dict[str, Any]:
    data = _load_json(tracks_dir() / "comparability.v1.json")
    if data.get("schema_version") != COMPARABILITY_SCHEMA_VERSION:
        raise ComparabilityError(
            f"comparability spec schema_version {data.get('schema_version')!r} "
            f"!= {COMPARABILITY_SCHEMA_VERSION!r}"
        )
    return data


def track_provenance(track: Dict[str, Any]) -> Dict[str, Any]:
    """The per-track provenance stamp for a benchmark record (track/provider/model
    identity + provenance). Values come from the track's ``per_track`` section."""
    per = track.get("per_track", {})
    stamp = {"track": track.get("track")}
    for key in _PROVENANCE_KEYS:
        if key in per:
            stamp[key] = per[key]
    return stamp


def _manifest_identity(manifest_path: Optional[Path] = None) -> Dict[str, Any]:
    p = manifest_path or default_manifest_path()
    raw = p.read_bytes()
    scenarios = load_scenarios(p)
    ids = sorted(s.id for s in scenarios)
    fams = sorted({s.family for s in scenarios})
    return {
        "manifest_sha256": sha256_norm(raw),
        "scenario_count": len(scenarios),
        "scenario_id_set_sha256": hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest(),
        "family_count": len(fams),
        "family_set_sha256": hashlib.sha256("\n".join(fams).encode("utf-8")).hexdigest(),
    }


def validate_comparability(*, manifest_path: Optional[Path] = None) -> Dict[str, Any]:
    """Prove Tracks A and B are legitimately comparable. Raises
    :class:`ComparabilityError` on any violation; returns a summary on success.

    Checks:
      1. both track configs load with the right schema;
      2. their ``shared`` sections are identical (canonical JSON) to each other and to the
         comparability spec's ``shared`` block;
      3. the pinned manifest hash + scenario-id-set + family-set match the ACTUAL
         task bank on disk (both tracks load the identical bank);
      4. the shared run limits contain exactly ``SHARED_LIMIT_KEYS`` and are equal
         across the tracks;
      5. Track A is local/zero-cost with an Owner-supplied model identifier (never
         a fabricated canonical model), excluded from the €10 campaign budget;
      6. Track B is a **non-Anthropic**, Owner-selected cloud provider using a
         file-based credential and bounded by the €10 cumulative ceiling.
    """
    spec = load_comparability()
    a = load_track("A")
    b = load_track("B")

    # (2) shared sections identical across tracks and equal to the spec. Compared
    # as canonical JSON so a type drift (int vs float, bool vs int) in a shared
    # invariant cannot slip through Python's == coercion.
    if _canonical(a["shared"]) != _canonical(b["shared"]):
        raise ComparabilityError(
            "Track A and Track B 'shared' sections differ — not comparable")
    if _canonical(a["shared"]) != _canonical(spec.get("shared")):
        raise ComparabilityError(
            "track 'shared' section does not match the comparability spec")

    shared = a["shared"]

    # (2b) schema versions the tracks agree on match this build's contracts.
    if shared.get("output_schema_version") != SCHEMA_VERSION:
        raise ComparabilityError(
            f"shared output_schema_version {shared.get('output_schema_version')!r} "
            f"!= {SCHEMA_VERSION!r}")
    if shared.get("taskbank_schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ComparabilityError(
            f"shared taskbank_schema_version {shared.get('taskbank_schema_version')!r} "
            f"!= {MANIFEST_SCHEMA_VERSION!r}")

    # (3) the pinned bank identity matches the real manifest on disk.
    ident = _manifest_identity(manifest_path)
    for key, actual in ident.items():
        if shared.get(key) != actual:
            raise ComparabilityError(
                f"shared {key} {shared.get(key)!r} != actual task bank {actual!r} "
                f"— the tracks are not pinned to the identical bank")
    if ident["family_count"] != len(FAMILIES):
        raise ComparabilityError(
            f"family_count {ident['family_count']} != {len(FAMILIES)} required families")

    # (4) shared limits: exactly the meaningful keys, identical across tracks.
    limits = shared.get("shared_limits")
    if not isinstance(limits, dict) or set(limits) != set(SHARED_LIMIT_KEYS):
        raise ComparabilityError(
            f"shared_limits keys {sorted(limits or {})} != {sorted(SHARED_LIMIT_KEYS)}")
    if "max_cost_eur" in limits:
        raise ComparabilityError("max_cost_eur must be per-track, not in shared_limits")

    # (5) Track A: local, zero cloud cost, Owner-supplied model identifier.
    pa = a["per_track"]
    if pa.get("execution") != "local_inference" or pa.get("mode") != "local_runtime":
        raise ComparabilityError("Track A must be local_inference / local_runtime")
    if pa.get("cost_model") != "local_zero_api_cost":
        raise ComparabilityError("Track A must be local_zero_api_cost")
    if pa.get("campaign_budget") != "excluded":
        raise ComparabilityError("Track A must be EXCLUDED from the €10 campaign budget")
    if pa.get("model_source") != "owner_supplied_via_env_or_registry":
        raise ComparabilityError(
            "Track A model must be Owner-supplied (env/registry), never fabricated here")
    if pa.get("model_identifier_status") != "OWNER_MODEL_IDENTIFIER_REQUIRED":
        raise ComparabilityError(
            "Track A must flag OWNER_MODEL_IDENTIFIER_REQUIRED "
            "('Qwen 3.5 9B' is not a canonical identifier)")
    # Guard against silently substituting a concrete canonical model name. Must be
    # an EXACT bare ${ENV_VAR} — an embedded default like ${VAR:-gpt-4o-mini} or
    # any surrounding text is refused (M1).
    model_a = str(pa.get("model_name", ""))
    if not is_bare_env_placeholder(model_a):
        raise ComparabilityError(
            f"Track A model_name {model_a!r} must be a bare env placeholder like "
            f"${{YOUTAB_ECO_MODEL}} (no default, no surrounding text) — the exact "
            f"tag is Owner/registry-supplied, never hardcoded here")

    # (6) Track B: non-Anthropic, Owner-selected, file credential, €10-bounded.
    pb = b["per_track"]
    if pb.get("execution") != "cloud_api" or pb.get("mode") != "real_provider":
        raise ComparabilityError("Track B must be cloud_api / real_provider")
    if pb.get("selection_status") != "OWNER_SELECTION_REQUIRED":
        raise ComparabilityError("Track B provider selection must be OWNER_SELECTION_REQUIRED")
    if pb.get("provider_name") != "OWNER_SELECTION_REQUIRED":
        raise ComparabilityError("Track B provider_name must be unselected (OWNER_SELECTION_REQUIRED)")
    constraints = pb.get("provider_constraints", {})
    excluded = [str(x).lower() for x in constraints.get("exclude", [])]
    if "anthropic" not in excluded:
        raise ComparabilityError("Track B must explicitly exclude Anthropic as a default")
    if pb.get("credential_source") != "file":
        raise ComparabilityError("Track B must use a file-based provider credential")
    if str(pb.get("max_cost_eur")) != "10.00":
        raise ComparabilityError("Track B cumulative max_cost_eur must be €10.00")
    alloc = pb.get("stage_allocation", {})
    try:
        from decimal import Decimal

        total = Decimal(str(alloc.get("canary"))) + Decimal(str(alloc.get("pilot"))) \
            + Decimal(str(alloc.get("full")))
        if total != Decimal(str(alloc.get("cumulative_max"))) or total != Decimal("10.00"):
            raise ComparabilityError(
                f"Track B stage allocation {alloc} must sum to the €10.00 ceiling")
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ComparabilityError(f"Track B stage_allocation is malformed: {exc}") from exc

    return {
        "tracks": ["A", "B"],
        "shared_manifest_sha256": ident["manifest_sha256"],
        "scenario_count": ident["scenario_count"],
        "family_count": ident["family_count"],
        "shared_limits": limits,
        "track_a_provenance": track_provenance(a),
        "track_b_provenance": track_provenance(b),
    }


def available_tracks() -> List[str]:
    return ["A", "B"]
