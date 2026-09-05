"""Live-benchmark preflight helpers for the harness (WAVE-30B §7/§10/§11/§12/§13).

Pure, dependency-light helpers the CLI uses to fail CLOSED before any live
provider call:

* :data:`STAGE_PROFILES` — the Stage-1 Canary / Stage-2 Pilot / Stage-3 Full
  limit + scenario profiles (no legacy 500-iteration default anywhere).
* :func:`validate_output_dir` — refuses an unsafe artifact directory (inside the
  repo, a symlink, a cloud-sync folder, or colliding with prior artifacts) and
  drops a retention marker.
* :func:`verify_runtime_sha` — refuses to run unless the runtime's reported build
  SHA (container) or the local HEAD + clean worktree (source) match the
  authorized SHA.
* :func:`assert_live_safety` — refuses a live run when redaction or budget
  enforcement is off, or a production dataset is selected.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Mapping, Optional

# Consumer cloud-sync markers (matched case-insensitively against path parts).
_CLOUD_MARKERS = (
    "onedrive", "dropbox", "google drive", "googledrive", "google_drive",
    "icloud drive", "icloud", "box sync", "nextcloud", "pcloud",
)

# Stage profiles (WAVE-30B §10). "scenarios" is a harness selection bound; the
# rest map to the runtime per-run RunLimits. Costs are EUR reservations that sum
# to <= the €10 campaign ceiling.
STAGE_PROFILES: dict[str, dict[str, Any]] = {
    "canary": {
        "scenarios": 1,
        "limits": {
            "max_iterations": 1,
            "max_requests": 1,
            "max_retries": 0,
            "max_concurrency": 1,
            "max_input_tokens": 4096,
            "max_output_tokens": 512,
            "max_total_tokens": 4608,
            "max_runtime_seconds": 60,
            "max_cost_eur": "0.10",
            "failure_threshold": 1,
        },
        "failover": False,
    },
    "pilot": {
        "scenarios": 10,
        "limits": {
            "max_iterations": 4,
            "max_requests": 40,
            "max_retries": 1,
            "max_concurrency": 1,
            "max_total_tokens": 250_000,
            "max_runtime_seconds": 1_200,
            "max_cost_eur": "2.00",
            "failure_threshold": 2,
        },
        "failover": False,
    },
    "full": {
        "scenarios": 49,
        "limits": {
            "max_iterations": 8,
            "max_requests": 392,
            "max_retries": 1,
            "max_concurrency": 1,
            "max_total_tokens": 1_000_000,
            "max_runtime_seconds": 7_200,
            "max_cost_eur": "10.00",
            "failure_threshold": 3,
        },
        "failover": False,
    },
}


class PreflightError(RuntimeError):
    """A preflight safety check failed — the live run must not proceed."""


def _parts_lower(path: Path) -> list[str]:
    return [p.lower() for p in path.parts]


def validate_output_dir(out: Path, *, repo_root: Path, force: bool = False) -> Path:
    """Validate the artifact output directory. Raises :class:`PreflightError` on
    an unsafe location; creates it owner-only and writes a retention marker."""
    out = Path(out)
    if not out.is_absolute():
        out = (Path.cwd() / out).resolve()
    # Not a symlink.
    if out.is_symlink():
        raise PreflightError(f"output dir {out} is a symlink — refused")
    # Outside the repo working tree.
    try:
        out.relative_to(repo_root.resolve())
        raise PreflightError(
            f"output dir {out} is inside the repo {repo_root} — choose a path outside"
        )
    except ValueError:
        pass  # good: not under the repo
    # Not in a cloud-sync folder.
    lowered = _parts_lower(out)
    for marker in _CLOUD_MARKERS:
        if marker in lowered:
            raise PreflightError(
                f"output dir {out} is inside a cloud-synced folder ({marker!r}) — refused"
            )
    # No collision with prior untrusted artifacts.
    results = out / "results.jsonl"
    if results.exists() and results.stat().st_size > 0 and not force:
        raise PreflightError(
            f"output dir {out} already holds results.jsonl — refusing to overwrite "
            f"prior artifacts (pass force=True to override)"
        )
    out.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(out, 0o700)
        except OSError:
            pass
    # Retention marker (90-day policy).
    marker = out / "retention.json"
    if not marker.exists():
        marker.write_text(
            '{"retention_days": 90, "contains": "redacted benchmark evidence only"}\n',
            encoding="utf-8",
        )
    return out


def _git(args: list[str], *, cwd: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=15
        )
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def verify_runtime_sha(
    preflight: Mapping[str, Any],
    *,
    expected_sha: str,
    repo_root: Path,
    require_clean_worktree: bool = True,
) -> None:
    """Refuse unless runtime build == authorized SHA.

    Container runtime: preflight.build_sha must equal expected_sha. Source
    runtime (build_sha absent): the local HEAD must equal expected_sha and, when
    required, the worktree must be clean.
    """
    if not expected_sha:
        raise PreflightError("no authorized SHA supplied (--expected-sha) — refusing live run")
    build_sha = (preflight or {}).get("build_sha")
    if build_sha:
        if not (build_sha == expected_sha or build_sha.startswith(expected_sha)
                or expected_sha.startswith(build_sha)):
            raise PreflightError(
                f"runtime build SHA {build_sha!r} != authorized {expected_sha!r}"
            )
        return
    # Source checkout: verify local HEAD + clean worktree.
    head = _git(["rev-parse", "HEAD"], cwd=repo_root)
    if head is None:
        raise PreflightError(
            "runtime reported no build SHA and local git HEAD is unavailable — "
            "cannot verify the runtime is the authorized build"
        )
    if not (head == expected_sha or head.startswith(expected_sha)
            or expected_sha.startswith(head)):
        raise PreflightError(
            f"local runtime HEAD {head!r} != authorized {expected_sha!r}"
        )
    if require_clean_worktree:
        status = _git(["status", "--porcelain"], cwd=repo_root)
        if status is None:
            raise PreflightError("could not determine worktree cleanliness — refusing")
        dirty = [ln for ln in status.splitlines() if ln.strip() and "scratchpad/" not in ln]
        if dirty:
            raise PreflightError(
                f"source worktree is not clean ({len(dirty)} change(s)) — refusing live run"
            )


def assert_live_safety(preflight: Mapping[str, Any]) -> None:
    """Refuse a live run unless the runtime's safety posture is sound."""
    p = preflight or {}
    if not p.get("service_ready", False):
        raise PreflightError("runtime is not service_ready — refusing")
    if p.get("redaction_enabled") is not True:
        raise PreflightError("runtime redaction is disabled — refusing live run")
    if p.get("budget_enforcement_enabled") is not True:
        raise PreflightError("runtime budget enforcement is disabled — refusing")
    if p.get("no_production_dataset") is not True:
        raise PreflightError("runtime reports a production dataset selected — refusing")
