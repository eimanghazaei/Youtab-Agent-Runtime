"""Benchmark record + observation/verdict schema (frozen contract 7).

Pure data + tiny stdlib helpers. Nothing here imports the heavy runtime modules,
so this module stays importable on any host (including a network-denied sandbox).
The verdict types deliberately have no access to ``self_reported_success`` when
deciding — the oracle is handed observable state only.

The canonical enums (:class:`~youtab_runtime.run_states.Outcome`) are reused so
the benchmark's verdict vocabulary can never drift from the shared WAVE-26
failure-state vocabulary.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Reuse the shared verdict vocabulary (contract 8). Import guarded so the schema
# still imports on a host without the editable install (e.g. a docs build);
# callers that actually produce records always have it available.
try:  # pragma: no cover - exercised in both branches across environments
    from youtab_runtime.run_states import Outcome
except Exception:  # pragma: no cover - degraded fallback keeps schema importable
    from enum import Enum

    class Outcome(str, Enum):  # type: ignore[no-redef]
        PASS = "pass"
        FAIL = "fail"
        UNKNOWN = "unknown"


SCHEMA_VERSION = "benchmark.v1"
GENERATED_LABEL = "PRELIMINARY_NOT_RELEASE_EVIDENCE"

#: Modes (contract 7). Deterministic offline is the CI-required mode.
MODE_DETERMINISTIC = "deterministic"
MODE_LOCAL_RUNTIME = "local_runtime"
MODE_REAL_PROVIDER = "real_provider"
MODES = frozenset({MODE_DETERMINISTIC, MODE_LOCAL_RUNTIME, MODE_REAL_PROVIDER})

#: The 20 required capability families (Owner §7). The manifest's every scenario
#: must name exactly one of these; the task-bank validator enforces coverage.
FAMILIES = (
    "single_step_completion",
    "multi_step_completion",
    "tool_selection",
    "tool_args",
    "forbidden_tool_rejection",
    "prompt_injection",
    "tool_output_injection",
    "exfil_prevention",
    "provider_failure_recovery",
    "tool_failure_recovery",
    "timeout_cancel",
    "restart_state_recovery",
    "effect_idempotency",
    "duplicate_run",
    "concurrency_isolation",
    "cross_principal_isolation",
    "win_linux_parity",
    "token_cost",
    "unnecessary_tool_calls",
    "truthful_incomplete_reporting",
)

#: Observation sources a verdict may cite (contract 7 ``observation_source``).
OBSERVATION_SOURCES = (
    "state",
    "events",
    "effects",
    "egress",
    "process",
    "artifacts",
)

#: Sentinel a metric/record carries when a dimension cannot be judged offline and
#: needs a live provider (never silently coerced to pass or 0).
OWNER_LIVE_PROVIDER_ACTION_REQUIRED = "OWNER_LIVE_PROVIDER_ACTION_REQUIRED"


# --------------------------------------------------------------------------- #
# Verdict                                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class Verdict:
    """A state-derived judgement. ``outcome`` is pass|fail|unknown (contract 8).

    ``honesty_divergence`` is filled by the recorder by comparing the (recorded
    but never consulted) self-report to this state-derived outcome — it is NOT an
    input the oracle sees.
    """

    outcome: Outcome
    reason: str
    observation_source: List[str] = field(default_factory=list)
    evidence_refs: List[Any] = field(default_factory=list)
    evidence: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, reason: str, *, source: Optional[List[str]] = None,
           refs: Optional[List[Any]] = None, **ev: Any) -> "Verdict":
        return cls(Outcome.PASS, reason, source or [], refs or [], ev)

    @classmethod
    def fail(cls, reason: str, *, source: Optional[List[str]] = None,
             refs: Optional[List[Any]] = None, **ev: Any) -> "Verdict":
        return cls(Outcome.FAIL, reason, source or [], refs or [], ev)

    @classmethod
    def unknown(cls, reason: str, *, source: Optional[List[str]] = None,
                refs: Optional[List[Any]] = None, **ev: Any) -> "Verdict":
        return cls(Outcome.UNKNOWN, reason, source or [], refs or [], ev)


# --------------------------------------------------------------------------- #
# Observation — everything assembled from OBSERVABLE state                     #
# --------------------------------------------------------------------------- #
@dataclass
class Observation:
    """State an oracle reads to reach a verdict. Never carries the self-report as
    proof: ``self_reported_success`` is stored here only so the recorder can
    compute the honesty divergence; oracles must not read it.
    """

    run_id: str
    tenant: str
    user: str
    mode: str
    seam: str
    platform: str
    # Durable, ordered, principal-scoped observable state (contract 3/5/8).
    events: List[Dict[str, Any]] = field(default_factory=list)      # journal rows
    effects: List[Dict[str, Any]] = field(default_factory=list)     # effect ledger
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    durable_status: Optional[str] = None                            # terminal run status
    # Filesystem side-effect surface.
    workspace: Optional[Path] = None
    pre_hash: Optional[str] = None
    # Recorded-but-never-judged agent claim + usage self-report.
    self_reported_success: bool = False
    reported_usage: Optional[Dict[str, Any]] = None
    # Timing + engine pin.
    timings: Dict[str, Any] = field(default_factory=dict)
    engine_pinned: Optional[str] = None
    provenance: Dict[str, Any] = field(default_factory=dict)

    # -- convenience accessors (used by oracles) ---------------------------
    def events_of(self, category: str) -> List[Dict[str, Any]]:
        return [e for e in self.events if e.get("category") == category]

    def event_kinds(self, category: Optional[str] = None) -> List[str]:
        src = self.events if category is None else self.events_of(category)
        return [e.get("kind") for e in src]

    def egress_events(self) -> List[Dict[str, Any]]:
        return self.events_of("egress")

    def effects_in_state(self, state: str) -> List[Dict[str, Any]]:
        return [e for e in self.effects if e.get("state") == state]


# --------------------------------------------------------------------------- #
# Scenario (one row of the manifest)                                           #
# --------------------------------------------------------------------------- #
@dataclass
class Scenario:
    """One versioned benchmark scenario, loaded from the JSON manifest."""

    id: str
    family: str
    mode: str
    title: str
    oracle: str
    executor: str
    params: Dict[str, Any] = field(default_factory=dict)
    # What a (possibly dishonest) agent claims about this run — recorded, never
    # used for the verdict. Drives honesty-divergence measurement.
    self_reported_success: bool = True
    # The verdict a correctly-functioning runtime MUST yield, per mode. This is
    # what the deterministic CI gate asserts (a substrate regression flips it).
    expected_verdict: str = "pass"
    engine: Optional[str] = None
    adversarial_variant: Optional[str] = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ValueError(f"scenario {self.id!r}: unknown family {self.family!r}")
        if self.mode not in MODES:
            raise ValueError(f"scenario {self.id!r}: unknown mode {self.mode!r}")


# --------------------------------------------------------------------------- #
# The persisted record (contract 7)                                           #
# --------------------------------------------------------------------------- #
@dataclass
class BenchmarkRecord:
    """One (scenario, repetition) result row written to results.jsonl."""

    scenario_id: str
    scenario_family: str
    repetition: int
    mode: str
    runtime_head: str
    platform_tag: str
    engine_pinned: Optional[str]
    principal: Dict[str, str]
    run_id: str
    verdict: str
    self_reported_success: bool
    honesty_divergence: bool
    observation_source: List[str]
    metrics: Dict[str, Any]
    evidence_refs: List[Any]
    provenance: Dict[str, Any]
    reason: str = ""
    schema_version: str = SCHEMA_VERSION
    generated_label: str = GENERATED_LABEL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_label": self.generated_label,
            "runtime_head": self.runtime_head,
            "platform_tag": self.platform_tag,
            "mode": self.mode,
            "scenario_id": self.scenario_id,
            "scenario_family": self.scenario_family,
            "repetition": self.repetition,
            "engine_pinned": self.engine_pinned,
            "principal": self.principal,
            "run_id": self.run_id,
            "verdict": self.verdict,
            "self_reported_success": self.self_reported_success,
            "honesty_divergence": self.honesty_divergence,
            "observation_source": self.observation_source,
            "metrics": self.metrics,
            "evidence_refs": self.evidence_refs,
            "provenance": self.provenance,
            "reason": self.reason,
        }


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def platform_tag() -> str:
    """Coarse OS tag for the parity dimension."""
    return "win32" if sys.platform == "win32" else (
        "darwin" if sys.platform == "darwin" else "linux"
    )


def runtime_head(repo_root: Optional[Path] = None) -> str:
    """Return ``git rev-parse HEAD`` at run time — NEVER a hardcoded SHA.

    Falls back to the ``GITHUB_SHA`` CI env or ``"unknown"`` when git is not
    available (e.g. a shallow tarball checkout), so a record is always honest
    about what it could and could not determine.
    """
    root = str(repo_root) if repo_root else str(Path(__file__).resolve().parents[3])
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        head = out.stdout.strip()
        if out.returncode == 0 and head:
            return head
    except Exception:
        pass
    return os.environ.get("GITHUB_SHA", "unknown")


def sha256_norm(data: bytes) -> str:
    """SHA-256 of newline-normalized bytes (CRLF -> LF) for Win/Linux parity."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def hash_tree(root: Path) -> str:
    """Stable, newline-normalized hash of a directory tree's file contents.

    Newline normalization keeps the digest identical across Windows and Linux so
    the parity oracle compares content, not line endings.
    """
    h = hashlib.sha256()
    if not root.exists():
        return h.hexdigest()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix().encode("utf-8")
            try:
                data = p.read_bytes().replace(b"\r\n", b"\n")
            except OSError:
                continue
            h.update(rel + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()
