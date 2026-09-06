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
    raw = Path(out)
    # Reject a symlinked FINAL component before resolving, then resolve the whole
    # path (following any symlinked PARENT) so containment/cloud checks see the
    # real target — a symlinked parent cannot smuggle artifacts back into the repo.
    if raw.is_symlink():
        raise PreflightError(f"output dir {raw} is a symlink — refused")
    out = raw.resolve() if raw.is_absolute() else (Path.cwd() / raw).resolve()
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
    # The authorized SHA must be a full or a deliberately-long (>=12) hex prefix the
    # OPERATOR supplies. A short/typo'd value that could match a broad commit set is
    # refused. The value being verified (build_sha / HEAD) is NEVER allowed to be a
    # prefix of expected_sha — otherwise the runtime under test could report a
    # 1-char SHA and satisfy the pin (H2/A#3).
    exp = expected_sha.strip().lower()
    if len(exp) < 12 or any(c not in "0123456789abcdef" for c in exp):
        raise PreflightError(
            f"authorized SHA {expected_sha!r} must be a >=12-char hex commit id"
        )

    def _matches(reported: str) -> bool:
        r = (reported or "").strip().lower()
        if len(r) < 40 or any(c not in "0123456789abcdef" for c in r):
            return False  # reported build must be a full 40-hex commit id
        return r == exp or r.startswith(exp)

    build_sha = (preflight or {}).get("build_sha")
    if build_sha:
        if not _matches(build_sha):
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
    if not _matches(head):
        raise PreflightError(
            f"local runtime HEAD {head!r} != authorized {expected_sha!r}"
        )
    if require_clean_worktree:
        status = _git(["status", "--porcelain"], cwd=repo_root)
        if status is None:
            raise PreflightError("could not determine worktree cleanliness — refusing")
        # Only an UNTRACKED top-level scratchpad/ is ignored (porcelain "?? scratchpad/…").
        # Any modified/renamed TRACKED file — including a nested path merely
        # containing "scratchpad" — still marks the worktree dirty.
        dirty = [
            ln for ln in status.splitlines()
            if ln.strip() and not ln.startswith("?? scratchpad/")
        ]
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
    # A live-benchmark runtime MUST source the provider key from a file (strict
    # tier), never a plaintext environment value — compensating gate for any read
    # path that might not itself refuse plaintext under live-benchmark mode (A#1).
    if p.get("live_benchmark_mode") is True and p.get("provider_credential_source") != "file":
        raise PreflightError(
            "live-benchmark mode requires a file-based provider credential "
            f"(provider_credential_source={p.get('provider_credential_source')!r}) — refusing"
        )


_VERIFIED_LOCAL_CLASSES = frozenset({"loopback", "private", "link_local", "cgnat"})


def assert_engine_attestation(
    preflight: Mapping[str, Any],
    track: Mapping[str, Any],
    *,
    expected_model_digest: Optional[str] = None,
) -> None:
    """Refuse a live Track A run unless the runtime's EFFECTIVE engine binding
    matches the track contract (WAVE-30D §B1/§B3).

    Verifies, before the first task, that the runtime resolved the exact engine
    profile, provider, a concrete Owner-supplied model, a verified-local endpoint
    class, and the ``local_zero_verified`` cost policy the track declares — so a
    run can never execute (or attest) the wrong engine/model/cost policy. When
    ``expected_model_digest`` is supplied, the Ollama manifest digest must match
    it EXACTLY (full 64-hex, no-prefix), mirroring :func:`verify_runtime_sha`.
    """
    p = preflight or {}
    if p.get("engine_attestation_error"):
        raise PreflightError(
            f"runtime engine attestation error: {p['engine_attestation_error']!r} — refusing"
        )
    att = p.get("engine_attestation")
    if not isinstance(att, dict):
        raise PreflightError("runtime returned no engine attestation — refusing live run")

    per = (track or {}).get("per_track", {}) or {}
    want_engine = per.get("engine_profile")
    if want_engine and att.get("engine_profile") != want_engine:
        raise PreflightError(
            f"engine mismatch: runtime attested {att.get('engine_profile')!r} "
            f"!= track {want_engine!r} — refusing"
        )
    want_provider = (per.get("provider_name") or "").strip().lower()
    if want_provider and (att.get("provider") or "").strip().lower() != want_provider:
        raise PreflightError(
            f"provider mismatch: runtime attested {att.get('provider')!r} "
            f"!= track {want_provider!r} — refusing"
        )

    # Track A local invariants: local execution, verified-local endpoint class,
    # and the local-zero cost policy (never a metered/cloud policy).
    if per.get("execution") == "local_inference" or per.get("mode") == "local_runtime":
        if att.get("execution") != "local":
            raise PreflightError(
                f"engine is not executing locally (execution={att.get('execution')!r}) — refusing"
            )
        if not att.get("endpoint_authorized"):
            raise PreflightError("engine endpoint is not authorized — refusing")
        if att.get("endpoint_class") not in _VERIFIED_LOCAL_CLASSES:
            raise PreflightError(
                f"endpoint class {att.get('endpoint_class')!r} is not a verified-local "
                "class (loopback/private/link_local/cgnat) — refusing"
            )
        if att.get("provider_cost_policy") != "local_zero_verified":
            raise PreflightError(
                f"cost policy {att.get('provider_cost_policy')!r} is not "
                "local_zero_verified — refusing"
            )

    # The Owner must have supplied the concrete model tag (YOUTAB_ECO_MODEL); the
    # committed placeholder is never accepted as the effective model.
    if att.get("model_identifier_status") != "resolved" or not att.get("model"):
        raise PreflightError(
            "effective model is not resolved "
            f"(status={att.get('model_identifier_status')!r}); supply the Owner model "
            "tag via YOUTAB_ECO_MODEL — refusing"
        )

    # Optional full model-manifest digest pin. Full 64-hex only; the reported
    # digest may never be a prefix of the expected (mirror verify_runtime_sha).
    if expected_model_digest:
        exp = expected_model_digest.strip().lower()
        if len(exp) != 64 or any(c not in "0123456789abcdef" for c in exp):
            raise PreflightError(
                f"--expected-model-digest {expected_model_digest!r} must be a 64-hex sha256"
            )
        if att.get("ollama_digest_status") != "verified_present":
            raise PreflightError(
                f"model manifest digest not verified "
                f"(status={att.get('ollama_digest_status')!r}) — refusing"
            )
        got = (att.get("ollama_model_digest") or "").strip().lower()
        if len(got) != 64 or got != exp:
            raise PreflightError(
                f"model manifest digest {got or '<none>'!r} != expected {exp!r} — refusing"
            )
