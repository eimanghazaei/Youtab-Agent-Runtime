"""Versioned task-bank loader + validator.

Loads the JSON manifest (mirroring ``tests/fixtures/wakeword/samples.json``: a
top-level object with metadata + a ``scenarios`` array) into validated
:class:`~tests.benchmark.harness.schema.Scenario` objects, and asserts the bank
is well-formed: unique ids, known family/mode/oracle/executor, and full coverage
of all 20 required families with the required minimum scenario count.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from .executors import EXECUTORS
from .oracles import ORACLES
from .schema import FAMILIES, MODE_REAL_PROVIDER, Scenario

MANIFEST_SCHEMA_VERSION = "benchmark_tasks.v1"
MIN_SCENARIOS = 40


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "tasks" / "manifest.json"


def load_manifest(path: Path | None = None) -> Dict:
    p = path or default_manifest_path()
    return json.loads(p.read_text(encoding="utf-8"))


def load_scenarios(path: Path | None = None) -> List[Scenario]:
    data = load_manifest(path)
    scenarios = [
        Scenario(
            id=row["id"],
            family=row["family"],
            mode=row["mode"],
            title=row.get("title", row["id"]),
            oracle=row["oracle"],
            executor=row["executor"],
            params=row.get("params", {}),
            self_reported_success=row.get("self_reported_success", True),
            expected_verdict=row.get("expected_verdict", "pass"),
            engine=row.get("engine"),
            adversarial_variant=row.get("adversarial_variant"),
            description=row.get("description", ""),
        )
        for row in data.get("scenarios", [])
    ]
    return scenarios


def validate(path: Path | None = None) -> List[Scenario]:
    """Validate the manifest and return the scenarios; raises on any defect.

    Deliberately fails LOUDLY (never vacuously): it first asserts the bank is
    non-empty and meets the minimum, so a manifest that silently lost its
    scenarios cannot pass validation.
    """
    data = load_manifest(path)
    version = data.get("schema_version")
    if version != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"manifest schema_version {version!r} != {MANIFEST_SCHEMA_VERSION!r}")
    scenarios = load_scenarios(path)
    if len(scenarios) < MIN_SCENARIOS:
        raise ValueError(
            f"task bank has {len(scenarios)} scenarios; require >= {MIN_SCENARIOS}")

    ids = [s.id for s in scenarios]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate scenario ids: {dupes}")

    for s in scenarios:
        if s.oracle not in ORACLES:
            raise ValueError(f"scenario {s.id!r}: unknown oracle {s.oracle!r}")
        if s.executor not in EXECUTORS:
            raise ValueError(f"scenario {s.id!r}: unknown executor {s.executor!r}")
        if s.expected_verdict not in ("pass", "fail", "unknown"):
            raise ValueError(
                f"scenario {s.id!r}: bad expected_verdict {s.expected_verdict!r}")
        # A real_provider scenario is expected to be "unknown" in deterministic
        # mode; guard against a manifest that claims a deterministic verdict for
        # a dimension that cannot be judged offline.
        if s.mode == MODE_REAL_PROVIDER and s.expected_verdict != "unknown":
            raise ValueError(
                f"scenario {s.id!r}: real_provider scenarios must expect "
                f"'unknown' in deterministic mode (got {s.expected_verdict!r})")

    covered = {s.family for s in scenarios}
    missing = sorted(set(FAMILIES) - covered)
    if missing:
        raise ValueError(f"families with no scenario: {missing}")

    return scenarios
