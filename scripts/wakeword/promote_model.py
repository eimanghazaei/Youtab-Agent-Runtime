#!/usr/bin/env python3
"""Promote a wake-word candidate into ``tools/wakewords/``, or refuse and say why.

Why this exists
---------------
``tools/wakewords/hey_youtab.onnx`` and ``.tflite`` are the detector Youtab
ships. Eleven candidates across seven synthetic rounds all failed acceptance —
the best reached 4/5 and never on false rejects — and the shipped pair is itself
a synthetic-era export that stays in place only because nothing better has
qualified. Both of its digests are recorded in
``retired_synthetic_artifacts.json`` as retired material.

So the danger here is not a missing feature. It is a person copying a file into
``tools/wakewords/`` and declaring victory: the runtime loads whatever bytes sit
at that path, ``MODEL_CARD.md`` is generated prose, and nothing downstream can
tell a qualified export from a hopeful one. This module is the only sanctioned
way those bytes change, and it refuses by default.

What it will not do
-------------------
Every requirement below is checked individually, by id, and a promotion happens
only if all of them pass. A refusal names the check that refused and what it
wanted, because "promotion failed" is not actionable.

============================  ==================================================
check id                      what it will not accept
============================  ==================================================
``record_schema``             a record from another schema, or one missing a section
``candidate_identity``        an unnamed candidate, a synthetic-era round, a
                              missing or out-of-range operating threshold
``artifact_bytes``            an artifact whose bytes do not hash to the record
``measured_artifacts``        acceptance measured against different bytes, or at
                              a different threshold, than the ones being promoted
``committed_artifacts``       artifacts that are untracked or dirty in git, so
                              what is promoted is what was reviewed
``acceptance_onnx``           anything less than 5/5 on the ONNX backend
``acceptance_tflite``         anything less than 5/5 on the TFLite backend
``parity``                    one detection disagreement between the backends
``statistical_completeness``  a point estimate with no denominator, or evidence
                              counted in windows where the unit is the utterance
``statistical_power``         a sample too small to bound the target it claims —
                              11.659 h of negative audio bounds 0.257/h, and
                              target 2 is 0.2/h, so a clean run there is refused
``statistical_consistency``   a recomputed bound the record disagrees with
``source_manifests``          a dataset with no frozen ``manifest_sha256``
``licence_provenance``        a source with no licence or no provenance record
``no_synthetic_lineage``      a synthetic source kind, a synthetic initialisation,
                              or a dataset contract that does not measure
                              ``synthetic_samples: 0``
``retired_artifacts``         bytes listed in ``retired_synthetic_artifacts.json``
                              — including the pair this repository ships today
``ci_success``                a red, partial or foreign-commit CI run
``authorization``             a record with no Owner authorization over its own
                              digest (see "Signing", below)
``destination_state``         a destination whose shipped bytes do not match its
                              own ledger and ``SHA256SUMS`` — i.e. one somebody
                              has already copied a file into
============================  ==================================================

Signing, and the exact Owner action to upgrade it
------------------------------------------------
A detached cryptographic signature is the right end state and it needs a
credential this repository does not have: a private key held by the Owner.
Rather than accept an unverifiable ``signature`` field — which would be worse
than nothing, because it would read as cryptography — the gate implements the
*repository-governed equivalent* and refuses every other method by name.

The governed equivalent: the promotion record is content-addressed
(``record_sha256`` over its canonical serialization), and promotion requires a
**tracked, hash-verified Owner decision document that quotes that digest
verbatim**, along with the candidate id and both artifact digests. Editing any
evidence in the record changes its digest, which invalidates the decision, which
can only be re-issued by a reviewed commit. Branch protection is therefore the
signing authority: the thing an attacker would have to forge is a merge.

**Owner action to upgrade to real signing** — one commit, no code change here
beyond adding the method:

1. ``ssh-keygen -t ed25519 -C wakeword-promotion -f ~/.ssh/youtab_promotion``
   (or reuse the Owner's existing signing key).
2. Commit the public key as ``scripts/wakeword/PROMOTION_SIGNERS`` in
   OpenSSH ``allowed_signers`` format:
   ``owner@youtab namespaces="wakeword-promotion" ssh-ed25519 AAAA...``.
3. Sign the digest, not the file:
   ``printf '%s' <record_sha256> > record.sha256`` then
   ``ssh-keygen -Y sign -f ~/.ssh/youtab_promotion -n wakeword-promotion record.sha256``.
4. Set ``authorization.method`` to ``"ssh-signature"`` and add
   ``authorization.signature`` (path to ``record.sha256.sig``), then add that
   method to ``_AUTHORIZATION_METHODS`` here, verified with
   ``ssh-keygen -Y verify -f scripts/wakeword/PROMOTION_SIGNERS -I owner@youtab
   -n wakeword-promotion -s record.sha256.sig < record.sha256``.

Until step 2 exists, ``"ssh-signature"`` is refused rather than trusted.

Atomicity and rollback
----------------------
Three files change on a promotion (both artifacts and ``SHA256SUMS``) and no
filesystem offers one atomic replace across three paths. So:

* the **rollback bundle is written and re-verified first**, before a single
  shipped byte is touched, and it is renamed into place under a name that never
  already exists;
* each new file is staged as ``<name>.incoming`` and fsynced;
* a **journal** naming the intended end state is written and fsynced, and only
  then are the staged files moved onto their targets with ``os.replace``, which
  is atomic per path;
* ``recover()`` — which ``promote`` and ``rollback`` run before anything else —
  reads the journal and *decides*: every target either already carries the new
  bytes or has a verified ``.incoming`` beside it, in which case it rolls
  forward; otherwise it restores the bundle and rolls back. It never leaves a
  mixture, and it is idempotent.

So a crash between two replaces leaves a journal, and the next invocation of any
command resolves it in one direction. ``verify`` is read-only and reports a
pending journal as a failure rather than repairing it, because a diagnostic that
mutates is not a diagnostic.

Usage::

    promote_model.py check    --record R.json --candidate-dir DIR --dest D
    promote_model.py promote  --record R.json --candidate-dir DIR --dest D
    promote_model.py rollback --dest D
    promote_model.py recover  --dest D
    promote_model.py verify   --dest D

``--dest`` is required and has no default on purpose: the path that ships must
be typed.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

#: Bumped when the promotion record changes shape. A record carrying a version
#: this gate does not know is refused, not guessed at — the same rule
#: ``freeze_manifest.py`` applies to a dataset manifest.
SCHEMA_VERSION = 1

#: The two files that ship, in the order ``tools/wakewords/SHA256SUMS`` lists
#: them. Both, always: macOS ARM64 loads the tflite pair
#: (dscripka/openWakeWord#336), so promoting one is promoting half a model.
ARTIFACT_NAMES: tuple[str, ...] = ("hey_youtab.onnx", "hey_youtab.tflite")

#: Backend id -> the artifact it loads. Acceptance is measured per backend and
#: both have to pass independently; an average over the two would let a good
#: ONNX result carry a bad tflite one onto a Mac.
BACKEND_ARTIFACTS: dict[str, str] = {
    "onnx": "hey_youtab.onnx",
    "tflite": "hey_youtab.tflite",
}


@dataclass(frozen=True)
class Target:
    """One acceptance target, and what kind of evidence can support it."""

    id: int
    name: str
    limit: float
    comparison: str  # "<=" or "=="
    kind: str  # "proportion" | "rate_per_hour" | "count"
    denominator: str  # the record field carrying the sample size: "n" or "hours"
    unit_must_contain: str = ""

    @property
    def denominator_label(self) -> str:
        """How the denominator reads in a refusal: "168 trials", "11.659 hours"."""
        return "hours" if self.denominator == "hours" else "trials"


#: The five acceptance targets, exactly as ``ROUND6_DESIGN.md`` fixed them and
#: ``round8_config.py`` restates them. Hard-coded rather than read from the
#: record: "the targets are unchanged" is the claim under test, and a record
#: that shipped its own limits would validate itself.
#:
#: ``unit_must_contain`` encodes the predeclaration's unit of evidence —
#: "utterance, never window; 7 deterministic framings of one utterance are one
#: trial". Counting the framings would multiply n by seven and shrink every
#: bound below by the square root of that, for free.
ACCEPTANCE_TARGETS: dict[int, Target] = {
    1: Target(1, "false rejects", 0.05, "<=", "proportion", "n", "utterance"),
    2: Target(
        2,
        "recorded-human-speech false activations",
        0.2,
        "<=",
        "rate_per_hour",
        "hours",
        "hour",
    ),
    3: Target(
        3, "deliberate near-miss false accepts", 0.02, "<=", "proportion", "n", "utterance"
    ),
    4: Target(4, "background-only false accepts", 0, "==", "count", "n"),
    5: Target(5, "ONNX/TFLite numerical and detection parity", 0, "==", "count", "n"),
}

#: One-sided 95%. Every bound in the round 8 predeclaration is computed at this
#: level, so a record that declared its own alpha could buy power by relaxing
#: confidence. It is asserted against the record rather than read from it.
ALPHA = 0.05

#: Relative tolerance when comparing a recorded figure with the recomputed one.
#: The same form ``round8_config.check`` uses: enough slack for a four-decimal
#: figure, not enough to hide a different sample size.
_TOLERANCE = 0.02
_ABSOLUTE_TOLERANCE = 5e-5

#: Dataset roles a candidate's sources may carry, and the ``freeze_manifest``
#: usage each one requires. A sealed set consumed as training data is the
#: expensive mistake, so the mapping is asserted rather than trusted.
ROLE_USAGE: dict[str, str] = {
    "train": "training",
    "validation": "validation",
    "sealed": "sealed-evaluation",
}

#: Source kinds that can feed a candidate — a closed allow-list, because "not
#: synthetic" as a boolean is a claim and an allow-list is a structure. Nothing
#: synthesized, voice-converted or pitch-shifted has a name here to be spelled.
RECORDED_SOURCE_KINDS: frozenset[str] = frozenset(
    {"human_recording", "recorded_corpus", "recorded_background"}
)

#: Authorization methods this gate can actually verify. ``ssh-signature`` is
#: deliberately absent: see the module docstring for the exact Owner action that
#: adds it. Accepting an unverified signature field would be worse than the
#: governed equivalent, because it would read as cryptography.
_AUTHORIZATION_METHODS: frozenset[str] = frozenset({"repository-governed"})

#: Rounds 1-7 are retired synthetic experiments. A candidate declaring one of
#: them is either mislabelled or an attempt to ship retired work.
FIRST_HUMAN_ONLY_ROUND = 8

_CANDIDATE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: Written beside the shipped artifacts, so the record of what ships travels
#: with the thing that ships.
LEDGER_NAME = "PROMOTION_LEDGER.json"
JOURNAL_NAME = "PROMOTION_JOURNAL.json"
SUMS_NAME = "SHA256SUMS"
ROLLBACK_DIRNAME = "rollback"
_INCOMING_SUFFIX = ".incoming"
_PARTIAL_SUFFIX = ".partial"

_CHUNK = 1 << 20


class Refused(RuntimeError):
    """The promotion cannot proceed, and this says what stopped it."""


class LedgerError(Refused):
    """The destination's own record of what ships is missing or broken."""


# ── bytes ────────────────────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(body: object) -> bytes:
    """The bytes a record's identity is computed over.

    Sorted keys and no incidental whitespace, so the digest describes content
    and not formatting, and ``ensure_ascii=False`` so a non-ASCII name hashes
    identically under a different locale. Same rule as
    ``freeze_manifest._canonical``; a promotion record and a dataset manifest
    are both evidence and are both content-addressed the same way.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def record_digest(record: dict) -> str:
    """Digest of a promotion record's *evidence*, excluding its authorization.

    The whole ``authorization`` block is outside the digest, and it has to be: the
    Owner decision quotes this digest, the record then records the decision's own
    hash, and a digest that covered both could never be computed by either. A
    signature is not part of what it signs.

    So the two directions are guarded separately — change the evidence and the
    digest moves out from under the decision that quoted it; change the decision
    and its recorded hash stops matching.
    """
    body = copy.deepcopy(record)
    body.pop("authorization", None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def _write_text_durable(path: Path, text: str) -> None:
    """Write and fsync. A journal that is still in the page cache is not a journal."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json_durable(path: Path, payload: dict) -> None:
    _write_text_durable(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _copy_durable(source: Path, target: Path) -> None:
    """Copy bytes and fsync them, then confirm the copy by hash.

    A rollback artifact that was never flushed is not a rollback artifact, and a
    copy that silently truncated is worse than no copy at all.
    """
    with source.open("rb") as reader, target.open("wb") as writer:
        for chunk in iter(lambda: reader.read(_CHUNK), b""):
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())
    if sha256_file(target) != sha256_file(source):
        raise Refused(f"copying {source.name} to {target} did not reproduce its bytes")


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry where the platform allows it.

    Windows cannot open a directory for reading, and there is no portable
    equivalent; the journal is still fsynced as a file, so the loss is that a
    power cut could forget the *rename* rather than the record of it. Recovery
    handles both, because it decides from the bytes on disk.
    """
    try:
        handle = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def _replace(source: Path, target: Path) -> None:
    """One atomic replace.

    Wrapped in a function so a test can inject a crash *between* two of them,
    which is the failure atomicity has to survive.
    """
    os.replace(source, target)


def _sums_text(digests: dict[str, str]) -> str:
    """``SHA256SUMS`` in the format coreutils reads, artifacts in shipped order."""
    return "".join(f"{digests[name]}  {name}\n" for name in ARTIFACT_NAMES)


# ── the arithmetic the record claims ─────────────────────────────────────────


def binomial_upper_bound(events: int, n: int, alpha: float = ALPHA) -> float:
    """Exact one-sided upper confidence bound on a proportion (Clopper-Pearson).

    The smallest ``p`` at which observing this few events would be surprising:
    ``P(X <= events | n, p) = alpha``. With ``events == 0`` this collapses to
    ``1 - alpha ** (1/n)``, the closed form the predeclaration reasons in, and
    the rule of three is its first-order approximation. Solved by bisection on
    the binomial CDF so no special functions — and no scipy — are needed.
    """
    if n <= 0:
        raise ValueError("n must be positive; a bound over no trials is not a bound")
    if events < 0 or events > n:
        raise ValueError(f"events must be in [0, {n}], not {events}")
    if events == n:
        return 1.0
    if events == 0:
        return 1.0 - alpha ** (1.0 / n)

    def cdf(p: float) -> float:
        return sum(math.comb(n, k) * p**k * (1.0 - p) ** (n - k) for k in range(events + 1))

    low, high = 0.0, 1.0
    for _ in range(200):
        middle = (low + high) / 2.0
        if cdf(middle) > alpha:
            low = middle
        else:
            high = middle
    return high


def poisson_rate_upper_bound(events: int, hours: float, alpha: float = ALPHA) -> float:
    """Exact one-sided upper bound on an event rate per hour.

    Solves ``P(X <= events | lambda = rate * hours) = alpha``. With
    ``events == 0`` it is ``-ln(alpha) / hours``, which is why 0.2 per hour needs
    14.98 hours of negative audio: a clean run over 11.659 h bounds the rate at
    0.257/h and says nothing about 0.2.
    """
    if hours <= 0:
        raise ValueError("hours must be positive; a rate over no time is not a rate")
    if events < 0:
        raise ValueError(f"events must not be negative, got {events}")
    if events == 0:
        return -math.log(alpha) / hours

    def cdf(lam: float) -> float:
        return math.exp(-lam) * sum(lam**k / math.factorial(k) for k in range(events + 1))

    low, high = 0.0, 1.0
    while cdf(high) > alpha:
        high *= 2.0
        if high > 1e12:  # pragma: no cover - defensive
            raise ValueError("could not bracket the Poisson bound")
    for _ in range(200):
        middle = (low + high) / 2.0
        if cdf(middle) > alpha:
            low = middle
        else:
            high = middle
    return high / hours


def minimum_trials_for_proportion(limit: float, alpha: float = ALPHA) -> int:
    """Trials a clean run needs before it can bound a proportion at ``limit``."""
    if not 0 < limit < 1:
        raise ValueError(f"limit must be in (0, 1), got {limit}")
    return math.ceil(math.log(alpha) / math.log(1.0 - limit))


def minimum_hours_for_rate(limit: float, alpha: float = ALPHA) -> float:
    """Hours a clean run needs before it can bound a rate at ``limit`` per hour."""
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")
    return -math.log(alpha) / limit


def _bound_for(target: Target, events: int, denominator: float) -> float:
    if target.kind == "rate_per_hour":
        return poisson_rate_upper_bound(events, float(denominator))
    return binomial_upper_bound(events, int(denominator))


def _close(recomputed: float, stated: float) -> bool:
    return abs(recomputed - stated) <= max(_ABSOLUTE_TOLERANCE, abs(stated) * _TOLERANCE)


# ── git, which is what "repository-governed" means ───────────────────────────


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("git")
    if exe is None:
        raise Refused(
            "git is not on PATH. This gate's authorization, its CI binding and "
            "its 'what is promoted is what was reviewed' check are all "
            "statements about the repository, and none of them can be verified "
            "without it. Refusing rather than skipping them."
        )
    return subprocess.run(
        [exe, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _git_output(repo: Path, *args: str) -> str:
    done = _git(repo, *args)
    if done.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed in {repo.name}: {done.stderr.strip()}")
    return done.stdout


def _is_tracked(repo: Path, path: Path) -> bool:
    return _git(repo, "ls-files", "--error-unmatch", "--", str(path)).returncode == 0


def _is_clean(repo: Path, path: Path) -> bool:
    """Do the bytes on disk match the ones committed at HEAD?

    ``--untracked-files=no`` because untracked-ness is reported by
    ``_is_tracked`` with a better message; this question is only about drift.
    """
    done = _git(repo, "status", "--porcelain", "--untracked-files=no", "--", str(path))
    if done.returncode != 0:
        raise Refused(f"git status failed in {repo.name}: {done.stderr.strip()}")
    return not done.stdout.strip()


def head_commit(repo: Path) -> str:
    return _git_output(repo, "rev-parse", "HEAD").strip()


def _blob_of(repo: Path, path: Path) -> str | None:
    """The blob id the bytes on disk would be stored as."""
    done = _git(repo, "hash-object", str(path))
    return done.stdout.strip() if done.returncode == 0 else None


def _blob_at(repo: Path, commit: str, path: Path) -> str | None:
    """The blob id a commit's tree records for ``path``, or None if it has none."""
    try:
        relative = path.resolve().relative_to(repo).as_posix()
    except ValueError:
        return None
    done = _git(repo, "rev-parse", f"{commit}:{relative}")
    return done.stdout.strip() if done.returncode == 0 else None


def _commit_exists(repo: Path, commit: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{commit}^{{commit}}").returncode == 0


def _is_ancestor(repo: Path, commit: str, of: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", commit, of).returncode == 0


# ── the retired-artifact registry ────────────────────────────────────────────

#: The registry's schema, checked on read for the same reason
#: ``build_human_dataset.py`` checks it: a registry read under the wrong schema
#: verifies the wrong thing.
REGISTRY_SCHEMA_VERSION = 1


def retired_digests(registry: Path) -> dict[str, str]:
    """``sha256 -> "<kind> <name>"`` for every retired artifact.

    A missing, unreadable, wrongly versioned or empty registry is a refusal and
    not a pass. The whole point of content addressing is that it answers when
    asked; a registry that cannot answer has to stop the promotion, because the
    bytes it would have recognised are exactly the ones somebody would be
    tempted to ship.
    """
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Refused(
            f"{registry} could not be read as JSON ({exc}). A promotion cannot "
            "proceed without the registry that recognises retired synthetic "
            "bytes — including the pair this repository currently ships."
        ) from exc
    if payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise Refused(
            f"{registry.name} is schema_version {payload.get('schema_version')!r}; "
            f"this gate reads {REGISTRY_SCHEMA_VERSION}. Refusing to interpret it."
        )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise Refused(f"{registry.name} lists no artifacts, so it recognises nothing")
    found: dict[str, str] = {}
    for entry in artifacts:
        digest = str(entry.get("sha256", "")).lower()
        if not _SHA256.match(digest):
            raise Refused(f"{registry.name} contains a malformed sha256: {digest!r}")
        found[digest] = f"{entry.get('kind', 'retired')} {entry.get('name', '?')}"
    return found


# ── the CI contexts this repository actually produces ────────────────────────


def required_ci_contexts(workflow: Path) -> tuple[str, ...]:
    """Every check context the workflow produces, derived from the workflow.

    Not a hard-coded list. The authority for "CI is green" is the set of jobs
    that exist, and a list typed here would keep passing after a job was added —
    which is precisely the shape of a gate that stops covering what it claims
    to. Matrix legs are expanded the way GitHub names them, ``job (value)``, so
    a leg that never ran is a missing context rather than an invisible one.
    """
    try:
        import yaml  # noqa: PLC0415 - only this check needs it
    except ModuleNotFoundError as exc:  # pragma: no cover - environment
        raise Refused(
            "PyYAML is needed to read the CI workflow and learn which check "
            "contexts exist. Without it 'complete CI success' would mean "
            "whatever the record says it means."
        ) from exc
    try:
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise Refused(f"{workflow} could not be read as YAML: {exc}") from exc
    jobs = (document or {}).get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise Refused(f"{workflow.name} declares no jobs, so it proves nothing about CI")
    contexts: list[str] = []
    for name, job in jobs.items():
        legs = _matrix_legs(str(name), job if isinstance(job, dict) else {})
        if legs:
            contexts.extend(f"{name} ({leg})" for leg in legs)
        else:
            contexts.append(str(name))
    return tuple(sorted(contexts))


def _matrix_legs(job_name: str, job: dict) -> tuple[str, ...]:
    """The ``os`` legs of a job's matrix, including ``include:`` entries."""
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return ()
    axes = {key for key in matrix if key not in ("include", "exclude")}
    if axes - {"os"}:
        raise Refused(
            f"job {job_name!r} has a matrix over {sorted(axes)}; this gate only "
            "knows how to name the check contexts of an `os` matrix. Teach it "
            "the new axis rather than letting a leg go unchecked."
        )
    legs = [str(value) for value in matrix.get("os", []) or []]
    for extra in matrix.get("include", []) or []:
        if isinstance(extra, dict) and "os" in extra and str(extra["os"]) not in legs:
            legs.append(str(extra["os"]))
    return tuple(legs)


# ── the record, and the context a check runs in ──────────────────────────────


def load_record(path: Path) -> dict:
    """Read a promotion record, refusing one this gate does not understand."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Refused(f"{path} could not be read as JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise Refused(f"{path} is not a JSON object")
    return record


@dataclass
class Context:
    """Everything a check may look at. Read-only; nothing here writes."""

    record: dict
    record_path: Path
    candidate_dir: Path
    repo_root: Path
    dest: Path
    workflow: Path
    registry: Path
    _digests: dict[str, str] = field(default_factory=dict, repr=False)

    def digest(self, path: Path) -> str:
        """Hash a file once per run; the artifacts are megabytes and re-read."""
        key = str(path)
        if key not in self._digests:
            self._digests[key] = sha256_file(path)
        return self._digests[key]

    def section(self, name: str) -> dict:
        value = self.record.get(name)
        return value if isinstance(value, dict) else {}

    def candidate_artifact(self, name: str) -> Path:
        return self.candidate_dir / name

    def declared_artifact_digest(self, name: str) -> str:
        entry = self.section("artifacts").get(name)
        return str(entry.get("sha256", "")).lower() if isinstance(entry, dict) else ""

    def sources(self) -> list[dict]:
        value = self.record.get("sources")
        if not isinstance(value, list):
            return []
        return [entry for entry in value if isinstance(entry, dict)]

    def statistics_targets(self) -> dict[int, dict]:
        value = self.section("statistics").get("targets")
        if not isinstance(value, list):
            return {}
        return {
            entry["id"]: entry
            for entry in value
            if isinstance(entry, dict) and isinstance(entry.get("id"), int)
        }


def build_context(
    *,
    record_path: Path,
    candidate_dir: Path,
    dest: Path,
    repo_root: Path | None = None,
    workflow: Path | None = None,
    registry: Path | None = None,
) -> Context:
    root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    return Context(
        record=load_record(record_path),
        record_path=record_path.resolve(),
        candidate_dir=candidate_dir.resolve(),
        repo_root=root,
        dest=dest.resolve(),
        workflow=(workflow or root / ".github" / "workflows" / "youtab-ci.yml").resolve(),
        registry=(registry or root / "scripts" / "wakeword" / "retired_synthetic_artifacts.json"),
    )


# ── the checks ───────────────────────────────────────────────────────────────

_REQUIRED_SECTIONS: tuple[str, ...] = (
    "candidate",
    "artifacts",
    "acceptance",
    "parity",
    "statistics",
    "sources",
    "lineage",
    "ci",
    "authorization",
)


def _check_record_schema(ctx: Context) -> list[str]:
    problems: list[str] = []
    version = ctx.record.get("schema_version")
    if version != SCHEMA_VERSION:
        problems.append(
            f"record is schema_version {version!r}; this gate reads {SCHEMA_VERSION}. "
            "A record read under the wrong schema verifies the wrong thing."
        )
    for section in _REQUIRED_SECTIONS:
        if section not in ctx.record:
            problems.append(f"no {section!r} section; an absent section is not an empty one")
    return problems


def _check_candidate_identity(ctx: Context) -> list[str]:
    """Exactly which candidate, and at exactly which operating threshold."""
    problems: list[str] = []
    candidate = ctx.section("candidate")
    identifier = str(candidate.get("id", ""))
    if not _CANDIDATE_ID.match(identifier):
        problems.append(
            f"candidate.id is {candidate.get('id')!r}; a promotion nobody can name "
            "cannot be traced back to the run that produced it"
        )
    round_number = candidate.get("round")
    if not isinstance(round_number, int):
        problems.append(f"candidate.round is {round_number!r}, not an integer")
    elif round_number < FIRST_HUMAN_ONLY_ROUND:
        problems.append(
            f"candidate.round is {round_number}; rounds 1-{FIRST_HUMAN_ONLY_ROUND - 1} "
            "are retired synthetic experiments and none of them may ship"
        )
    commit = str(candidate.get("commit", ""))
    if not _COMMIT.match(commit):
        problems.append(
            f"candidate.commit is {candidate.get('commit')!r}, not a full 40-hex "
            "commit; an abbreviated one cannot be compared to a CI run"
        )
    threshold = candidate.get("threshold")
    if not isinstance(threshold, float):
        problems.append(
            f"candidate.threshold is {threshold!r}; the operating threshold is a "
            "float folded into the exported bias and has to be recorded as one"
        )
    elif not 0.0 < threshold < 1.0 or not math.isfinite(threshold):
        problems.append(f"candidate.threshold is {threshold!r}, which is not a probability")
    return problems


def _check_artifact_bytes(ctx: Context) -> list[str]:
    """The exact ONNX and TFLite hashes, recomputed from the bytes on disk."""
    problems: list[str] = []
    for name in ARTIFACT_NAMES:
        declared = ctx.declared_artifact_digest(name)
        entry = ctx.section("artifacts").get(name)
        if not isinstance(entry, dict):
            problems.append(f"artifacts has no entry for {name}")
            continue
        if not _SHA256.match(declared):
            problems.append(f"artifacts.{name}.sha256 is {entry.get('sha256')!r}, not a sha256")
        path = ctx.candidate_artifact(name)
        if not path.is_file():
            problems.append(f"{name} is not in the candidate directory, so nothing can be hashed")
            continue
        actual = ctx.digest(path)
        if declared and actual != declared:
            problems.append(f"{name} hashes to {actual}, the record says {declared}")
        size = path.stat().st_size
        if entry.get("bytes") != size:
            problems.append(f"artifacts.{name}.bytes is {entry.get('bytes')!r}, the file is {size}")
        if size == 0:
            problems.append(f"{name} is empty")
    return problems


def _check_measured_artifacts(ctx: Context) -> list[str]:
    """What is promoted has to be what was measured, byte for byte.

    Each backend's acceptance block names the artifact digest it was measured
    against and the threshold it was measured at. Both have to be the ones being
    promoted, or the numbers describe a different file.
    """
    problems: list[str] = []
    threshold = ctx.section("candidate").get("threshold")
    for backend, name in BACKEND_ARTIFACTS.items():
        block = ctx.section("acceptance").get(backend)
        if not isinstance(block, dict):
            problems.append(f"acceptance has no {backend!r} block, so nothing was measured on it")
            continue
        measured = str(block.get("artifact_sha256", "")).lower()
        path = ctx.candidate_artifact(name)
        promoted = ctx.digest(path) if path.is_file() else ""
        if not _SHA256.match(measured):
            problems.append(
                f"acceptance.{backend}.artifact_sha256 is {block.get('artifact_sha256')!r}; "
                "an acceptance result that does not name the bytes it measured is not evidence"
            )
        elif promoted and measured != promoted:
            problems.append(
                f"acceptance.{backend} was measured against {measured[:12]}… but "
                f"{name} being promoted is {promoted[:12]}…"
            )
        if block.get("threshold") != threshold:
            problems.append(
                f"acceptance.{backend}.threshold is {block.get('threshold')!r}, the "
                f"candidate ships at {threshold!r}; a result measured at another "
                "operating point is a result about another detector"
            )
    return problems


def _check_committed_artifacts(ctx: Context) -> list[str]:
    """Tracked, clean, and the same blobs the tested commit carried.

    Hashes prove the record and the file agree. They cannot prove the file was
    ever seen by anyone: an untracked artifact beside a correct record is a local
    file with a local hash. Three questions, and they fail differently — is it in
    the repository, does it still match the commit it is tracked at, and is it the
    same blob the commit CI went green on carried? The last one is what ties a
    green run to these bytes rather than to a filename.
    """
    problems: list[str] = []
    commit = str(ctx.section("candidate").get("commit", ""))
    for path in (*[ctx.candidate_artifact(name) for name in ARTIFACT_NAMES], ctx.record_path):
        if not path.is_file():
            problems.append(f"{path.name} does not exist, so it cannot be committed")
            continue
        if not _is_tracked(ctx.repo_root, path):
            problems.append(
                f"{path.name} is not tracked in git. An untracked candidate has "
                "been reviewed by nobody and measured by CI never."
            )
            continue
        if not _is_clean(ctx.repo_root, path):
            problems.append(
                f"{path.name} differs from the commit it is tracked at, so what "
                "would be promoted is not what was reviewed"
            )
    if not _COMMIT.match(commit):
        return problems  # candidate_identity reports it
    for name in ARTIFACT_NAMES:
        path = ctx.candidate_artifact(name)
        if not path.is_file():
            continue
        at_commit = _blob_at(ctx.repo_root, commit, path)
        here = _blob_of(ctx.repo_root, path)
        if at_commit is None:
            problems.append(
                f"{name} is not in the tree of {commit[:12]}…, the commit this "
                "candidate was measured and tested at"
            )
        elif here is not None and at_commit != here:
            problems.append(
                f"{name} is blob {here[:12]}… here and {at_commit[:12]}… at "
                f"{commit[:12]}…; the bytes CI saw are not the bytes on disk"
            )
    return problems


def _acceptance_failures(ctx: Context, backend: str) -> list[str]:
    """5/5 on one backend, recomputed rather than read off a boolean."""
    problems: list[str] = []
    block = ctx.section("acceptance").get(backend)
    if not isinstance(block, dict):
        return [f"acceptance.{backend} is missing; both backends are measured independently"]
    entries = block.get("targets")
    if not isinstance(entries, list):
        return [f"acceptance.{backend}.targets is {entries!r}, not a list of the five targets"]
    by_id = {entry["id"]: entry for entry in entries if isinstance(entry, dict) and "id" in entry}
    if set(by_id) != set(ACCEPTANCE_TARGETS):
        problems.append(
            f"acceptance.{backend} reports targets {sorted(by_id)}, not "
            f"{sorted(ACCEPTANCE_TARGETS)}; 5/5 means all five were measured"
        )
    for target_id, target in ACCEPTANCE_TARGETS.items():
        entry = by_id.get(target_id)
        if entry is None:
            problems.append(f"acceptance.{backend} target {target_id} ({target.name}) is missing")
            continue
        if entry.get("limit") != target.limit or entry.get("comparison") != target.comparison:
            problems.append(
                f"acceptance.{backend} target {target_id} declares "
                f"{entry.get('comparison')!r} {entry.get('limit')!r}; the target is "
                f"{target.comparison} {target.limit} and a record does not get to relax it"
            )
            continue
        observed = entry.get("observed")
        if not isinstance(observed, (int, float)) or isinstance(observed, bool):
            problems.append(
                f"acceptance.{backend} target {target_id} observed {observed!r}, "
                "which is not a measurement"
            )
            continue
        passed = observed <= target.limit if target.comparison == "<=" else observed == target.limit
        if entry.get("passed") is not passed:
            problems.append(
                f"acceptance.{backend} target {target_id} claims passed="
                f"{entry.get('passed')!r} for observed {observed!r} against "
                f"{target.comparison} {target.limit}; the claim and the arithmetic disagree"
            )
        if not passed:
            problems.append(
                f"acceptance.{backend} target {target_id} ({target.name}) failed: "
                f"observed {observed!r}, required {target.comparison} {target.limit}"
            )
    return problems


def _check_acceptance_onnx(ctx: Context) -> list[str]:
    return _acceptance_failures(ctx, "onnx")


def _check_acceptance_tflite(ctx: Context) -> list[str]:
    return _acceptance_failures(ctx, "tflite")


def _check_parity(ctx: Context) -> list[str]:
    """Zero detection disagreements, over a stated and cross-checked corpus.

    Numerical parity can look excellent while the two backends still split on a
    window sitting on the threshold, and that split is what a user experiences
    as "it wakes on my Mac but not on my PC".
    """
    problems: list[str] = []
    parity = ctx.section("parity")
    if parity.get("measured") is not True:
        problems.append(
            f"parity.measured is {parity.get('measured')!r}; parity that was not "
            "measured cannot be zero"
        )
    windows = parity.get("windows")
    if not isinstance(windows, int) or windows <= 0:
        problems.append(
            f"parity.windows is {windows!r}; zero disagreements over no windows is free"
        )
    disagreements = parity.get("detection_disagreements")
    if not isinstance(disagreements, int):
        problems.append(f"parity.detection_disagreements is {disagreements!r}, not a count")
    elif disagreements != 0:
        problems.append(
            f"parity.detection_disagreements is {disagreements}; the requirement is "
            "zero, and one disagreement is one user whose build does not wake"
        )
    # Cross-check against target 5, which is the same measurement counted in the
    # statistics block. Two numbers for one fact have to agree.
    entry = ctx.statistics_targets().get(5)
    if isinstance(entry, dict):
        if isinstance(windows, int) and entry.get("n") != windows:
            problems.append(
                f"statistics target 5 compares {entry.get('n')!r} windows, parity "
                f"reports {windows!r}"
            )
        events = entry.get("events")
        if isinstance(events, dict) and isinstance(disagreements, int):
            for backend in BACKEND_ARTIFACTS:
                if events.get(backend) != disagreements:
                    problems.append(
                        f"statistics target 5 events[{backend}] is "
                        f"{events.get(backend)!r}, parity reports {disagreements}"
                    )
    return problems


def _check_statistical_completeness(ctx: Context) -> list[str]:
    """Every target, with a denominator, in the predeclared unit.

    A point estimate is not evidence about a target. "0.000 per hour" with no
    hours behind it is a number that cannot be wrong, and every ``0.000/h`` in
    the frontier record was one observed event away from 0.086/h.
    """
    problems: list[str] = []
    statistics = ctx.section("statistics")
    alpha = statistics.get("alpha")
    if alpha != ALPHA:
        problems.append(
            f"statistics.alpha is {alpha!r}; every bound in the predeclaration is "
            f"one-sided {ALPHA}, and a record does not get to buy power by "
            "relaxing confidence"
        )
    entries = ctx.statistics_targets()
    if set(entries) != set(ACCEPTANCE_TARGETS):
        problems.append(
            f"statistics covers targets {sorted(entries)}, not {sorted(ACCEPTANCE_TARGETS)}"
        )
    for target_id, target in ACCEPTANCE_TARGETS.items():
        entry = entries.get(target_id)
        if entry is None:
            problems.append(f"statistics has no target {target_id} ({target.name})")
            continue
        denominator = entry.get(target.denominator)
        if not isinstance(denominator, (int, float)) or isinstance(denominator, bool):
            problems.append(
                f"statistics target {target_id} has no {target.denominator!r}: "
                f"{denominator!r}. Without a denominator this is a point estimate, "
                "and a point estimate bounds nothing."
            )
        elif denominator <= 0:
            problems.append(
                f"statistics target {target_id} {target.denominator}={denominator!r}"
            )
        events = entry.get("events")
        if not isinstance(events, dict) or set(events) != set(BACKEND_ARTIFACTS):
            problems.append(
                f"statistics target {target_id} events is {events!r}; it must count "
                f"events for each of {sorted(BACKEND_ARTIFACTS)} separately"
            )
        else:
            for backend, count in events.items():
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    problems.append(
                        f"statistics target {target_id} events[{backend}] is {count!r}, "
                        "not a count of observed events"
                    )
        unit = str(entry.get("unit", ""))
        if not unit.strip():
            problems.append(f"statistics target {target_id} does not say what it counted")
        elif target.unit_must_contain and target.unit_must_contain not in unit.lower():
            problems.append(
                f"statistics target {target_id} counts {unit!r}; the unit of evidence "
                f"is the {target.unit_must_contain}. Seven deterministic framings of "
                "one utterance are one trial, and counting them as seven inflates "
                "every bound below."
            )
        elif target.unit_must_contain == "utterance" and "window" in unit.lower():
            problems.append(
                f"statistics target {target_id} counts {unit!r}, which mixes windows "
                "into an utterance-level claim"
            )
    return problems


def _check_statistical_power(ctx: Context) -> list[str]:
    """The sample has to be able to bound the target it claims.

    This is the check that refuses a clean run on too little audio. Target 2 is
    0.2 activations per hour; a clean run needs 14.98 h before it bounds that at
    95%, and the eval partition's 11.659 h bounds only 0.257/h. Zero observed
    events over too little audio is not a pass, it is an absence of evidence.
    """
    problems: list[str] = []
    for target_id, target in ACCEPTANCE_TARGETS.items():
        entry = ctx.statistics_targets().get(target_id)
        if not isinstance(entry, dict):
            continue  # completeness reports it; nothing to compute here
        denominator = entry.get(target.denominator)
        events = entry.get("events")
        if not isinstance(denominator, (int, float)) or isinstance(denominator, bool):
            continue
        if denominator <= 0 or not isinstance(events, dict):
            continue
        for backend in sorted(BACKEND_ARTIFACTS):
            count = events.get(backend)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                continue
            if target.kind != "rate_per_hour" and count > denominator:
                problems.append(
                    f"statistics target {target_id} observed {count} events in "
                    f"{denominator} trials on {backend}"
                )
                continue
            bound = _bound_for(target, count, denominator)
            if target.limit > 0:
                if bound > target.limit:
                    needed = (
                        f"{minimum_hours_for_rate(target.limit):.2f} hours"
                        if target.kind == "rate_per_hour"
                        else f"{minimum_trials_for_proportion(target.limit)} trials"
                    )
                    problems.append(
                        f"statistics target {target_id} ({target.name}) is "
                        f"underpowered on {backend}: {count} event(s) in "
                        f"{denominator} {target.denominator_label} bounds the rate at "
                        f"{bound:.4g}, above the {target.limit} target. A clean run "
                        f"needs at least {needed}."
                    )
            elif count != 0:
                # Limit 0 is a count, not a rate: no finite sample bounds a rate
                # at zero, so the requirement is zero observed and a real
                # denominator, and the bound is reported rather than met.
                problems.append(
                    f"statistics target {target_id} ({target.name}) observed {count} "
                    f"event(s) on {backend}; the target is exactly zero"
                )
    return problems


def _check_statistical_consistency(ctx: Context) -> list[str]:
    """A recorded bound has to be the bound its own sample supports."""
    problems: list[str] = []
    for target_id, target in ACCEPTANCE_TARGETS.items():
        entry = ctx.statistics_targets().get(target_id)
        if not isinstance(entry, dict):
            continue
        denominator = entry.get(target.denominator)
        events = entry.get("events")
        stated = entry.get("bound")
        if not isinstance(denominator, (int, float)) or isinstance(denominator, bool):
            continue
        if denominator <= 0 or not isinstance(events, dict):
            continue
        if not isinstance(stated, dict) or set(stated) != set(BACKEND_ARTIFACTS):
            problems.append(
                f"statistics target {target_id} bound is {stated!r}; each backend's "
                "sample supports its own bound and both have to be stated"
            )
            continue
        for backend in sorted(BACKEND_ARTIFACTS):
            count = events.get(backend)
            claimed = stated.get(backend)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                continue
            if target.kind != "rate_per_hour" and count > denominator:
                continue
            if not isinstance(claimed, (int, float)) or isinstance(claimed, bool):
                problems.append(
                    f"statistics target {target_id} bound[{backend}] is {claimed!r}, "
                    "not a number"
                )
                continue
            recomputed = _bound_for(target, count, denominator)
            if not _close(recomputed, float(claimed)):
                problems.append(
                    f"statistics target {target_id} bound[{backend}] is {claimed!r}; "
                    f"{count} event(s) in {denominator} {target.denominator_label} gives "
                    f"{recomputed:.6g}"
                )
        # The acceptance figure and the statistics sample describe one
        # measurement. If they disagree, one of them is about another run.
        acceptance = ctx.section("acceptance")
        for backend in sorted(BACKEND_ARTIFACTS):
            block = acceptance.get(backend)
            if not isinstance(block, dict):
                continue
            targets = block.get("targets")
            if not isinstance(targets, list):
                continue
            reported = next(
                (
                    item
                    for item in targets
                    if isinstance(item, dict) and item.get("id") == target_id
                ),
                None,
            )
            count = events.get(backend)
            if reported is None or not isinstance(count, int) or isinstance(count, bool):
                continue
            observed = reported.get("observed")
            if not isinstance(observed, (int, float)) or isinstance(observed, bool):
                continue
            expected = count if target.kind == "count" else count / float(denominator)
            if not _close(expected, float(observed)):
                problems.append(
                    f"acceptance.{backend} target {target_id} observed {observed!r}, "
                    f"but statistics records {count} event(s) in {denominator} "
                    f"{target.denominator_label} = {expected:.6g}"
                )
    return problems


def _check_source_manifests(ctx: Context) -> list[str]:
    """Every dataset that fed the candidate, frozen and identified by digest."""
    problems: list[str] = []
    sources = ctx.sources()
    if not sources:
        problems.append(
            "sources is empty. A measurement that cannot be tied to inputs is a "
            "claim about bytes nobody can name."
        )
    seen: set[str] = set()
    roles: set[str] = set()
    for index, source in enumerate(sources):
        label = str(source.get("dataset", f"sources[{index}]"))
        if not str(source.get("dataset", "")).strip():
            problems.append(f"sources[{index}] has no dataset name")
        elif label in seen:
            problems.append(f"{label} appears in sources twice")
        seen.add(label)
        role = source.get("role")
        if role not in ROLE_USAGE:
            problems.append(f"{label} has role {role!r}, not one of {sorted(ROLE_USAGE)}")
        else:
            roles.add(role)
            if source.get("usage") != ROLE_USAGE[role]:
                problems.append(
                    f"{label} is role {role!r} but usage {source.get('usage')!r}; "
                    f"role {role!r} requires usage {ROLE_USAGE[role]!r}"
                )
        digest = str(source.get("manifest_sha256", "")).lower()
        if not _SHA256.match(digest):
            problems.append(
                f"{label} has manifest_sha256 {source.get('manifest_sha256')!r}. A "
                "recorded dataset is exactly as authoritative as its frozen "
                "manifest, and an unfrozen one cannot be re-verified later."
            )
    missing_roles = sorted(set(ROLE_USAGE) - roles)
    if sources and missing_roles:
        problems.append(
            f"no source with role {missing_roles}; a candidate promoted without a "
            "sealed measurement has only ever been measured on data it was tuned on"
        )
    return problems


def _check_licence_provenance(ctx: Context) -> list[str]:
    """A licence and a provenance record for every input, and consent for voices."""
    problems: list[str] = []
    for index, source in enumerate(ctx.sources()):
        label = str(source.get("dataset", f"sources[{index}]"))
        if not str(source.get("licence", "")).strip():
            problems.append(f"{label} has no licence recorded")
        if not str(source.get("provenance", "")).strip():
            problems.append(
                f"{label} has no provenance recorded — where the bytes came from is "
                "part of whether they may ship"
            )
        if source.get("redistributable") is not True:
            problems.append(
                f"{label} is not marked redistributable; a model fitted on material "
                "that cannot be redistributed is a model that cannot ship"
            )
        if source.get("kind") == "human_recording" and not str(
            source.get("consent_record", "")
        ).strip():
            problems.append(
                f"{label} is a human recording with no consent record referenced"
            )
    return problems


def _check_no_synthetic_lineage(ctx: Context) -> list[str]:
    """No synthetic input anywhere: not as data, not as an initialisation.

    Three independent things have to hold, because each defeats a different
    mistake: the source kinds are an allow-list (a synthesized corpus has no name
    to be spelled), the initialisation is declared fresh, and the dataset
    contracts the build stage wrote have to *measure* zero synthetic samples
    rather than assert it.
    """
    problems: list[str] = []
    for index, source in enumerate(ctx.sources()):
        label = str(source.get("dataset", f"sources[{index}]"))
        kind = source.get("kind")
        if kind not in RECORDED_SOURCE_KINDS:
            problems.append(
                f"{label} is kind {kind!r}; only {sorted(RECORDED_SOURCE_KINDS)} can "
                "feed a candidate, and synthetic material has no kind here to be named"
            )
        if source.get("synthetic") is not False:
            problems.append(
                f"{label} does not declare synthetic=false "
                f"(got {source.get('synthetic')!r})"
            )

    lineage = ctx.section("lineage")
    if lineage.get("initialised_from_synthetic_checkpoint") is not False:
        problems.append(
            "lineage.initialised_from_synthetic_checkpoint is "
            f"{lineage.get('initialised_from_synthetic_checkpoint')!r}; a candidate "
            "warm-started from a retired synthetic checkpoint carries synthetic "
            "training into what ships"
        )
    contracts = lineage.get("dataset_contracts")
    if not isinstance(contracts, list) or not contracts:
        return [
            *problems,
            "lineage.dataset_contracts is empty. `synthetic_samples: 0` has to come "
            "from the build stage that counted the emitted windows, not from this "
            "record restating it.",
        ]

    covered: set[str] = set()
    for entry in contracts:
        if not isinstance(entry, dict):
            problems.append(f"lineage.dataset_contracts entry {entry!r} is not an object")
            continue
        relative = str(entry.get("path", ""))
        declared = str(entry.get("sha256", "")).lower()
        if not relative or ".." in Path(relative).parts or Path(relative).is_absolute():
            problems.append(
                f"dataset contract path {relative!r} must be relative to the candidate "
                "directory and must not climb out of it"
            )
            continue
        path = ctx.candidate_dir / relative
        if not path.is_file():
            problems.append(f"dataset contract {relative} is not in the candidate directory")
            continue
        if not _SHA256.match(declared) or ctx.digest(path) != declared:
            problems.append(
                f"dataset contract {relative} hashes to {ctx.digest(path)}, the record "
                f"says {entry.get('sha256')!r}"
            )
            continue
        try:
            contract = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"dataset contract {relative} is not readable as JSON: {exc}")
            continue
        if contract.get("human_only") is not True or contract.get("synthetic_samples") != 0:
            problems.append(
                f"dataset contract {relative} does not assert a human-only dataset "
                f"(human_only={contract.get('human_only')!r}, "
                f"synthetic_samples={contract.get('synthetic_samples')!r})"
            )
        if contract.get("round", 0) < FIRST_HUMAN_ONLY_ROUND:
            problems.append(
                f"dataset contract {relative} is round {contract.get('round')!r}, "
                f"before the human-only phase began at {FIRST_HUMAN_ONLY_ROUND}"
            )
        declared_digests = {
            str(source.get("manifest_sha256", "")).lower() for source in ctx.sources()
        }
        for source in contract.get("sources", []) or []:
            if not isinstance(source, dict):
                continue
            digest = str(source.get("manifest_sha256", "")).lower()
            covered.add(digest)
            if digest and digest not in declared_digests:
                problems.append(
                    f"dataset contract {relative} was built from manifest "
                    f"{digest[:12]}…, which this record does not list as a source"
                )

    for source in ctx.sources():
        if source.get("role") not in ("train", "validation"):
            continue
        digest = str(source.get("manifest_sha256", "")).lower()
        if digest and digest not in covered:
            problems.append(
                f"{source.get('dataset')!r} is role {source.get('role')!r} but no "
                "dataset contract records having consumed it, so nothing measured "
                "that its windows were free of synthetic material"
            )
    return problems


def _check_retired_artifacts(ctx: Context) -> list[str]:
    """Content addressing, against everything the candidate is made of.

    Path rules and manifests are defeated by a copy: rename the file, move it,
    write a fresh manifest that hashes the copy correctly, and every name reads
    clean. A SHA-256 does not move with the file. This is also what refuses the
    pair this repository currently ships — both digests are in the registry as
    retired synthetic-era exports, so they cannot be re-promoted as if new.
    """
    digests: dict[str, str] = {}
    for name in ARTIFACT_NAMES:
        declared = ctx.declared_artifact_digest(name)
        if declared:
            digests[declared] = name
        path = ctx.candidate_artifact(name)
        if path.is_file():
            digests[ctx.digest(path)] = name
    lineage = ctx.section("lineage")
    checkpoint = str(lineage.get("checkpoint_sha256", "")).lower()
    if _SHA256.match(checkpoint):
        digests[checkpoint] = "the training checkpoint"
    for entry in lineage.get("dataset_contracts", []) or []:
        if isinstance(entry, dict):
            digest = str(entry.get("sha256", "")).lower()
            if _SHA256.match(digest):
                digests[digest] = f"dataset contract {entry.get('path')}"
    for source in ctx.sources():
        digest = str(source.get("manifest_sha256", "")).lower()
        if _SHA256.match(digest):
            digests[digest] = f"the frozen manifest of {source.get('dataset')!r}"

    retired = retired_digests(ctx.registry)  # a registry that cannot answer refuses
    return [
        f"{what} is {digest[:12]}…, which {ctx.registry.name} lists as retired "
        f"synthetic material ({retired[digest]}). Retired bytes are refused "
        "wherever they sit and whatever they are called."
        for digest, what in sorted(digests.items())
        if digest in retired
    ]


def _check_ci_success(ctx: Context) -> list[str]:
    """Complete CI success, on this exact commit, over every context that exists."""
    problems: list[str] = []
    ci = ctx.section("ci")
    candidate_commit = str(ctx.section("candidate").get("commit", ""))
    if str(ci.get("commit", "")) != candidate_commit or not candidate_commit:
        problems.append(
            f"ci.commit is {ci.get('commit')!r} and candidate.commit is "
            f"{candidate_commit!r}; a green run on another commit is a green run on "
            "another tree"
        )
    if ci.get("conclusion") != "success":
        problems.append(f"ci.conclusion is {ci.get('conclusion')!r}, not 'success'")
    if not str(ci.get("run_id", "")).strip():
        problems.append("ci.run_id is empty, so the run cannot be found again")
    contexts = ci.get("contexts")
    if not isinstance(contexts, dict) or not contexts:
        problems.append(f"ci.contexts is {contexts!r}; no context is not every context")
        contexts = {}
    required = required_ci_contexts(ctx.workflow)
    for name in required:
        if name not in contexts:
            problems.append(
                f"ci.contexts does not include {name!r}, which "
                f"{ctx.workflow.name} produces. A leg that never ran is not a leg "
                "that passed."
            )
    for name, conclusion in sorted(contexts.items()):
        if conclusion != "success":
            problems.append(f"ci context {name!r} concluded {conclusion!r}")

    if _COMMIT.match(candidate_commit):
        # The tested commit has to be *this* history, and it has to be behind or at
        # HEAD. Requiring equality would be circular — the record cites the run and
        # is then committed itself, which moves HEAD — so the binding to bytes is
        # made by `committed_artifacts` comparing blobs at that commit instead.
        if not _commit_exists(ctx.repo_root, candidate_commit):
            problems.append(
                f"{candidate_commit[:12]}… is not a commit in this repository, so no "
                "CI run on it can be checked against anything here"
            )
        else:
            head = head_commit(ctx.repo_root)
            if not _is_ancestor(ctx.repo_root, candidate_commit, head):
                problems.append(
                    f"the CI-green commit {candidate_commit[:12]}… is not an ancestor "
                    f"of HEAD ({head[:12]}…); promoting from an unrelated branch "
                    "ships bytes this history never tested"
                )
    return problems


def _check_authorization(ctx: Context) -> list[str]:
    """An Owner authorization over this record's exact digest.

    The governed equivalent of a signature: a tracked, hash-verified decision
    document that quotes the record digest, the candidate id and both artifact
    digests. Change one number in the evidence and the digest changes, which
    invalidates the decision, which can only be re-issued through a reviewed
    commit. See the module docstring for the exact Owner action that replaces
    this with a real detached signature.
    """
    problems: list[str] = []
    authorization = ctx.section("authorization")
    method = authorization.get("method")
    if method not in _AUTHORIZATION_METHODS:
        problems.append(
            f"authorization.method is {method!r}; this gate can verify "
            f"{sorted(_AUTHORIZATION_METHODS)}. 'ssh-signature' is refused rather "
            "than trusted until scripts/wakeword/PROMOTION_SIGNERS exists — see "
            "this module's docstring for the exact Owner action."
        )
    recomputed = record_digest(ctx.record)
    stated = str(authorization.get("record_sha256", "")).lower()
    if stated != recomputed:
        problems.append(
            f"authorization.record_sha256 is {authorization.get('record_sha256')!r}; "
            f"this record's canonical digest is {recomputed}. An authorization of "
            "different content authorizes a different promotion."
        )
    decision = authorization.get("owner_decision")
    if not isinstance(decision, dict):
        problems.append("authorization.owner_decision is missing; nobody authorized this")
        return problems
    relative = str(decision.get("path", ""))
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        problems.append(
            f"authorization.owner_decision.path is {relative!r}; it must be a path "
            "inside the repository that does not climb out of it"
        )
        return problems
    path = ctx.repo_root / relative
    if not path.is_file():
        problems.append(f"the Owner decision {relative} does not exist")
        return problems
    declared = str(decision.get("sha256", "")).lower()
    actual = ctx.digest(path)
    if not _SHA256.match(declared) or actual != declared:
        problems.append(
            f"the Owner decision {relative} hashes to {actual}, the record says "
            f"{decision.get('sha256')!r}"
        )
    if not _is_tracked(ctx.repo_root, path):
        problems.append(
            f"the Owner decision {relative} is not tracked in git. Branch protection "
            "is the signing authority here, and an untracked file has passed through "
            "no review at all."
        )
    elif not _is_clean(ctx.repo_root, path):
        problems.append(
            f"the Owner decision {relative} differs from the commit it is tracked at"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"the Owner decision {relative} could not be read: {exc}")
        return problems
    expected = {
        "the record digest": recomputed,
        "the candidate id": str(ctx.section("candidate").get("id", "")),
    }
    for name in ARTIFACT_NAMES:
        expected[f"the {name} digest"] = ctx.declared_artifact_digest(name)
    for what, needle in expected.items():
        if not needle or needle not in text:
            problems.append(
                f"the Owner decision {relative} does not name {what} ({needle!r}). A "
                "decision that does not quote what it authorizes authorizes anything."
            )
    return problems


def _check_destination_state(ctx: Context) -> list[str]:
    """The destination has to be the state its own record says it is.

    This is the check that catches the failure the whole module exists for: bytes
    that arrived in ``tools/wakewords/`` without going through a promotion. The
    ledger's head says which digests were last promoted, ``SHA256SUMS`` says
    which are supposed to be there, and both are compared against the files.
    """
    problems: list[str] = []
    dest = ctx.dest
    if not dest.is_dir():
        return [f"{dest} is not a directory; a promotion has nowhere to land"]
    if (dest / JOURNAL_NAME).exists():
        problems.append(
            f"{dest.name}/{JOURNAL_NAME} exists, so an earlier promotion was "
            "interrupted. Run `recover` first: promoting over an unresolved journal "
            "would mix two models."
        )
    shipped: dict[str, str] = {}
    for name in ARTIFACT_NAMES:
        path = dest / name
        if not path.is_file():
            problems.append(
                f"{name} is not in {dest.name}. This gate replaces a shipped model "
                "and preserves it as a rollback artifact; it does not populate an "
                "empty destination."
            )
            continue
        shipped[name] = ctx.digest(path)
    if len(shipped) == len(ARTIFACT_NAMES):
        for name, digest in shipped.items():
            if ctx.declared_artifact_digest(name) == digest:
                problems.append(
                    f"{name} already ships these exact bytes ({digest[:12]}…); there "
                    "is nothing to promote"
                )
    sums = dest / SUMS_NAME
    if sums.is_file() and len(shipped) == len(ARTIFACT_NAMES):
        expected = _sums_text(shipped)
        if sums.read_text(encoding="utf-8") != expected:
            problems.append(
                f"{dest.name}/{SUMS_NAME} does not match the bytes beside it. Either "
                "an artifact was replaced without a promotion or the sums were edited."
            )
    ledger_path = dest / LEDGER_NAME
    if ledger_path.exists():
        try:
            ledger = read_ledger(dest)
        except LedgerError as exc:
            problems.append(str(exc))
        else:
            head = ledger["entries"][-1] if ledger["entries"] else None
            if head and shipped and head.get("to") != shipped:
                problems.append(
                    f"{dest.name} ships {sorted(shipped.values())[0][:12]}… but the "
                    "ledger's last entry promoted different bytes. Something wrote "
                    "into the destination outside this gate."
                )
    return problems


#: Every required item, in the order a reader should meet them. Deleting one line
#: here deletes one requirement, which is exactly what the mutation tests in
#: ``tests/tools/test_wakeword_promotion_gate.py`` exploit: each of them removes
#: one entry and proves the suite goes red.
CHECKS: tuple[tuple[str, str, Callable[[Context], list[str]]], ...] = (
    ("record_schema", "a record this gate understands", _check_record_schema),
    ("candidate_identity", "which candidate, at which threshold", _check_candidate_identity),
    ("artifact_bytes", "the bytes hash to the recorded digests", _check_artifact_bytes),
    ("measured_artifacts", "what is promoted is what was measured", _check_measured_artifacts),
    ("committed_artifacts", "promoted bytes are reviewed bytes", _check_committed_artifacts),
    ("acceptance_onnx", "5/5 acceptance on the ONNX backend", _check_acceptance_onnx),
    ("acceptance_tflite", "5/5 acceptance on the TFLite backend", _check_acceptance_tflite),
    ("parity", "zero detection disagreements", _check_parity),
    ("statistical_completeness", "a denominator, in the unit", _check_statistical_completeness),
    ("statistical_power", "a sample that can bound the target", _check_statistical_power),
    ("statistical_consistency", "the bounds the sample supports", _check_statistical_consistency),
    ("source_manifests", "every dataset frozen and identified", _check_source_manifests),
    ("licence_provenance", "a licence and a provenance record", _check_licence_provenance),
    ("no_synthetic_lineage", "no synthetic data or initialisation", _check_no_synthetic_lineage),
    ("retired_artifacts", "no retired bytes in the candidate", _check_retired_artifacts),
    ("ci_success", "complete CI success on this commit", _check_ci_success),
    ("authorization", "an Owner authorization over this digest", _check_authorization),
    ("destination_state", "a destination nobody wrote into", _check_destination_state),
)


@dataclass(frozen=True)
class Verdict:
    """Per-check results. ``passed`` is all of them, never most of them."""

    results: tuple[tuple[str, tuple[str, ...]], ...]

    @property
    def passed(self) -> bool:
        return not any(failures for _, failures in self.results)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(
            f"{check_id}: {message}" for check_id, failures in self.results for message in failures
        )

    def failed_checks(self) -> tuple[str, ...]:
        return tuple(check_id for check_id, failures in self.results if failures)


def evaluate(ctx: Context) -> Verdict:
    """Run every check. Read-only: nothing here touches the destination.

    A check that raises ``Refused`` — a missing registry, no git, an unreadable
    workflow — becomes that check's failure rather than an exception, so one
    unavailable input cannot mask the state of the other seventeen.
    """
    results: list[tuple[str, tuple[str, ...]]] = []
    for check_id, _description, check in CHECKS:
        try:
            failures = tuple(check(ctx))
        except Refused as exc:
            failures = (str(exc),)
        results.append((check_id, failures))
    return Verdict(tuple(results))


# ── the ledger ───────────────────────────────────────────────────────────────


def _entry_digest(entry: dict) -> str:
    body = {key: value for key, value in entry.items() if key != "entry_sha256"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def read_ledger(dest: Path) -> dict:
    """Read the ledger and verify its hash chain.

    Each entry carries the digest of the one before it, so an entry cannot be
    rewritten or removed without breaking every entry after it. That chain is the
    other half of the governed-signing story: the authorization proves who said
    yes, the chain proves what has happened since.
    """
    path = dest / LEDGER_NAME
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"{path} could not be read as JSON: {exc}") from exc
    if ledger.get("schema_version") != SCHEMA_VERSION:
        raise LedgerError(
            f"{LEDGER_NAME} is schema_version {ledger.get('schema_version')!r}; "
            f"this gate reads {SCHEMA_VERSION}"
        )
    entries = ledger.get("entries")
    if not isinstance(entries, list):
        raise LedgerError(f"{LEDGER_NAME} has no entries list")
    previous = ""
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise LedgerError(f"{LEDGER_NAME} entry {index} is not an object")
        if entry.get("previous_sha256", "") != previous:
            raise LedgerError(
                f"{LEDGER_NAME} entry {index} follows {entry.get('previous_sha256')!r} "
                f"but the entry before it hashes to {previous!r}. The chain is broken, "
                "which means an entry was rewritten or removed."
            )
        recomputed = _entry_digest(entry)
        if entry.get("entry_sha256") != recomputed:
            raise LedgerError(
                f"{LEDGER_NAME} entry {index} records digest "
                f"{entry.get('entry_sha256')!r}, its content hashes to {recomputed}"
            )
        previous = recomputed
    return ledger


def _append_ledger(dest: Path, entry: dict) -> dict:
    ledger = (
        read_ledger(dest)
        if (dest / LEDGER_NAME).exists()
        else {"schema_version": SCHEMA_VERSION, "entries": []}
    )
    previous = ledger["entries"][-1]["entry_sha256"] if ledger["entries"] else ""
    sealed = {**entry, "previous_sha256": previous}
    sealed["entry_sha256"] = _entry_digest(sealed)
    ledger["entries"].append(sealed)
    _write_json_durable(dest / LEDGER_NAME, ledger)
    _fsync_dir(dest)
    return sealed


# ── atomicity: staging, journal, recovery ────────────────────────────────────


def _shipped_digests(dest: Path) -> dict[str, str]:
    return {
        name: sha256_file(dest / name) for name in ARTIFACT_NAMES if (dest / name).is_file()
    }


def _write_rollback_bundle(
    dest: Path, name: str, *, created: str, candidate: str, record_sha256: str
) -> Path:
    """Preserve what currently ships, verify the copy, then make it visible.

    Built under a ``.partial`` name and renamed, so a bundle directory that
    exists is a bundle that is complete. A rollback artifact nobody verified is a
    promise, and this is the one thing that has to be true when everything else
    has gone wrong.
    """
    final = dest / ROLLBACK_DIRNAME / name
    if final.exists():
        raise Refused(f"a rollback bundle already exists at {final}")
    staging = final.with_name(final.name + _PARTIAL_SUFFIX)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    preserved: dict[str, str] = {}
    for artifact in ARTIFACT_NAMES:
        source = dest / artifact
        if not source.is_file():
            raise Refused(f"{artifact} is not in {dest}, so it cannot be preserved")
        _copy_durable(source, staging / artifact)
        preserved[artifact] = sha256_file(staging / artifact)
    if (dest / SUMS_NAME).is_file():
        _copy_durable(dest / SUMS_NAME, staging / SUMS_NAME)
    _write_json_durable(
        staging / "ROLLBACK.json",
        {
            "schema_version": SCHEMA_VERSION,
            "created_utc": created,
            "preserved_from": preserved,
            "replaced_by_candidate": candidate,
            "promotion_record_sha256": record_sha256,
            "restore_with": "scripts/wakeword/promote_model.py rollback --dest <dest>",
        },
    )
    _fsync_dir(staging)
    os.rename(staging, final)
    _fsync_dir(final.parent)
    return final


def _stage(dest: Path, sources: dict[str, Path], sums: str) -> list[dict]:
    """Copy the new bytes in beside the old ones and describe the intended swap."""
    plan: list[dict] = []
    for name, source in sources.items():
        incoming = dest / (name + _INCOMING_SUFFIX)
        _copy_durable(source, incoming)
        plan.append(
            {"target": name, "incoming": incoming.name, "sha256": sha256_file(incoming)}
        )
    incoming_sums = dest / (SUMS_NAME + _INCOMING_SUFFIX)
    _write_text_durable(incoming_sums, sums)
    plan.append(
        {
            "target": SUMS_NAME,
            "incoming": incoming_sums.name,
            "sha256": sha256_file(incoming_sums),
        }
    )
    _fsync_dir(dest)
    return plan


def _apply(dest: Path, journal: dict) -> None:
    """Move every staged file onto its target, artifacts back to back."""
    for step in journal["plan"]:
        _replace(dest / step["incoming"], dest / step["target"])
    _fsync_dir(dest)


def recover(dest: Path) -> dict:
    """Resolve an interrupted promotion in one direction, and only one.

    Deterministic, and idempotent. For every target the journal names: if it
    already carries the new digest it is done; if a verified ``.incoming`` sits
    beside it, it can be completed. If all of them are in one of those two
    states the promotion rolls forward. If any is not — a staged file lost to the
    crash — the whole thing is restored from the rollback bundle. Never a mixture.
    """
    journal_path = dest / JOURNAL_NAME
    if not journal_path.exists():
        return {"action": "none", "state": "clean"}
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Refused(
            f"{journal_path} is unreadable ({exc}), so which model is supposed to be "
            "shipping cannot be determined. Restore both artifacts from "
            f"{dest / ROLLBACK_DIRNAME} by hand."
        ) from exc
    plan = journal.get("plan")
    if journal.get("schema_version") != SCHEMA_VERSION or not isinstance(plan, list) or not plan:
        raise Refused(f"{journal_path} does not describe a promotion this gate wrote")

    pending: list[dict] = []
    forward = True
    for step in plan:
        target = dest / str(step.get("target", ""))
        expected = str(step.get("sha256", ""))
        if target.is_file() and sha256_file(target) == expected:
            continue
        incoming = dest / str(step.get("incoming", ""))
        if incoming.is_file() and sha256_file(incoming) == expected:
            pending.append(step)
            continue
        forward = False
        break

    if forward:
        for step in pending:
            _replace(dest / step["incoming"], dest / step["target"])
        _fsync_dir(dest)
        entry = _append_ledger(
            dest,
            {
                "action": "recover-forward",
                "created_utc": _utc_now_text(),
                "candidate": journal.get("candidate"),
                "promotion_record_sha256": journal.get("promotion_record_sha256"),
                "rollback_bundle": journal.get("rollback_bundle"),
                "from": journal.get("from"),
                "to": journal.get("to"),
                "note": (
                    "an interrupted promotion was completed from its journal; every "
                    "staged file was present and hash-verified"
                ),
            },
        )
        journal_path.unlink()
        _fsync_dir(dest)
        return {
            "action": "recover-forward",
            "completed": [step["target"] for step in pending],
            "entry": entry,
        }

    bundle = dest / ROLLBACK_DIRNAME / str(journal.get("rollback_bundle", ""))
    previous = journal.get("from") or {}
    if not bundle.is_dir():
        raise Refused(
            f"the promotion cannot be completed and its rollback bundle {bundle} is "
            "missing, so nothing here can restore the previous model. Recover the "
            "bytes from git: git show <commit>:tools/wakewords/hey_youtab.onnx"
        )
    for name, digest in previous.items():
        copy_path = bundle / name
        if not copy_path.is_file() or sha256_file(copy_path) != digest:
            raise Refused(
                f"the rollback bundle's {name} is missing or does not hash to "
                f"{digest[:12]}…, so it cannot be trusted to restore the model"
            )
    for name in previous:
        staged = dest / (name + _PARTIAL_SUFFIX)
        _copy_durable(bundle / name, staged)
        _replace(staged, dest / name)
    if (bundle / SUMS_NAME).is_file():
        staged = dest / (SUMS_NAME + _PARTIAL_SUFFIX)
        _copy_durable(bundle / SUMS_NAME, staged)
        _replace(staged, dest / SUMS_NAME)
    for step in plan:
        incoming = dest / str(step.get("incoming", ""))
        if incoming.exists():
            incoming.unlink()
    entry = _append_ledger(
        dest,
        {
            "action": "recover-revert",
            "created_utc": _utc_now_text(),
            "candidate": journal.get("candidate"),
            "promotion_record_sha256": journal.get("promotion_record_sha256"),
            "rollback_bundle": journal.get("rollback_bundle"),
            "from": journal.get("to"),
            "to": previous,
            "note": (
                "an interrupted promotion could not be completed — a staged file was "
                "lost — so the preserved model was restored from its bundle"
            ),
        },
    )
    journal_path.unlink()
    _fsync_dir(dest)
    return {"action": "recover-revert", "restored": sorted(previous), "entry": entry}


def _utc_now_text(now: dt.datetime | None = None) -> str:
    moment = now or dt.datetime.now(dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bundle_name(created: str, candidate: str) -> str:
    return f"{created.replace(':', '')}__{candidate}"


def promote(ctx: Context, *, now: dt.datetime | None = None) -> dict:
    """Promote, having refused unless every check passed.

    The order is the whole design: recover any interrupted promotion, evaluate,
    preserve, stage, journal, swap, record. Nothing in the destination is touched
    until the verdict is unanimous.
    """
    recovered = recover(ctx.dest)
    verdict = evaluate(ctx)
    if not verdict.passed:
        raise Refused(
            "refusing to promote. "
            + f"{len(verdict.failed_checks())} check(s) failed: "
            + ", ".join(verdict.failed_checks())
        )

    created = _utc_now_text(now)
    candidate = str(ctx.section("candidate")["id"])
    digest = str(ctx.section("authorization")["record_sha256"])
    previous = _shipped_digests(ctx.dest)
    incoming = {name: ctx.candidate_artifact(name) for name in ARTIFACT_NAMES}
    new_digests = {name: ctx.digest(path) for name, path in incoming.items()}

    bundle = _write_rollback_bundle(
        ctx.dest,
        _bundle_name(created, candidate),
        created=created,
        candidate=candidate,
        record_sha256=digest,
    )
    plan = _stage(ctx.dest, incoming, _sums_text(new_digests))
    journal = {
        "schema_version": SCHEMA_VERSION,
        "created_utc": created,
        "candidate": candidate,
        "promotion_record_sha256": digest,
        "rollback_bundle": bundle.name,
        "from": previous,
        "to": new_digests,
        "plan": plan,
    }
    _write_json_durable(ctx.dest / JOURNAL_NAME, journal)
    _fsync_dir(ctx.dest)

    _apply(ctx.dest, journal)

    shipped = _shipped_digests(ctx.dest)
    if shipped != new_digests:
        raise Refused(  # pragma: no cover - defensive; recovery resolves this state
            f"after the swap {ctx.dest} holds {shipped}, expected {new_digests}. The "
            f"journal is still in place; run `recover --dest {ctx.dest}`."
        )
    entry = _append_ledger(
        ctx.dest,
        {
            "action": "promote",
            "created_utc": created,
            "candidate": candidate,
            "round": ctx.section("candidate").get("round"),
            "threshold": ctx.section("candidate").get("threshold"),
            "commit": ctx.section("candidate").get("commit"),
            "promotion_record": ctx.record_path.name,
            "promotion_record_sha256": digest,
            "owner_decision": ctx.section("authorization").get("owner_decision"),
            "rollback_bundle": bundle.name,
            "from": previous,
            "to": new_digests,
        },
    )
    (ctx.dest / JOURNAL_NAME).unlink()
    _fsync_dir(ctx.dest)
    return {
        "action": "promote",
        "recovered": recovered,
        "candidate": candidate,
        "from": previous,
        "to": new_digests,
        "rollback_bundle": str(bundle.relative_to(ctx.dest)),
        "entry": entry,
    }


def rollback(dest: Path, *, now: dt.datetime | None = None) -> dict:
    """Put the previously shipped bytes back, exactly.

    Reads the ledger head rather than guessing, verifies the bundle by hash
    before touching anything, and goes through the same journalled swap — a
    rollback that is not itself atomic is a second way to end up with a mixture.
    """
    recover(dest)
    ledger = read_ledger(dest)
    entries = ledger["entries"]
    if not entries:
        raise Refused(f"{dest / LEDGER_NAME} records no promotion to roll back")
    head = entries[-1]
    if head.get("action") not in ("promote", "recover-forward"):
        raise Refused(
            f"the last ledger entry is {head.get('action')!r}, so the shipped model is "
            "already the preserved one. Rolling back further would need the bundle "
            "from an earlier promotion, which is a deliberate choice and not a repeat "
            "of this command."
        )
    previous = head.get("from") or {}
    if set(previous) != set(ARTIFACT_NAMES):
        raise Refused(
            f"ledger entry {head.get('entry_sha256', '')[:12]}… preserved "
            f"{sorted(previous)}, not both artifacts"
        )
    bundle = dest / ROLLBACK_DIRNAME / str(head.get("rollback_bundle", ""))
    if not bundle.is_dir():
        raise Refused(
            f"the rollback bundle {bundle} is missing. The previous bytes are still in "
            "git history: git show <commit>:tools/wakewords/hey_youtab.onnx > "
            "hey_youtab.onnx, then verify against the ledger's recorded digest."
        )
    for name, digest in previous.items():
        source = bundle / name
        if not source.is_file() or sha256_file(source) != digest:
            raise Refused(
                f"{bundle.name}/{name} is missing or does not hash to {digest[:12]}…, "
                "so it is not the model that was preserved"
            )

    created = _utc_now_text(now)
    current = _shipped_digests(dest)
    plan = _stage(dest, {name: bundle / name for name in ARTIFACT_NAMES}, _sums_text(previous))
    journal = {
        "schema_version": SCHEMA_VERSION,
        "created_utc": created,
        "candidate": head.get("candidate"),
        "promotion_record_sha256": head.get("promotion_record_sha256"),
        "rollback_bundle": bundle.name,
        "from": current,
        "to": previous,
        "plan": plan,
    }
    _write_json_durable(dest / JOURNAL_NAME, journal)
    _fsync_dir(dest)
    _apply(dest, journal)
    shipped = _shipped_digests(dest)
    if shipped != previous:
        raise Refused(  # pragma: no cover - defensive
            f"after the rollback {dest} holds {shipped}, expected {previous}"
        )
    entry = _append_ledger(
        dest,
        {
            "action": "rollback",
            "created_utc": created,
            "candidate": head.get("candidate"),
            "promotion_record_sha256": head.get("promotion_record_sha256"),
            "rollback_bundle": bundle.name,
            "from": current,
            "to": previous,
            "note": "restored the model preserved by the promotion above",
        },
    )
    (dest / JOURNAL_NAME).unlink()
    _fsync_dir(dest)
    return {"action": "rollback", "from": current, "to": previous, "entry": entry}


def verify_shipped(dest: Path) -> dict:
    """Read-only: are the shipped bytes the ones the destination's record claims?

    Deliberately does not repair anything. A pending journal is reported as a
    failure, because a diagnostic that mutates cannot be used to find out what
    state a machine is in.
    """
    problems: list[str] = []
    shipped = _shipped_digests(dest)
    for name in ARTIFACT_NAMES:
        if name not in shipped:
            problems.append(f"{name} is missing from {dest}")
    if (dest / JOURNAL_NAME).exists():
        problems.append(
            f"{JOURNAL_NAME} is present: a promotion was interrupted and has not been "
            "resolved. Run `recover`."
        )
    sums = dest / SUMS_NAME
    if not sums.is_file():
        problems.append(f"{SUMS_NAME} is missing")
    elif len(shipped) == len(ARTIFACT_NAMES) and sums.read_text(encoding="utf-8") != _sums_text(
        shipped
    ):
        problems.append(f"{SUMS_NAME} does not describe the artifacts beside it")
    head: dict | None = None
    if (dest / LEDGER_NAME).exists():
        try:
            entries = read_ledger(dest)["entries"]
        except LedgerError as exc:
            problems.append(str(exc))
        else:
            head = entries[-1] if entries else None
            if head is None:
                problems.append(f"{LEDGER_NAME} is empty")
            elif head.get("to") != shipped:
                problems.append(
                    f"the ledger's last entry ({head.get('action')}) left "
                    f"{head.get('to')}, the destination holds {shipped}"
                )
    return {"shipped": shipped, "head": head, "problems": problems, "passed": not problems}


# ── command line ─────────────────────────────────────────────────────────────


def _add_candidate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--record", type=Path, required=True, help="the promotion record JSON")
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        required=True,
        help="directory holding hey_youtab.onnx, hey_youtab.tflite and the contracts",
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument("--workflow", type=Path, default=None, help="the CI workflow to read")
    parser.add_argument("--registry", type=Path, default=None, help="retired-artifact registry")


def _print_verdict(verdict: Verdict) -> None:
    width = max(len(check_id) for check_id, _, _ in CHECKS)
    for check_id, failures in verdict.results:
        print(f"  {'PASS' if not failures else 'FAIL'}  {check_id.ljust(width)}")
        for message in failures:
            print(f"          {message}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    checker = sub.add_parser("check", help="evaluate a candidate and write nothing")
    _add_candidate_arguments(checker)
    checker.add_argument("--dest", type=Path, required=True)

    promoter = sub.add_parser("promote", help="promote a candidate that passes every check")
    _add_candidate_arguments(promoter)
    promoter.add_argument("--dest", type=Path, required=True)

    roller = sub.add_parser("rollback", help="restore the model the last promotion preserved")
    roller.add_argument("--dest", type=Path, required=True)

    recoverer = sub.add_parser("recover", help="resolve an interrupted promotion")
    recoverer.add_argument("--dest", type=Path, required=True)

    verifier = sub.add_parser("verify", help="check the shipped bytes against the ledger")
    verifier.add_argument("--dest", type=Path, required=True)

    args = parser.parse_args(argv)

    try:
        if args.command in ("check", "promote"):
            ctx = build_context(
                record_path=args.record,
                candidate_dir=args.candidate_dir,
                dest=args.dest,
                repo_root=args.repo_root,
                workflow=args.workflow,
                registry=args.registry,
            )
            if args.command == "check":
                verdict = evaluate(ctx)
                _print_verdict(verdict)
                print("  PROMOTABLE" if verdict.passed else "  REFUSED")
                return 0 if verdict.passed else 1
            report = promote(ctx)
            print(f"promoted {report['candidate']}")
            for name, digest in sorted(report["to"].items()):
                print(f"  {name}  {digest}")
            print(f"  rollback artifact: {report['rollback_bundle']}")
            print(f"  ledger entry: {report['entry']['entry_sha256']}")
            return 0

        if args.command == "rollback":
            report = rollback(args.dest)
            print("rolled back to")
            for name, digest in sorted(report["to"].items()):
                print(f"  {name}  {digest}")
            return 0

        if args.command == "recover":
            report = recover(args.dest)
            print(f"recovery: {report['action']}")
            return 0

        report = verify_shipped(args.dest)
        for name, digest in sorted(report["shipped"].items()):
            print(f"  {name}  {digest}")
        for problem in report["problems"]:
            print(f"  {problem}")
        print("  PASS" if report["passed"] else "  FAIL")
        return 0 if report["passed"] else 1
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
