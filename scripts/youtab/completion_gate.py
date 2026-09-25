#!/usr/bin/env python3
"""Decide whether a task is done. The agent's self-assessment is not a verdict.

The agent reports what it believes it achieved. This gate re-establishes every
claim from a source the agent does not write, and decides. Where the two
disagree, the gate wins and the disagreement is recorded, because a run whose
author also grades it has no independent evidence in it at all.

What this replaces, and why it is not a port
--------------------------------------------
The uploaded kit's gate took ``verify_by`` out of the task spec and ran it as
``subprocess.run(cmd, shell=True)``. A task spec is a document a model writes,
so that is arbitrary command execution chosen by the thing being judged --
through the one component whose whole job is to be harder to fool than the
agent. It also failed *open* three ways: a missing brand-audit script returned
"clean", an unsupplied repository returned "clean", and ``files_changed`` and
``denied_actions`` were read from the agent's own JSON.

So the design here inverts both:

* **Commands are a governed list.** A spec names a verification ``id``;
  ``scripts/youtab/verifications.toml`` holds the argument array. There is no
  path by which spec content becomes a command. ``shell=False`` throughout, so
  no quoting, glob, substitution, pipe or ``;`` is interpreted.
* **Evidence comes from the source of truth.** File changes come from ``git``,
  not from the evidence document. A claim in the JSON is treated as a *claim*
  and scored against reality, never as the reality.
* **Every unknown is a failure.** Missing registry, unknown id, missing audit
  script, absent criteria, unreadable input: all fail closed. A gate that
  cannot check something has not found it acceptable.

Isolation
---------
Resource ceilings beyond wall-clock and output size (CPU, memory, PID count,
egress) are the verifier sandbox's job, not this process's -- a limit a
compromised process applies to itself is not a limit. This gate declares what
each verification needs (``network``, ``timeout_s``) so the sandbox can enforce
it, and refuses to run a verification whose declaration it cannot read.

Exit codes
    0   GOAL_REACHED       ("ready for review", never "ship it")
    3   GOAL_NOT_REACHED
    2   malformed input
    1   gate error
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REGISTRY = REPO_ROOT / "scripts" / "youtab" / "verifications.toml"

#: Hard ceiling on captured output per verification. A verification that floods
#: stdout must not become a memory exhaustion path into the gate.
MAX_OUTPUT_BYTES = 256 * 1024
#: Upper bound on any registry-declared timeout.
MAX_TIMEOUT_S = 3600

#: The only environment a verification receives. Inherited variables are how a
#: provider key reaches a subprocess, so the default is to inherit nothing and
#: name what is needed. PATH is included because argv[0] may be an interpreter.
_ENV_ALLOWLIST = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR",
    "LANG", "LC_ALL", "TZ", "PYTHONHASHSEED", "PYTHONUTF8",
)


class GateError(RuntimeError):
    """The gate cannot reach a verdict. Never silently downgraded to a pass."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitized_env() -> dict[str, str]:
    """A minimal, allowlisted environment for a verification subprocess."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    # Deterministic, and never inherits a profile home that holds credentials.
    env.setdefault("PYTHONHASHSEED", "0")
    env.setdefault("PYTHONUTF8", "1")
    return env


def load_registry(path: Path) -> dict[str, dict]:
    """Load the governed verification registry, or raise.

    A missing or unparseable registry is a gate error, not an empty registry:
    "there are no verifications" and "I could not read the verifications" must
    never produce the same verdict.
    """
    if not path.is_file():
        raise GateError(f"verification registry not found: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"verification registry unreadable: {exc}") from None

    entries = raw.get("verification") or []
    registry: dict[str, dict] = {}
    for entry in entries:
        vid = entry.get("id")
        argv = entry.get("argv")
        timeout = entry.get("timeout_s")
        if not vid or not isinstance(argv, list) or not argv:
            raise GateError(f"registry entry is not executable: {entry!r}")
        if not isinstance(timeout, int) or not (0 < timeout <= MAX_TIMEOUT_S):
            raise GateError(f"{vid}: timeout_s must be 1..{MAX_TIMEOUT_S}")
        if any(not isinstance(a, str) for a in argv):
            raise GateError(f"{vid}: argv must be strings")
        if vid in registry:
            raise GateError(f"duplicate verification id: {vid}")
        registry[vid] = {
            "id": vid,
            "argv": argv,
            "timeout_s": timeout,
            "network": bool(entry.get("network", False)),
            "description": entry.get("description", ""),
        }
    if not registry:
        raise GateError("verification registry defines nothing")
    return registry


def run_verification(entry: dict, cwd: Path) -> dict:
    """Execute one registry entry. No shell, ever."""
    argv = [a.replace("{python}", sys.executable) for a in entry["argv"]]
    try:
        proc = subprocess.run(
            argv,
            shell=False,  # the whole point: argv is a list, nothing is parsed
            cwd=str(cwd),
            capture_output=True,
            timeout=entry["timeout_s"],
            env=sanitized_env(),
        )
    except subprocess.TimeoutExpired:
        return {"id": entry["id"], "exit": 124,
                "output_tail": f"timeout after {entry['timeout_s']}s"}
    except OSError as exc:
        return {"id": entry["id"], "exit": 126, "output_tail": f"{type(exc).__name__}"}

    blob = (proc.stdout or b"") + (proc.stderr or b"")
    tail = blob[-MAX_OUTPUT_BYTES:].decode("utf-8", errors="replace")
    return {
        "id": entry["id"],
        "exit": proc.returncode,
        "output_tail": "\n".join(tail.strip().splitlines()[-20:]),
    }


def git_files_changed(repo: Path, base: str) -> int | None:
    """How many files changed, according to git. ``None`` if git cannot say.

    Read here rather than taken from the evidence document, because "did this
    run actually do anything" is precisely the claim an agent has an incentive
    to get wrong, and git is the record it does not author.
    """
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            shell=False, cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return len([ln for ln in proc.stdout.splitlines() if ln.strip()])


def evaluate(
    spec: dict,
    evidence: dict,
    *,
    registry: dict[str, dict],
    repo: Path,
    base: str,
    run: bool = True,
) -> dict:
    """Produce the verdict. Pure enough to test without spawning anything."""
    criteria = spec.get("acceptance_criteria") or []
    results: list[dict] = []
    failed: list[str] = []
    blocking: list[str] = []

    if not criteria:
        blocking.append("spec defines no acceptance criteria - nothing is provable")

    for crit in criteria:
        cid = crit.get("id", "?")
        vid = crit.get("verify_by")
        if not vid:
            blocking.append(f"{cid}: no verify_by")
            results.append({"id": cid, "status": "unverifiable"})
            failed.append(cid)
            continue
        if not isinstance(vid, str) or vid not in registry:
            # A spec naming something outside the registry is refused, not
            # executed. This is the branch the kit did not have.
            blocking.append(
                f"{cid}: verify_by {vid!r} is not a governed verification id"
            )
            results.append({"id": cid, "status": "unknown_verification"})
            failed.append(cid)
            continue
        if not run:
            results.append({"id": cid, "status": "not_run", "verification": vid})
            continue
        outcome = run_verification(registry[vid], repo)
        passed = outcome["exit"] == 0
        results.append({"id": cid, "status": "pass" if passed else "fail",
                        "verification": vid, **outcome})
        if not passed:
            failed.append(cid)

    # Authoritative, not self-reported.
    changed = git_files_changed(repo, base)
    if changed is None:
        blocking.append("git could not report changed files - cannot verify the run did anything")
    elif changed == 0:
        blocking.append("no file changes produced")

    claimed_changed = ((evidence.get("diff") or {}).get("files_changed"))
    if changed is not None and claimed_changed is not None and int(claimed_changed) != changed:
        blocking.append(
            f"evidence claims {claimed_changed} changed files; git reports {changed}"
        )

    for ev in evidence.get("denied_actions") or []:
        blocking.append(f"denied action attempted: {ev}")

    reached = not failed and not blocking
    claim = bool((evidence.get("agent_self_assessment") or {}).get("claims_success"))
    return {
        "verdict": "GOAL_REACHED" if reached else "GOAL_NOT_REACHED",
        "task_id": spec.get("task_id") or evidence.get("task_id") or "unknown",
        "checked": _now(),
        "criteria": results,
        "failed_criteria": failed,
        "blocking_issues": blocking,
        "git_files_changed": changed,
        "agent_claimed_success": claim,
        "claim_matched_reality": claim == reached,
        "next_step": (
            "Hand the branch and diff to a human. GOAL_REACHED is not permission to merge."
            if reached else
            "Do not use completion language. Fix the failures and re-run."
        ),
    }


def _load(path: str) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"verdict": "malformed", "error": f"{path}: {exc}"}))
        sys.exit(2)
    if not isinstance(data, dict):
        print(json.dumps({"verdict": "malformed", "error": f"{path}: not an object"}))
        sys.exit(2)
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="youtab-completion-gate")
    ap.add_argument("--spec", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--repo", default=str(REPO_ROOT))
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    args = ap.parse_args(argv)

    spec, evidence = _load(args.spec), _load(args.evidence)
    try:
        registry = load_registry(Path(args.registry))
    except GateError as exc:
        print(json.dumps({"verdict": "gate_error", "error": str(exc)}))
        return 1

    verdict = evaluate(
        spec, evidence, registry=registry, repo=Path(args.repo), base=args.base,
    )
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["verdict"] == "GOAL_REACHED" else 3


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(main())
    except GateError as exc:
        print(json.dumps({"verdict": "gate_error", "error": str(exc)}))
        sys.exit(1)
