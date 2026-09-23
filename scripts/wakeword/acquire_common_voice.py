#!/usr/bin/env python3
"""Fetch a bounded governed slice of Common Voice as measured negatives.

Nothing here has downloaded anything. The corpus is gated: reaching it needs a
person to accept the dataset's terms under their own account and to mint a
read-scope token, and neither is something this repository can do or should
hold. ``--plan`` and ``--preflight`` therefore run with no credential and no
network — the first prints exactly what a fetch would do, the second checks
everything a fetch needs *except* the credential and says precisely what is
missing — and the fetch itself refuses to start without a token rather than
prompting for one.

Why the corpus at all, and why this many hours
---------------------------------------------
Acceptance target 2 is ≤ 0.2 recorded-speech false activations per hour. A
clean run is only evidence in proportion to how long it ran: observing zero
events in *n* hours bounds the rate at ``2.9957 / n`` at 95% (the Poisson form
of the rule of three), so ``0.2/h`` needs **at least 14.98 hours** of real
negative speech before a perfect run says anything at all. The consented
recording programme yields about 0.05 h of conversational negatives — a bound
of 60/h. Speech Commands' evaluation partition is 11.66 h of *single words* —
a bound of 0.257/h, still above target. This corpus is the only pool with the
hours, and 14.98 is where the number comes from: it is not a round figure and
not a preference.

``TARGET_HOURS`` is 20, which is the predeclared figure in
``round8_config.json`` and lands the demonstrable bound at 0.1498/h. The
margin is deliberate: clips are excluded after the download by rules that run
over the corpus's own metadata, so the acquired hours are known only at the
end, and a subset that lands at 14.9 h would demonstrate nothing.

Why this is a measurement corpus and not training data
------------------------------------------------------
A negative set the model was fitted on cannot also bound its false-activation
rate — the number it produces is a training-set number wearing an
acceptance-criterion's name. Since the *reason* this corpus exists is to make
target 2 demonstrable, it is frozen as ``usage="sealed-evaluation"``, which is
the marking ``freeze_manifest.assert_usable_for`` refuses for training and for
validation. ``assert_never_trainable`` re-checks that refusal against the
manifest this script just wrote, so the marking is a post-condition of the
acquisition rather than a sentence in a document.

Why the paths are an input rather than a constant
-------------------------------------------------
Every other external input is pinned by SHA-256 in ``assets.py``, established
by a human who fetched the bytes once. That is impossible here: nothing has
been fetched, so there is nothing to pin, and writing a plausible shard layout
into this file would be a guess dressed as a specification.

So the file list is supplied by the caller — one listing from the dataset's
file browser — and the *first* fetch establishes a lock file recording what it
received. Every later run verifies against that lock and refuses on mismatch.
This is trust-on-first-use. It is weaker than a pin, it is stated as such by
``--establish-lock`` being an explicit flag, and it is the strongest thing
available before the first byte arrives.

What the fetch is bounded by, structurally
------------------------------------------
Three separate limits, none of which is a default a caller can raise away:

* ``--max-bytes`` may not exceed ``ceiling_bytes(target_hours)`` — the hours
  asked for, at the corpus's MP3 bitrate, times a factor of two. A request
  above it is refused with the arithmetic.
* ``--target-hours`` may not exceed ``MAX_TARGET_HOURS``, so the ceiling
  cannot be inflated by asking for a thousand hours.
* Every stream is handed the *remaining* budget and aborts mid-download the
  moment it crosses it, deleting its partial file. A listing that names the
  full archive therefore stops at the budget instead of becoming tens of
  gigabytes.

Overlap exclusion, and what it does not claim
---------------------------------------------
Clips are excluded using only fields the corpus itself publishes in its own
TSVs: ``client_id``, ``path``, ``sentence`` (with ``sentence_id`` recorded when
present), ``locale``, and ``up_votes``/``down_votes``. Nothing here infers
speaker identity, computes a voice embedding, diarizes, or compares audio
across corpora — the dataset's terms of access forbid attempting to identify
speakers, and this file has no code path that could.

That means the exclusion is a metadata claim, and the honest statement of it is
in ``IDENTITY_NOT_CLAIMED``: ``client_id`` is a per-release pseudonym, so
disjointness from Speech Commands, from the consented speaker recordings, or
from another Common Voice release is **not** claimed and cannot be established
from published metadata.

See COMMON_VOICE.md for the derivation, the licence, the residual risk and the
one Owner action.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import freeze_manifest  # noqa: E402
import phrases  # noqa: E402

#: The dataset this plan is written against. English, and a specific corpus
#: release — "Common Voice" without a version is not a version.
REPO_ID = "mozilla-foundation/common_voice_17_0"
LANGUAGE = "en"
LICENSE = "CC0-1.0"

#: Hugging Face's documented file-resolution endpoint. The revision is part of
#: the path, which is what makes a pinned commit meaningful.
RESOLVE = "https://huggingface.co/datasets/{repo}/resolve/{revision}/{path}"

#: Both spellings the Hub's own tools accept, checked in this order.
TOKEN_VARS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")

# ── the bound, derived ───────────────────────────────────────────────────────

#: Acceptance target 2, verbatim: recorded-speech false activations per hour.
ACCEPTANCE_FA_PER_HOUR = 0.2

#: Poisson: zero events in an exposure of ``rate × hours`` is only 95%
#: evidence against a rate once ``rate × hours ≥ 2.9957``. This is the exact
#: constant behind the "rule of three" approximation, and it is what
#: ``round8_config.json`` predeclares for target 2.
POISSON_ZERO_EVENT_95 = 2.9957

#: Hours acquired, ≥ ``hours_required()`` with margin for post-download
#: exclusions. Predeclared in ``round8_config.json`` as 20.0 h → 0.1498/h.
TARGET_HOURS = 20.0

#: Refuse a target beyond this. Without it, ``ceiling_bytes`` scales with the
#: request and the byte bound could be raised by asking for more hours.
MAX_TARGET_HOURS = 40.0

#: Common Voice ships MP3. 32 kbit/s mono is the bitrate the size estimates
#: rest on; it is an assumption, stated as one, and the script records the
#: measured figures rather than these.
MP3_BITRATE_BITS_PER_SECOND = 32_000
MP3_BYTES_PER_HOUR = MP3_BITRATE_BITS_PER_SECOND // 8 * 3600

#: The one decoded format ``build_dataset.read_wav16`` accepts. Duplicated
#: rather than imported: that module needs numpy, scipy and pyroomacoustics,
#: and this one has to run on a machine that has none of them.
SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2
DECODED_BYTES_PER_HOUR = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH * 3600

#: How much slack the download budget may have over the physical minimum for
#: the hours: shards contain clips this subset will exclude, so some of what is
#: fetched is not kept. Two is generous and it is a *ceiling*, not a target.
BUDGET_HEADROOM = 2.0

#: Room for one source being staged and extracted, on top of the two trees.
STAGING_BYTES = 1 << 30

# ── the selection ────────────────────────────────────────────────────────────

#: The selection seed, frozen here and recorded in the selection record before
#: any candidate is scored. A seed chosen after looking at results is not a
#: seed, it is a filter; a seed that lives only in a command line cannot be
#: shown to have been chosen first. The value is arbitrary — 17.0 and 20 h read
#: out of it as a mnemonic and nothing more — and its only load-bearing
#: property is that it was fixed here, in advance, in a reviewable diff.
SELECTION_SEED = 170_020

#: The manifest marking. ``sealed-evaluation`` is what
#: ``freeze_manifest.assert_usable_for`` refuses for anything but measuring.
USAGE = "sealed-evaluation"
SPLIT = "evaluate"

#: Purposes the frozen manifest must refuse, checked after it is written.
#: "Hard-negative mining" is a training use and lands on ``training`` here;
#: there is no fourth purpose in ``freeze_manifest`` for it to hide in.
FORBIDDEN_PURPOSES = ("training", "validation")

#: The corpus's own TSV columns this script reads. Required ones must be in the
#: header or the eligibility rules cannot be applied and the run is refused;
#: optional ones are used when the release publishes them and recorded in the
#: report when they were. Nothing outside this tuple is read, and no field is
#: combined with any other to guess at a person.
REQUIRED_TSV_FIELDS = ("client_id", "path", "sentence")
OPTIONAL_TSV_FIELDS = ("locale", "up_votes", "down_votes", "sentence_id")

#: What the exclusion above does *not* establish. Printed by ``--plan``,
#: recorded in the report, and repeated in COMMON_VOICE.md, because an
#: exclusion rule that travels without its residual risk reads as a guarantee.
IDENTITY_NOT_CLAIMED = (
    "client_id is the corpus's own per-release pseudonym, not an identity: the "
    "same person re-registering gets a different one, so even within Common "
    "Voice the exclusion is best-effort rather than complete.",
    "No disjointness from Speech Commands, from the consented speaker "
    "recordings, or from any other corpus is claimed. Nothing published by "
    "either side could establish it, and establishing it by comparing voices "
    "is what the dataset's terms of access forbid.",
    "The wake-phrase exclusion reads the published transcript. A contributor "
    "who misread the prompt or ad-libbed is not visible in the text, so 'no "
    "clip contains the wake phrase' is a claim about the sentence field and "
    "not about the audio.",
    "Self-reported demographic fields (age, gender, accents) are optional in "
    "the corpus and are not read here, so no coverage or representativeness "
    "claim is made about the subset.",
)

#: Files in a listing, by what this script does with them. Anything else is
#: refused rather than fetched and ignored: a listing entry nobody consumes is
#: bytes spent against the budget for nothing.
METADATA_SUFFIXES = (".tsv",)
ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz")
CLIP_SUFFIX = ".mp3"

#: The decoder. Common Voice ships MP3 and the pipeline reads 16 kHz mono
#: 16-bit PCM only, so exactly one decode stands between them — and it happens
#: once, is verified, and is frozen.
DECODER_NAME = "ffmpeg"

#: Environment markers that mean "this is a shared runner". The clips are not
#: allowed anywhere a CI artifact or a public log could pick them up, and the
#: cheapest way to guarantee that is to refuse to acquire here at all.
CI_MARKERS = ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE", "JENKINS_URL")

#: Bumped when the state or selection record changes shape.
STATE_SCHEMA_VERSION = 1

#: How often the resume record is flushed, in clips. The state holds one entry
#: per clip, so writing it after every decode would be quadratic in the size of
#: the subset; this bounds the work an interruption throws away instead.
CHECKPOINT_CLIPS = 200

_REDACTED = "<redacted credential>"
_CHUNK = 1 << 20
_TIMEOUT = 600
_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")
_NON_WORD = re.compile(r"[^a-z0-9]+")


class CredentialRequired(RuntimeError):
    """The Owner-only step has not been done. Not an error to retry around."""


class Refused(RuntimeError):
    """A governed precondition the acquisition will not proceed without."""


class BoundExceeded(Refused):
    """A fetch would cross the governed byte bound."""


# ── the credential, which never leaves the environment ───────────────────────


def token_from_environment() -> str:
    """The read-scope token, or an explanation of exactly what is missing.

    Read from the environment and never stored, logged or echoed. The message
    below is the whole of what this repository knows how to say about
    credentials: what to do, and who has to do it.
    """
    for name in TOKEN_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise CredentialRequired(
        "No Hugging Face token in the environment, so nothing was fetched.\n"
        "\n"
        "This is an Owner action and cannot be automated:\n"
        f"  1. Sign in and accept the dataset terms at\n"
        f"     https://huggingface.co/datasets/{REPO_ID}\n"
        "     (the dataset is gated; a token alone does not open it)\n"
        "  2. Create an access token with READ scope only, and export it as\n"
        f"     {' or '.join(TOKEN_VARS)}\n"
        "\n"
        "Run with --preflight to check everything else with no credential, or "
        "--plan to see exactly what a fetch would do."
    )


def token_source() -> str | None:
    """Which variable holds a token, by name. Never the value."""
    for name in TOKEN_VARS:
        if os.environ.get(name, "").strip():
            return name
    return None


def _token_values() -> tuple[str, ...]:
    return tuple(value for name in TOKEN_VARS if (value := os.environ.get(name, "").strip()))


def scrub(text: str) -> str:
    """Replace any credential in ``text``, for anything about to be shown.

    Applied to every error this script prints. A token reaches a log line
    exactly once — through an exception message somebody forgot came from a
    library — and once is forever, because logs are copied into issues.
    """
    for value in _token_values():
        text = text.replace(value, _REDACTED)
    return text


def assert_no_credential(text: str, where: str) -> None:
    """Refuse to emit ``text`` if a credential survived into it.

    The backstop behind ``scrub``: scrubbing is a transformation and can be
    forgotten at a new call site, so every write and every print passes through
    here and fails loudly rather than leaking quietly. The message names the
    destination and never the value.
    """
    for value in _token_values():
        if value and value in text:
            raise Refused(
                f"refusing to write or print {where}: it contains the value of "
                f"a {'/'.join(TOKEN_VARS)} variable. The credential is an input "
                "to one Authorization header and nothing else."
            )


def _child_env() -> dict[str, str]:
    """The environment a subprocess gets: this one, minus the credential.

    The decoder has no use for a Hub token, and a child process that cannot see
    it cannot print it into a crash report.
    """
    return {name: value for name, value in os.environ.items() if name not in TOKEN_VARS}


# ── the derivation ───────────────────────────────────────────────────────────


def hours_required(rate: float | None = None) -> float:
    """Hours of recorded negative speech a clean run needs to bound ``rate``.

    Zero events over an exposure of ``rate × hours`` is 95% evidence only once
    that product reaches 2.9957. Read from the module constant rather than
    hard-coded so the acceptance rate has exactly one home.
    """
    rate = ACCEPTANCE_FA_PER_HOUR if rate is None else rate
    if rate <= 0:
        raise ValueError(f"a false-activation rate must be positive, not {rate!r}")
    return POISSON_ZERO_EVENT_95 / rate


def bound_per_hour(hours: float) -> float:
    """The rate a clean run over ``hours`` of speech actually bounds."""
    if hours <= 0:
        return float("inf")
    return POISSON_ZERO_EVENT_95 / hours


def ceiling_bytes(target_hours: float) -> int:
    """The most a governed acquisition of ``target_hours`` may download.

    Derived, not chosen: the hours asked for, at the corpus's stated MP3
    bitrate, times ``BUDGET_HEADROOM`` for the clips a shard carries that this
    subset excludes.
    """
    return int(target_hours * MP3_BYTES_PER_HOUR * BUDGET_HEADROOM)


def derivation(target_hours: float = TARGET_HOURS) -> dict:
    """Every number the bound rests on, and the arithmetic between them."""
    required = hours_required()
    return {
        "acceptance_target_fa_per_hour": ACCEPTANCE_FA_PER_HOUR,
        "poisson_zero_event_95": POISSON_ZERO_EVENT_95,
        "hours_required": round(required, 4),
        "how": (
            f"zero activations in n hours bounds the rate at {POISSON_ZERO_EVENT_95}/n "
            f"(the exact form of the 3/n rule of three), so demonstrating "
            f"{ACCEPTANCE_FA_PER_HOUR}/h needs n >= "
            f"{POISSON_ZERO_EVENT_95}/{ACCEPTANCE_FA_PER_HOUR} = {required:.2f} h"
        ),
        "target_hours": target_hours,
        "clean_run_bound_per_hour": round(bound_per_hour(target_hours), 4),
        "margin_hours": round(target_hours - required, 4),
        "why_margin": (
            "clips are excluded after download by rules over the corpus's own "
            "metadata, so the acquired hours are known only at the end; a "
            "subset that landed at 14.9 h would demonstrate nothing"
        ),
        "download_floor_bytes": int(target_hours * MP3_BYTES_PER_HOUR),
        "download_ceiling_bytes": ceiling_bytes(target_hours),
        "decoded_bytes": int(target_hours * DECODED_BYTES_PER_HOUR),
        "assumed_mp3_bitrate_bits_per_second": MP3_BITRATE_BITS_PER_SECOND,
    }


def assert_within_bound(max_bytes: int, target_hours: float) -> dict:
    """Refuse a request that is not a bounded governed subset.

    Both ends are checked, because both ends are failures. Too few hours and no
    clean run can demonstrate the target — the acquisition would spend
    bandwidth to produce an unusable measurement. Too many bytes and this stops
    being a subset: the whole point is that the full archive is unreachable by
    construction rather than by nobody having asked for it.
    """
    required = hours_required()
    if target_hours < required:
        raise Refused(
            f"--target-hours {target_hours} is below the {required:.2f} h a clean "
            f"run needs to bound {ACCEPTANCE_FA_PER_HOUR}/h at 95% "
            f"({POISSON_ZERO_EVENT_95} / {ACCEPTANCE_FA_PER_HOUR}). A shorter "
            "corpus cannot demonstrate acceptance target 2 no matter how the "
            "model behaves, so acquiring one is bandwidth spent on nothing."
        )
    if target_hours > MAX_TARGET_HOURS:
        raise Refused(
            f"--target-hours {target_hours} exceeds MAX_TARGET_HOURS "
            f"({MAX_TARGET_HOURS}). The byte ceiling is derived from the hours, "
            "so an unbounded hours request would be an unbounded download."
        )
    floor = int(target_hours * MP3_BYTES_PER_HOUR)
    ceiling = ceiling_bytes(target_hours)
    if max_bytes < floor:
        raise Refused(
            f"--max-bytes {max_bytes} cannot hold {target_hours} h: at "
            f"{MP3_BITRATE_BITS_PER_SECOND // 1000} kbit/s that is {floor} bytes "
            "of MP3 at the very least. The run would always end short."
        )
    if max_bytes > ceiling:
        raise BoundExceeded(
            f"--max-bytes {max_bytes} exceeds the governed ceiling {ceiling} "
            f"for {target_hours} h ({target_hours} h x {MP3_BYTES_PER_HOUR} B/h "
            f"x {BUDGET_HEADROOM} headroom). This is the refusal that makes the "
            "subset a subset: raising the budget is how an acquisition becomes "
            "the full archive, so it is not a raisable default."
        )
    return derivation(target_hours)


def assert_local_destination(out: Path) -> None:
    """The clips stay on local disk: not in git, not on a shared runner.

    Two refusals rather than one, because they fail differently. A destination
    inside the checkout is one ``git add -A`` from committing audio. A CI
    runner is a machine whose whole filesystem is a build artifact and whose
    stdout is a public log.
    """
    repo = Path(__file__).resolve().parents[2]
    resolved = out.resolve()
    if repo == resolved or repo in resolved.parents:
        raise Refused(
            f"refusing to acquire into the repository ({resolved.name}). The "
            "corpus is audio and audio never enters this checkout; point --out "
            "at the work directory beside the pipeline's other intermediates."
        )
    for marker in CI_MARKERS:
        if os.environ.get(marker, "").strip():
            raise Refused(
                f"{marker} is set, so this looks like a shared runner. The "
                "acquisition is a local, human-run step: a CI workspace is "
                "archived as an artifact and its output is a public log, and "
                "neither is a place for a speech corpus. Run it on the machine "
                "that holds the work directory."
            )


# ── the listing ──────────────────────────────────────────────────────────────


def read_file_list(path: Path) -> tuple[str, ...]:
    """Repository-relative paths to fetch, one per line, ``#`` for comments."""
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    paths = tuple(line for line in lines if line and not line.startswith("#"))
    if not paths:
        raise ValueError(f"{path.name} lists no files")
    seen: set[str] = set()
    for entry in paths:
        if entry.startswith("/") or ".." in entry.split("/"):
            raise ValueError(f"refusing a path that escapes the dataset: {entry!r}")
        if entry in seen:
            raise ValueError(f"{path.name} lists {entry!r} twice")
        seen.add(entry)
    return paths


def classify(files: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a listing into metadata TSVs and clip sources.

    An entry this script would not consume is refused rather than downloaded
    and skipped: bytes charged to the budget for a file nothing reads are the
    budget being spent on a typo.
    """
    metadata: list[str] = []
    sources: list[str] = []
    for entry in files:
        lowered = entry.lower()
        if lowered.endswith(METADATA_SUFFIXES):
            metadata.append(entry)
        elif lowered.endswith(ARCHIVE_SUFFIXES) or lowered.endswith(CLIP_SUFFIX):
            sources.append(entry)
        else:
            raise Refused(
                f"{entry!r} is neither corpus metadata ({', '.join(METADATA_SUFFIXES)}) "
                f"nor a clip source ({', '.join((*ARCHIVE_SUFFIXES, CLIP_SUFFIX))}). "
                "Nothing here would read it, so fetching it would spend the "
                "budget on a file the acquisition ignores."
            )
    if not metadata:
        raise Refused(
            "the listing names no .tsv. The eligibility rules read the corpus's "
            "own transcript metadata "
            f"({', '.join(REQUIRED_TSV_FIELDS)}), so a listing without it could "
            "only select clips blind — and 'no clip contains the wake phrase' "
            "would be an assumption instead of a check."
        )
    if not sources:
        raise Refused("the listing names no clip source; there would be nothing to acquire")
    return tuple(metadata), tuple(sources)


def _looks_like_a_commit(revision: str) -> bool:
    return bool(_HEX40.match(revision.lower()))


def resolved_url(repo: str, revision: str, path: str) -> str:
    """The exact URL a file is fetched from, recorded in the lock."""
    return RESOLVE.format(repo=repo, revision=revision, path=path)


# ── the fetch ────────────────────────────────────────────────────────────────


def fetch_one(
    repo: str,
    revision: str,
    path: str,
    into: Path,
    token: str,
    *,
    limit: int,
) -> Path:
    """Download one file, streaming it to disk, and return where it landed.

    Staged through ``.partial`` and renamed, so an interrupted fetch cannot
    leave a truncated file that hashes into the lock on the next run. Same
    reason ``assets.fetch`` does it.

    ``limit`` is the *remaining* byte budget and is enforced twice: against
    ``Content-Length`` before a body is read, and against the running total
    while it is. That second check is what makes the bound structural — a
    listing that names the full archive aborts at the budget with its partial
    deleted, rather than after tens of gigabytes.
    """
    target = into / Path(path).name
    if target.exists():
        return target
    if limit <= 0:
        raise BoundExceeded(f"no budget left to fetch {path}")
    url = resolved_url(repo, revision, path)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    partial = target.with_suffix(target.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise BoundExceeded(
                    f"{path} is {int(declared)} bytes and only {limit} of the "
                    "governed budget remain; not started"
                )
            written = 0
            with partial.open("wb") as handle:
                while chunk := response.read(_CHUNK):
                    written += len(chunk)
                    if written > limit:
                        raise BoundExceeded(
                            f"{path} crossed the remaining governed budget of "
                            f"{limit} bytes and was abandoned mid-stream"
                        )
                    handle.write(chunk)
    except urllib.error.HTTPError as exc:
        partial.unlink(missing_ok=True)
        # The token is never included in this message, and neither is the
        # header. 401 and 403 are the two outcomes that mean "the Owner step
        # is incomplete" rather than "the network is broken".
        if exc.code in (401, 403):
            raise CredentialRequired(
                f"{exc.code} from the Hub for {path}. The token is present but "
                f"not accepted. Either it lacks read scope, or the dataset "
                f"terms have not been accepted by the account that minted it: "
                f"https://huggingface.co/datasets/{repo}"
            ) from exc
        raise
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(target)
    return target


# ── the lock: trust on first use, enforced after ─────────────────────────────


def _read_json(path: Path) -> dict:
    """Parse one of this tool's own records, refusing an unreadable one.

    A corrupt lock or state file is a refusal rather than a traceback: it is the
    normal consequence of a machine losing power mid-write, and the operator
    needs to be told which file to look at.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise Refused(
            f"{path.name} is not readable JSON ({exc}). Nothing is inferred from "
            "a damaged record — inspect it, or delete it and re-establish."
        ) from exc
    if not isinstance(data, dict):
        raise Refused(f"{path.name} does not contain a record object")
    return data


def load_lock(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = _read_json(path)
    version = data.get("schema_version")
    if version != STATE_SCHEMA_VERSION:
        raise Refused(
            f"{path.name} is schema_version {version!r}; this tool writes and "
            f"reads {STATE_SCHEMA_VERSION}. A lock read under the wrong schema "
            "verifies the wrong thing."
        )
    files = data.get("files", {})
    if not isinstance(files, dict):
        raise Refused(f"{path.name} does not record a file table")
    return {str(key): dict(value) for key, value in files.items()}


def write_lock(path: Path, repo: str, revision: str, files: dict[str, dict]) -> str:
    """Every downloaded byte: its URL, its size, its digest and its licence."""
    return _write_json(
        path,
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "dataset": repo,
            "language": LANGUAGE,
            "revision": revision,
            "license": LICENSE,
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "files": {key: files[key] for key in sorted(files)},
        },
    )


def _write_json(path: Path, payload: dict) -> str:
    """Write JSON atomically, and never write a credential.

    ``os.replace`` through a temporary file: a state file half-written by an
    interrupted run is the one thing worse than no state file, because it reads
    as a record.
    """
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    assert_no_credential(text, path.name)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the seed, frozen before anything is scored ───────────────────────────────


def wake_phrase_texts() -> tuple[str, ...]:
    """The wake phrase's spellings, normalised for transcript matching.

    Taken from ``phrases.POSITIVE_SPELLINGS`` rather than written out here, so
    a phrase inventory that grows a spelling does not leave this exclusion
    matching the old one.
    """
    return tuple(sorted({_normalise(text) for text, _weight in phrases.POSITIVE_SPELLINGS}))


def _normalise(text: str) -> str:
    """Lower-cased words, space-delimited and space-padded.

    The padding is what makes a plain substring test a word-boundary test, so
    "hey youtab" matches "hey youtab please" and never "they heyyoutabbed".
    """
    return " " + " ".join(_NON_WORD.sub(" ", text.lower()).split()) + " "


def selection_key(seed: int, item: str) -> str:
    """A deterministic score for one candidate, from the frozen seed."""
    return hashlib.sha256(f"{seed}\x00{item}".encode()).hexdigest()


def ranked(items: list[str] | tuple[str, ...], seed: int) -> list[str]:
    """Candidates in seeded order. Ties broken by name, so it is total."""
    return sorted(items, key=lambda item: (selection_key(seed, item), item))


def selection_rules(exclude_ids: tuple[str, ...]) -> dict:
    """The eligibility rules, as data, so the record pins them too."""
    return {
        "metadata_fields_read": list(REQUIRED_TSV_FIELDS + OPTIONAL_TSV_FIELDS),
        "required_metadata_fields": list(REQUIRED_TSV_FIELDS),
        "locale_must_be": LANGUAGE,
        "wake_phrase_texts_excluded": list(wake_phrase_texts()),
        "near_miss_text_kept": (
            "deliberately. A negative corpus with the confusable phrases "
            "filtered out would measure a false-activation rate against speech "
            "chosen for being easy, which is the number acceptance is not "
            "asking for."
        ),
        "excluded_client_id_count": len(exclude_ids),
        "excluded_client_id_sha256": hashlib.sha256(
            "\n".join(sorted(exclude_ids)).encode("utf-8")
        ).hexdigest(),
        "identity_inference": (
            "none: no embedding, no diarization, no cross-corpus audio comparison"
        ),
    }


def freeze_selection(
    path: Path,
    *,
    repo: str,
    revision: str,
    seed: int,
    target_hours: float,
    listing: tuple[str, ...],
    exclude_ids: tuple[str, ...],
) -> dict:
    """Record the seed and the rules, or verify the record already there.

    Written **before** a single candidate is scored, and immutable afterwards
    except in one direction: the listing may be *extended*, because "list more
    shards and resume" is the documented remedy for a subset that landed short.
    A reordered or shortened listing is refused, since it would silently
    describe a different selection than the one that was frozen.
    """
    listing_digest = hashlib.sha256("\n".join(listing).encode("utf-8")).hexdigest()
    rules = selection_rules(exclude_ids)
    record = {
        "schema_version": STATE_SCHEMA_VERSION,
        "dataset": repo,
        "language": LANGUAGE,
        "license": LICENSE,
        "revision": revision,
        "selection_seed": seed,
        "target_hours": target_hours,
        "acceptance_fa_per_hour": ACCEPTANCE_FA_PER_HOUR,
        "hours_required": round(hours_required(), 4),
        "listing": list(listing),
        "listing_sha256": listing_digest,
        "rules": rules,
        "frozen_utc": _now(),
        "extensions": [],
    }
    if not path.exists():
        _write_json(path, record)
        return record

    existing = _read_json(path)
    if existing.get("schema_version") != STATE_SCHEMA_VERSION:
        raise Refused(f"{path.name} was written under another schema; refusing to interpret it")
    for field in ("dataset", "revision", "selection_seed", "target_hours", "rules"):
        if existing.get(field) != record[field]:
            raise Refused(
                f"{path.name} froze {field}={existing.get(field)!r} and this run "
                f"asks for {record[field]!r}. The selection was frozen before "
                "anything was scored, which is the whole of its value; a "
                "different one is a different corpus and belongs in a different "
                "work directory."
            )
    previous = list(existing.get("listing", []))
    if list(listing[: len(previous)]) != previous:
        raise Refused(
            f"{path.name} froze a listing of {len(previous)} files and this run "
            "supplies one that does not extend it. Appending shards is how a "
            "short subset is completed; reordering or dropping them changes "
            "which clips the seed selects."
        )
    if len(listing) > len(previous):
        existing["extensions"] = list(existing.get("extensions", [])) + [
            {
                "at_utc": _now(),
                "added": list(listing[len(previous) :]),
                "listing_sha256": listing_digest,
            }
        ]
        existing["listing"] = list(listing)
        existing["listing_sha256"] = listing_digest
        _write_json(path, existing)
    return existing


# ── the corpus's own metadata ────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    """One clip, as the corpus describes it. Nothing inferred, nothing joined."""

    clip: str
    client_id: str
    sentence: str
    locale: str
    up_votes: str
    down_votes: str
    sentence_id: str


def read_metadata(paths: list[Path]) -> tuple[dict[str, Candidate], tuple[str, ...]]:
    """Parse the corpus's transcript TSVs into candidates, keyed by clip name.

    ``QUOTE_NONE`` because Common Voice's TSVs are not quoted and a sentence
    containing a double quote would otherwise be re-parsed into a different
    sentence — and the sentence is what the wake-phrase exclusion reads.
    """
    candidates: dict[str, Candidate] = {}
    present: set[str] = set()
    for path in paths:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t", quoting=csv.QUOTE_NONE)
            header = tuple(reader.fieldnames or ())
            missing = [field for field in REQUIRED_TSV_FIELDS if field not in header]
            if missing:
                raise Refused(
                    f"{path.name} publishes {list(header)} and the eligibility "
                    f"rules need {missing}. Without those columns the selection "
                    "could not exclude on anything the corpus states, and the "
                    "exclusion would be an assumption."
                )
            present.update(field for field in OPTIONAL_TSV_FIELDS if field in header)
            for row in reader:
                clip = (row.get("path") or "").strip()
                if not clip:
                    continue
                candidates[Path(clip).name] = Candidate(
                    clip=Path(clip).name,
                    client_id=(row.get("client_id") or "").strip(),
                    sentence=(row.get("sentence") or "").strip(),
                    locale=(row.get("locale") or "").strip(),
                    up_votes=(row.get("up_votes") or "").strip(),
                    down_votes=(row.get("down_votes") or "").strip(),
                    sentence_id=(row.get("sentence_id") or "").strip(),
                )
    if not candidates:
        raise Refused("the corpus metadata lists no clips")
    return candidates, tuple(sorted(REQUIRED_TSV_FIELDS)) + tuple(sorted(present))


def ineligibility(
    clip: str,
    candidates: dict[str, Candidate],
    *,
    exclude_ids: frozenset[str],
    phrase_texts: tuple[str, ...],
) -> str | None:
    """Why this clip is not in the subset, or ``None`` if it is.

    A reason string rather than a boolean, so the report can say how many clips
    each rule removed. A filter nobody can count is a filter nobody can audit.
    """
    candidate = candidates.get(clip)
    if candidate is None:
        return "no metadata row"
    if candidate.locale and candidate.locale != LANGUAGE:
        return f"locale is {candidate.locale!r}, not {LANGUAGE!r}"
    if not candidate.sentence:
        return "no published transcript, so the wake phrase cannot be ruled out"
    sentence = _normalise(candidate.sentence)
    if any(text in sentence for text in phrase_texts):
        return "the published transcript contains the wake phrase"
    if candidate.client_id and candidate.client_id in exclude_ids:
        return "client_id is on the held-out list"
    if candidate.up_votes.isdigit() and candidate.down_votes.isdigit():
        if int(candidate.up_votes) <= int(candidate.down_votes):
            return "the corpus's own listeners did not validate it"
    return None


# ── the decode, once ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Decoder:
    """The one decoder this tree was decoded with, recorded by identity."""

    executable: str
    version: str

    @property
    def name(self) -> str:
        return Path(self.executable).name


def resolve_decoder(explicit: str | None = None) -> Decoder:
    """Find ffmpeg and record which one it is.

    The version is part of the record because a resample is a computation: the
    same MP3 through two decoders can differ in the last bit, and a tree half
    decoded by each is a dataset whose digests describe two things.
    """
    executable = explicit or shutil.which(DECODER_NAME)
    if not executable:
        raise Refused(
            f"no {DECODER_NAME} on PATH. Common Voice ships MP3 and this "
            "pipeline reads 16 kHz mono 16-bit PCM, so exactly one decode "
            f"stands between them. Install {DECODER_NAME} (apt install "
            f"{DECODER_NAME} / brew install {DECODER_NAME} / winget install "
            f"Gyan.FFmpeg) or pass --{DECODER_NAME} PATH."
        )
    probe = subprocess.run(  # noqa: S603
        [executable, "-version"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=False,
        env=_child_env(),
    )
    if probe.returncode != 0:
        raise Refused(f"{Path(executable).name} -version exited {probe.returncode}")
    first = (probe.stdout or probe.stderr).splitlines()
    if not first:
        raise Refused(f"{Path(executable).name} -version printed nothing")
    return Decoder(executable=executable, version=first[0].strip())


def decode_one(decoder: Decoder, source: Path, target: Path) -> None:
    """MP3 in, 16 kHz mono 16-bit PCM WAV out, once.

    Every flag is there for a reason. ``-map 0:a:0`` takes one audio stream
    rather than whatever the container leads with; ``-map_metadata -1`` and
    ``-fflags +bitexact`` keep the output free of tags and of the encoder
    string, so two decodes of one clip are the same bytes; ``-ac``/``-ar``/
    ``pcm_s16le`` are the format ``build_dataset.read_wav16`` will accept and
    no other. The credential is asserted absent from the command line, because
    ``argv`` on Linux is world-readable.
    """
    partial = target.with_name(target.name + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        decoder.executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(partial),
    ]
    for argument in argv:
        assert_no_credential(argument, "the decoder command line")
    try:
        result = subprocess.run(  # noqa: S603
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, env=_child_env()
        )
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if result.returncode != 0 or not partial.exists():
        lines = (result.stderr or "").strip().splitlines()
        partial.unlink(missing_ok=True)
        raise Refused(
            f"{decoder.name} failed on {source.name} (exit {result.returncode}): "
            + scrub(lines[-1] if lines else "no error output")
        )
    partial.replace(target)


def wav_seconds(path: Path) -> float:
    """Duration of a decoded clip, refusing anything the pipeline cannot read.

    Two checks, because they catch different failures. The header must state
    exactly the format ``read_wav16`` accepts — a 44.1 kHz stereo file would
    otherwise reach the feature extractor as a different signal. And the file
    must actually contain the audio its header declares, which is what catches
    a decode killed halfway: a truncated WAV reports its full length and reads
    as silence.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            frames = handle.getnframes()
    except (wave.Error, EOFError) as exc:
        raise Refused(
            f"{path.name} is not a readable WAV ({exc}). The decode produced "
            "something this pipeline cannot read, which is a finding about the "
            "decoder rather than a file to skip."
        ) from exc
    if (rate, channels, width) != (SAMPLE_RATE, CHANNELS, SAMPLE_WIDTH):
        raise Refused(
            f"{path.name} decoded to {rate} Hz / {channels} ch / {width * 8}-bit; "
            f"this pipeline reads {SAMPLE_RATE} Hz mono {SAMPLE_WIDTH * 8}-bit "
            "only, and a mismatch reaches the feature extractor as a different "
            "signal rather than as an error."
        )
    if frames <= 0:
        raise Refused(f"{path.name} decoded to no audio")
    declared = frames * channels * width
    if path.stat().st_size < declared:
        raise Refused(
            f"{path.name} declares {declared} bytes of audio and holds "
            f"{path.stat().st_size}. A truncated decode reads as silence, and "
            "silence in a negative corpus is hours of nothing counted as speech."
        )
    return frames / float(SAMPLE_RATE)


# ── clip sources: a tar of clips, or a clip ──────────────────────────────────


class ClipSource:
    """One fetched file, and the MP3 clips inside it.

    A tar is opened once and kept open for the whole selection pass: tarfile
    caches its member table, so extracting the seeded order costs one seek per
    clip instead of one full scan.
    """

    def __init__(self, dataset_path: str, local: Path) -> None:
        self.dataset_path = dataset_path
        self.local = local
        self._tar: tarfile.TarFile | None = None

    @property
    def is_archive(self) -> bool:
        return self.dataset_path.lower().endswith(ARCHIVE_SUFFIXES)

    def __enter__(self) -> ClipSource:
        if self.is_archive:
            try:
                self._tar = tarfile.open(self.local, "r:*")
            except tarfile.TarError as exc:
                raise Refused(
                    f"{self.dataset_path} does not read as a tar archive ({exc}). "
                    "Its bytes matched the lock, so this is what the corpus "
                    "published: the listing names a file that is not a shard."
                ) from exc
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._tar is not None:
            self._tar.close()
            self._tar = None

    def clips(self) -> dict[str, str]:
        """Clip basename -> member name (or the file itself), safely."""
        if self._tar is None:
            return {self.local.name: self.local.name}
        found: dict[str, str] = {}
        for member in self._tar.getmembers():
            if not member.isfile() or not member.name.lower().endswith(CLIP_SUFFIX):
                continue
            name = member.name.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise Refused(
                    f"{self.dataset_path}: refusing tar member {member.name!r}, "
                    "which would write outside the extraction directory"
                )
            found[Path(name).name] = member.name
        return found

    def stage(self, member: str, into: Path) -> tuple[Path, bool]:
        """Put one clip on disk. Returns the path and whether it is a copy."""
        if self._tar is None:
            return self.local, False
        target = into / Path(member).name
        partial = target.with_name(target.name + ".partial")
        partial.parent.mkdir(parents=True, exist_ok=True)
        extracted = self._tar.extractfile(member)
        if extracted is None:
            raise Refused(f"{self.dataset_path}: member {member!r} holds no data")
        try:
            with extracted, partial.open("wb") as handle:
                shutil.copyfileobj(extracted, handle, _CHUNK)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(target)
        return target, True


# ── the state, which is what makes a resume a resume ─────────────────────────


def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "complete": False,
            "downloads": {},
            "clips": {},
            "decoder": "",
        }
    state = _read_json(path)
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise Refused(
            f"{path.name} is schema_version {state.get('schema_version')!r}; this "
            f"tool writes and reads {STATE_SCHEMA_VERSION}"
        )
    for field in ("downloads", "clips"):
        if not isinstance(state.get(field), dict):
            raise Refused(f"{path.name} does not record a {field} table")
    return state


def assert_never_trainable(manifest: dict) -> None:
    """The frozen record must refuse every use that would fit a model on it.

    Checked against the manifest this run just wrote, rather than trusted from
    the ``usage=`` argument, because the guarantee is about what a *consumer*
    will find in the file. ``build_human_dataset.py`` calls the same
    ``assert_usable_for`` before it reads a clip, so a subset marked this way
    cannot be trained on or mined for hard negatives by the stage that would
    otherwise do it.
    """
    if manifest.get("usage") != USAGE:
        raise Refused(
            f"the frozen manifest records usage={manifest.get('usage')!r}, not "
            f"{USAGE!r}. This corpus exists to bound the false-activation rate; "
            "a set the model was fitted on cannot measure the model."
        )
    for purpose in FORBIDDEN_PURPOSES:
        try:
            freeze_manifest.assert_usable_for(manifest, purpose)
        except (freeze_manifest.SealedDatasetError, ValueError):
            continue
        raise Refused(
            f"the frozen manifest permits {purpose}, which it must never do. "
            "Hard-negative mining is a training use and lands on this check."
        )
    freeze_manifest.assert_usable_for(manifest, "evaluation")


# ── the acquisition ──────────────────────────────────────────────────────────


def acquire(
    out: Path,
    revision: str,
    files: tuple[str, ...],
    *,
    max_bytes: int,
    establish_lock: bool,
    repo: str = REPO_ID,
    target_hours: float = TARGET_HOURS,
    seed: int = SELECTION_SEED,
    exclude_client_ids: tuple[str, ...] = (),
    ffmpeg: str | None = None,
) -> dict:
    """Fetch, select, decode, verify and freeze — resumably, and in that order.

    The order is the design. Nothing is fetched before the destination, the
    revision, the bound, the credential and the decoder are settled; the seed
    is frozen before a candidate is scored; the manifest is written only when
    the subset is complete and long enough to bound the acceptance target, so
    an interrupted run leaves a resumable state and never a manifest that reads
    as a finished corpus.
    """
    assert_local_destination(out)
    if not _looks_like_a_commit(revision):
        raise ValueError(
            f"--revision must be a full 40-character commit sha, not {revision!r}. "
            "A branch name is not a version: the same name can resolve to "
            "different bytes tomorrow, and the measurement would not know."
        )
    bound = assert_within_bound(max_bytes, target_hours)
    metadata_paths, source_paths = classify(files)
    token = token_from_environment()
    decoder = resolve_decoder(ffmpeg)

    clips_dir = out / "clips"
    decoded_dir = out / "decoded"
    staging = out / "staging"
    for directory in (clips_dir, decoded_dir, staging):
        directory.mkdir(parents=True, exist_ok=True)

    stem = f"common_voice_{LANGUAGE}"
    lock_path = out / f"{stem}.lock.json"
    selection_path = out / f"{stem}.selection.json"
    state_path = out / f"{stem}.state.json"
    manifest_path = out / f"{stem}.manifest.json"

    # The seed is frozen here: before any candidate is read, let alone scored.
    selection = freeze_selection(
        selection_path,
        repo=repo,
        revision=revision,
        seed=seed,
        target_hours=target_hours,
        listing=files,
        exclude_ids=exclude_client_ids,
    )

    locked = load_lock(lock_path)
    if not locked and not establish_lock:
        raise Refused(
            f"no lock file at {lock_path.name} and --establish-lock was not "
            "given. The first fetch has nothing to verify against, which is a "
            "decision rather than a default: run it with --establish-lock and "
            "review the digests it prints."
        )

    state = load_state(state_path)
    if state.get("decoder") and state["decoder"] != decoder.version:
        raise Refused(
            f"{state_path.name} records {state['decoder']!r} and this machine has "
            f"{decoder.version!r}. Half a tree decoded by each is a dataset "
            "whose digests describe two computations. Finish it with the "
            "decoder it was started with, or decode a fresh tree."
        )
    if manifest_path.exists() and not state.get("complete"):
        raise Refused(
            f"{manifest_path.name} exists but {state_path.name} says the "
            "acquisition never finished. A manifest is a claim that a dataset "
            "is complete; refusing rather than trusting either file. Remove the "
            "manifest to resume, or the work directory to start over."
        )
    resumed = bool(state["downloads"] or state["clips"])
    state["decoder"] = decoder.version
    if not (state.get("complete") and manifest_path.exists()):
        # Written before the first byte: an acquisition killed at any point
        # after this leaves a record that says, explicitly, that it did not
        # finish. "No state file" and "a state file claiming completion" are the
        # two ways a partial acquisition reads as a whole one.
        #
        # A corpus that is *already* complete keeps its flag, so a re-run that
        # refuses over something else — a tampered file, a stray clip — leaves
        # the finished record finished instead of demoting it.
        state.update(complete=False, started_utc=_now(), updated_utc=_now())
        _write_json(state_path, state)

    downloads: dict[str, dict] = dict(state["downloads"])
    fetched_bytes = sum(int(entry.get("bytes", 0)) for entry in downloads.values())
    newly_fetched = 0

    verified: set[str] = set()

    def verify(dataset_path: str, local: Path) -> str:
        """Re-hash one downloaded file against both records that describe it.

        The lock is what a republished corpus contradicts; the state is what a
        half-finished run left behind. A resume that skipped the hash would be a
        resume that skipped the verification, which is the only reason the lock
        exists.
        """
        digest = freeze_manifest.sha256_file(local)
        _check_digest(dataset_path, digest, locked.get(dataset_path, {}).get("sha256"), lock_path)
        _check_digest(
            dataset_path, digest, downloads.get(dataset_path, {}).get("sha256"), state_path
        )
        verified.add(dataset_path)
        return digest

    def take(dataset_path: str) -> Path:
        """Ensure one dataset file is on disk, verified and locked."""
        nonlocal fetched_bytes, newly_fetched
        local = clips_dir / Path(dataset_path).name
        if dataset_path in verified and local.is_file():
            return local
        if not local.is_file():
            local = fetch_one(
                repo, revision, dataset_path, clips_dir, token, limit=max_bytes - fetched_bytes
            )
            newly_fetched += 1
        digest = verify(dataset_path, local)
        size = local.stat().st_size
        if dataset_path not in downloads:
            fetched_bytes += size
        downloads[dataset_path] = {
            "sha256": digest,
            "bytes": size,
            "url": resolved_url(repo, revision, dataset_path),
            "license": LICENSE,
        }
        if dataset_path not in locked:
            if not establish_lock:
                raise Refused(
                    f"{dataset_path} is not in {lock_path.name}, so there is "
                    "nothing to verify it against. Extending the listing means "
                    "trusting new bytes on first use, which is a decision: rerun "
                    "with --establish-lock and review the digests it records."
                )
            # Recorded as it arrives, not at the end: an acquisition
            # interrupted halfway through establishing its lock would otherwise
            # have to re-download everything it had already verified.
            locked[dataset_path] = downloads[dataset_path]
            write_lock(lock_path, repo, revision, locked)
        return local

    # Everything already on disk is re-verified before anything new is fetched,
    # not only the files this run happens to open. "Verified" has to cover the
    # whole subset, or a resume that stops early leaves unverified downloads
    # inside a record that says they were checked.
    for dataset_path in sorted(downloads):
        local = clips_dir / Path(dataset_path).name
        if local.is_file():
            verify(dataset_path, local)

    for dataset_path in metadata_paths:
        take(dataset_path)
    candidates, fields_used = read_metadata(
        [clips_dir / Path(path).name for path in metadata_paths]
    )

    phrase_texts = wake_phrase_texts()
    excluded_ids = frozenset(exclude_client_ids)
    target_seconds = target_hours * 3600.0
    seconds = 0.0
    excluded: dict[str, int] = {}
    decoded: dict[str, dict] = {}
    newly_decoded = 0

    # Already-decoded clips are re-verified, never re-decoded: the decode is
    # the expensive step and repeating it is how a "resume" becomes a rerun.
    for clip, record in sorted(state["clips"].items()):
        name = str(record.get("wav") or "")
        if not name or "seconds" not in record:
            raise Refused(
                f"{state_path.name} records clip {clip!r} without a decoded file "
                "and a duration. A resume record that cannot say what it already "
                "has is not a resume record."
            )
        wav = decoded_dir / name
        if not wav.is_file():
            continue
        digest = freeze_manifest.sha256_file(wav)
        if digest != record.get("wav_sha256"):
            raise Refused(
                f"{wav.name} no longer matches the digest recorded for it. The "
                "decoded bytes changed after they were verified; nothing "
                "downstream should run until somebody decides which set the "
                "measurement refers to."
            )
        decoded[clip] = dict(record)
        seconds += float(record["seconds"])

    budget_exhausted: str | None = None
    for dataset_path in ranked(source_paths, seed):
        if seconds >= target_seconds:
            break
        try:
            local = take(dataset_path)
        except BoundExceeded as exc:
            budget_exhausted = f"{dataset_path}: {exc}"
            break
        with ClipSource(dataset_path, local) as source:
            members = source.clips()
            for clip in ranked(tuple(members), seed):
                if seconds >= target_seconds:
                    break
                if clip in decoded:
                    continue
                reason = ineligibility(
                    clip, candidates, exclude_ids=excluded_ids, phrase_texts=phrase_texts
                )
                if reason:
                    excluded[reason] = excluded.get(reason, 0) + 1
                    continue
                wav = decoded_dir / (Path(clip).stem + ".wav")
                mp3, staged = source.stage(members[clip], staging)
                try:
                    mp3_digest = freeze_manifest.sha256_file(mp3)
                    # A clip already decoded is never decoded again — not even
                    # when the resume record was lost with the run that wrote
                    # it. ``decode_one`` stages through ``.partial``, so a WAV
                    # that exists is a WAV that finished, and ``wav_seconds``
                    # below re-checks its format and its length either way.
                    if not wav.is_file():
                        decode_one(decoder, mp3, wav)
                        newly_decoded += 1
                finally:
                    if staged:
                        mp3.unlink(missing_ok=True)
                clip_seconds = wav_seconds(wav)
                decoded[clip] = {
                    "wav": wav.name,
                    "wav_sha256": freeze_manifest.sha256_file(wav),
                    "mp3_sha256": mp3_digest,
                    "seconds": round(clip_seconds, 3),
                    "source": dataset_path,
                    "sentence_id": candidates[clip].sentence_id,
                }
                seconds += clip_seconds
                # Checkpointed rather than written per clip: the state file
                # holds one record per clip, so writing it every time would be
                # quadratic over a twenty-hour subset. Nothing is lost by the
                # interval — the decoded tree is the durable part, and both the
                # skip above and the reconciliation below read it directly.
                if len(decoded) % CHECKPOINT_CLIPS == 0:
                    state.update(downloads=downloads, clips=decoded, updated_utc=_now())
                    _write_json(state_path, state)

    # The manifest is frozen over the *tree*, so anything in the tree the state
    # does not know about would be frozen into the corpus while contributing no
    # hours — understating the exposure the bound is computed from. A clip this
    # selection would have taken is adopted, with its own duration measured; a
    # file it would not is a refusal that names the file, because "somebody
    # dropped audio into the decoded directory" must not be indistinguishable
    # from a governed acquisition.
    seconds += _reconcile_tree(
        decoded_dir,
        decoded,
        candidates=candidates,
        exclude_ids=excluded_ids,
        phrase_texts=phrase_texts,
    )

    hours = seconds / 3600.0
    state.update(
        downloads=downloads,
        clips=decoded,
        hours=round(hours, 4),
        bytes_fetched=fetched_bytes,
        selection_sha256=hashlib.sha256(
            json.dumps(selection, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        complete=False,
        updated_utc=_now(),
    )
    _write_json(state_path, state)

    required = hours_required()
    if hours < required:
        raise Refused(
            f"acquired {hours:.4f} h, which is below the {required:.2f} h a clean "
            f"run needs to bound {ACCEPTANCE_FA_PER_HOUR}/h "
            f"({POISSON_ZERO_EVENT_95} / {ACCEPTANCE_FA_PER_HOUR}). Nothing was "
            "frozen: a manifest over a short corpus would read as a governed "
            "measurement set that cannot measure the thing it exists for.\n"
            f"  downloaded {fetched_bytes} of {max_bytes} budgeted bytes"
            + (f"\n  budget exhausted at {budget_exhausted}" if budget_exhausted else "")
            + f"\n  {sum(excluded.values())} candidates excluded by the corpus's "
            "own metadata\n"
            "Append more shards to the file list and run again — the state file "
            "makes that a resume, not a restart."
        )

    manifest = freeze_manifest.freeze(
        decoded_dir,
        manifest_path,
        dataset=f"{repo}@{revision[:12]}",
        split=SPLIT,
        # The marking that makes this corpus unusable for fitting anything.
        # ``assert_never_trainable`` below checks the written record, not this
        # argument.
        usage=USAGE,
        note=(
            f"Common Voice {LANGUAGE} {revision[:12]}, {LICENSE}; recorded "
            f"negatives only, decoded once by {decoder.version}; "
            f"{hours:.3f} h bounds a clean run at {bound_per_hour(hours):.4f}/h"
        ),
        allow_in_repo=False,
    )
    assert_never_trainable(manifest)

    state.update(complete=True, manifest_sha256=manifest["manifest_sha256"], updated_utc=_now())
    _write_json(state_path, state)
    shutil.rmtree(staging, ignore_errors=True)

    report = {
        "dataset": manifest["dataset"],
        "revision": revision,
        "license": LICENSE,
        "usage": manifest["usage"],
        "split": manifest["split"],
        "selection_seed": seed,
        "hours_acquired": round(hours, 4),
        "hours_required": round(required, 4),
        "clean_run_bound_per_hour": round(bound_per_hour(hours), 4),
        "bound": bound,
        "files_fetched": len(downloads),
        "files_fetched_this_run": newly_fetched,
        "bytes_fetched": fetched_bytes,
        "max_bytes": max_bytes,
        "clips_selected": len(decoded),
        "clips_decoded_this_run": newly_decoded,
        "clips_excluded": dict(sorted(excluded.items())),
        "metadata_fields_used": list(fields_used),
        "identity_not_claimed": list(IDENTITY_NOT_CLAIMED),
        "decoder": decoder.version,
        "decoded_bytes": manifest["total_bytes"],
        "manifest_sha256": manifest["manifest_sha256"],
        "resumed": resumed,
        "produced": [lock_path.name, selection_path.name, state_path.name, manifest_path.name],
    }
    if budget_exhausted:
        report["budget_exhausted_on"] = budget_exhausted
    return report


def _reconcile_tree(
    decoded_dir: Path,
    decoded: dict[str, dict],
    *,
    candidates: dict[str, Candidate],
    exclude_ids: frozenset[str],
    phrase_texts: tuple[str, ...],
) -> float:
    """Make the decoded tree and the resume record agree. Returns added seconds.

    Runs between the last decode and the freeze, because those are the two
    things that can disagree: a run interrupted between a decode and a
    checkpoint leaves a clip on disk that the record does not list, and
    ``freeze_manifest`` hashes what is on disk.
    """
    added = 0.0
    known = {record["wav"] for record in decoded.values()}
    for path in sorted(decoded_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.name.endswith(".partial"):
            # A decode that was killed before its rename. Removed rather than
            # refused: it is by definition incomplete, and leaving it would put
            # a truncated file into the frozen manifest.
            path.unlink(missing_ok=True)
            continue
        if path.suffix.lower() != ".wav":
            raise Refused(
                f"{path.name} is in the decoded tree and is not a WAV. The "
                "manifest is frozen over this directory, so anything here "
                "becomes part of the corpus; the tree holds decoded clips only."
            )
        if path.name in known:
            continue
        clip = path.stem + CLIP_SUFFIX
        reason = ineligibility(
            clip, candidates, exclude_ids=exclude_ids, phrase_texts=phrase_texts
        )
        if reason:
            raise Refused(
                f"{path.name} is in the decoded tree but this selection would "
                f"not have taken it ({reason}). Refusing to freeze it into the "
                "corpus: a measurement set has to be the set the rules chose."
            )
        seconds = wav_seconds(path)
        decoded[clip] = {
            "wav": path.name,
            "wav_sha256": freeze_manifest.sha256_file(path),
            "mp3_sha256": "",
            "seconds": round(seconds, 3),
            "source": "adopted: decoded before an interruption, re-verified here",
            "sentence_id": candidates[clip].sentence_id,
        }
        added += seconds
    return added


def _check_digest(dataset_path: str, digest: str, expected: str | None, lock_path: Path) -> None:
    if expected and expected != digest:
        raise Refused(
            f"{dataset_path}: SHA-256 mismatch against {lock_path.name}\n"
            f"  expected {expected}\n"
            f"  got      {digest}\n"
            "The published bytes changed. Nothing downstream should run "
            "until somebody decides which set the measurements refer to."
        )


# ── preflight: everything a fetch needs except the credential ────────────────


def preflight(
    out: Path | None,
    *,
    revision: str | None = None,
    file_list: Path | None = None,
    target_hours: float = TARGET_HOURS,
    max_bytes: int | None = None,
    ffmpeg: str | None = None,
) -> dict:
    """Check the machine, compute the disk cost, fetch nothing, create nothing.

    Deliberately total: it reports every problem it can see rather than the
    first, because the point is to hand the Owner one list. The credential is
    the only item it cannot resolve itself, and with no token present it says
    so precisely — which variable to set, and what to accept first.
    """
    missing: list[str] = []
    report: dict = {
        "mode": "preflight",
        "dataset": REPO_ID,
        "language": LANGUAGE,
        "license": LICENSE,
        "derivation": derivation(target_hours),
        "selection_seed": SELECTION_SEED,
        "usage": USAGE,
        "split": SPLIT,
    }

    source = token_source()
    report["credential"] = {
        "present": source is not None,
        "variable": source,
        "value_read": "never printed, never written, never passed as an argument",
    }
    if source is None:
        missing.append(
            f"a read-scope Hugging Face token in {' or '.join(TOKEN_VARS)} "
            f"(Owner action: accept the gated dataset terms at "
            f"https://huggingface.co/datasets/{REPO_ID} while signed in, then "
            f"mint a READ-scope token and export it)"
        )

    try:
        decoder = resolve_decoder(ffmpeg)
        report["decoder"] = {"present": True, "name": decoder.name, "version": decoder.version}
    except Refused as exc:
        report["decoder"] = {"present": False, "name": DECODER_NAME, "version": None}
        missing.append(scrub(str(exc)))

    budget = ceiling_bytes(target_hours) if max_bytes is None else max_bytes
    try:
        assert_within_bound(budget, target_hours)
        report["budget"] = {
            "max_bytes": budget,
            "floor_bytes": int(target_hours * MP3_BYTES_PER_HOUR),
            "ceiling_bytes": ceiling_bytes(target_hours),
            "within_bound": True,
        }
    except Refused as exc:
        report["budget"] = {"max_bytes": budget, "within_bound": False}
        missing.append(scrub(str(exc)))

    if revision is not None and not _looks_like_a_commit(revision):
        missing.append(
            f"--revision {revision!r} is not a full 40-character commit sha; a "
            "branch name is not a version"
        )
    report["revision"] = revision

    if file_list is not None:
        try:
            listing = read_file_list(file_list)
            metadata, sources = classify(listing)
            report["listing"] = {
                "files": len(listing),
                "metadata": len(metadata),
                "clip_sources": len(sources),
                "sha256": hashlib.sha256("\n".join(listing).encode("utf-8")).hexdigest(),
            }
        except (Refused, ValueError, OSError) as exc:
            report["listing"] = None
            missing.append(scrub(str(exc)))
    else:
        report["listing"] = None
        missing.append("--file-list: one dataset-relative path per line, from the file browser")

    report["destination"] = _preflight_destination(out, missing)
    report["disk"] = _preflight_disk(out, target_hours, max_bytes, missing)
    report["resume"] = _preflight_resume(out)
    report["identity_not_claimed"] = list(IDENTITY_NOT_CLAIMED)
    report["missing"] = missing
    report["ready"] = not missing
    report["blocked_on_owner_only"] = bool(missing) and all(
        item.startswith("a read-scope Hugging Face token") for item in missing
    )
    return report


def _preflight_destination(out: Path | None, missing: list[str]) -> dict:
    if out is None:
        missing.append("--out: the work directory the corpus lives in, outside the repository")
        return {"given": False}
    record: dict = {"given": True, "name": out.name, "exists": out.exists()}
    try:
        assert_local_destination(out)
        record["local_only"] = True
    except Refused as exc:
        record["local_only"] = False
        missing.append(scrub(str(exc)))
    return record


def _preflight_disk(
    out: Path | None, target_hours: float, max_bytes: int | None, missing: list[str]
) -> dict:
    downloads = ceiling_bytes(target_hours) if max_bytes is None else max_bytes
    decoded = int(target_hours * DECODED_BYTES_PER_HOUR)
    required = downloads + decoded + STAGING_BYTES
    record: dict = {
        "required_bytes": required,
        "breakdown": {
            "downloads_at_the_ceiling": downloads,
            "decoded_16k_mono_pcm": decoded,
            "staging_and_extraction": STAGING_BYTES,
        },
        "free_bytes": None,
        "measured_at": None,
        "enough": None,
    }
    if out is None:
        return record
    probe = out.resolve()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        missing.append(f"cannot measure free space at {probe.name}: {exc}")
        return record
    record["free_bytes"] = free
    record["measured_at"] = probe.name
    record["enough"] = free >= required
    if not record["enough"]:
        missing.append(
            f"free space: {free} bytes available where the work directory would "
            f"be, {required} needed ({downloads} downloads at the ceiling + "
            f"{decoded} decoded + {STAGING_BYTES} staging)"
        )
    return record


def _preflight_resume(out: Path | None) -> dict | None:
    if out is None:
        return None
    state_path = out / f"common_voice_{LANGUAGE}.state.json"
    if not state_path.is_file():
        return {"state": None}
    try:
        state = load_state(state_path)
    except (Refused, json.JSONDecodeError) as exc:
        return {"state": state_path.name, "readable": False, "problem": scrub(str(exc))}
    return {
        "state": state_path.name,
        "readable": True,
        "complete": bool(state.get("complete")),
        "files_downloaded": len(state.get("downloads", {})),
        "clips_decoded": len(state.get("clips", {})),
        "hours": state.get("hours"),
        "decoder": state.get("decoder") or None,
    }


# ── the plan ─────────────────────────────────────────────────────────────────


def plan() -> dict:
    """What a fetch would do. No credential, no network, no side effects."""
    stem = f"common_voice_{LANGUAGE}"
    return {
        "dataset": REPO_ID,
        "language": LANGUAGE,
        "license": LICENSE,
        "license_permits": (
            "use, modification, redistribution and commercial use, with no "
            "attribution requirement"
        ),
        "license_does_not_cover": (
            "the terms of access accepted to reach the gated distribution, "
            "which are a separate agreement; and it does not make committing "
            "audio to this repository acceptable"
        ),
        "role": (
            "recorded negatives only, and measurement only: no positive is "
            "taken from this corpus and nothing is fitted on it"
        ),
        "target_hours": TARGET_HOURS,
        "why_that_many_hours": (
            f"with zero activations in n hours the 95% upper bound on the rate "
            f"is {POISSON_ZERO_EVENT_95}/n (the exact 3/n rule of three), so "
            f"demonstrating {ACCEPTANCE_FA_PER_HOUR}/h needs at least "
            f"{hours_required():.2f} h; {TARGET_HOURS} h lands the bound at "
            f"{bound_per_hour(TARGET_HOURS):.4f}/h with margin for the clips "
            "the corpus's own metadata excludes"
        ),
        "derivation": derivation(),
        "usage": USAGE,
        "split": SPLIT,
        "forbidden_purposes": list(FORBIDDEN_PURPOSES),
        "why_sealed": (
            "a negative set the model was fitted on cannot bound its "
            "false-activation rate; freeze_manifest.assert_usable_for refuses "
            f"usage={USAGE!r} for training and validation, and "
            "assert_never_trainable re-checks that against the manifest this "
            "script writes"
        ),
        "selection_seed": SELECTION_SEED,
        "selection": (
            "the seed is frozen into the selection record before any candidate "
            "is scored; shards and clips are taken in seeded order and the pass "
            "stops at the target hours, so which shards are downloaded is "
            "determined by the seed and the bound rather than by listing order"
        ),
        "overlap_exclusion_fields": list(REQUIRED_TSV_FIELDS + OPTIONAL_TSV_FIELDS),
        "identity_not_claimed": list(IDENTITY_NOT_CLAIMED),
        "decode": (
            f"{DECODER_NAME} once per clip to {SAMPLE_RATE} Hz mono "
            f"{SAMPLE_WIDTH * 8}-bit PCM — the only format "
            "build_dataset.read_wav16 accepts — then frozen; a resume verifies "
            "the decoded digests instead of decoding again"
        ),
        "estimated_download_bytes": int(TARGET_HOURS * MP3_BYTES_PER_HOUR),
        "estimated_decoded_bytes": int(TARGET_HOURS * DECODED_BYTES_PER_HOUR),
        "download_ceiling_bytes": ceiling_bytes(TARGET_HOURS),
        "revision_policy": "a full 40-character commit sha; branch names are refused",
        "produces": [
            "clips/… and decoded/… (never committed; the work directory is outside the repo)",
            f"{stem}.lock.json          url, size, sha256 and licence of every fetched file",
            f"{stem}.selection.json     the frozen seed and the eligibility rules",
            f"{stem}.state.json         the resume record; complete=false until it is",
            f"{stem}.manifest.json      frozen, usage={USAGE}, split={SPLIT}",
            f"{stem}.manifest.SHA256SUMS  coreutils format, for `sha256sum -c`",
        ],
        "owner_action": [
            f"accept the dataset terms at https://huggingface.co/datasets/{REPO_ID}",
            f"export a READ-scope token as {' or '.join(TOKEN_VARS)}",
        ],
        "token_present": token_source() is not None,
    }


# ── the command line ─────────────────────────────────────────────────────────


def emit(payload: dict) -> None:
    """Print a report, having proved it carries no credential."""
    text = json.dumps(payload, indent=2)
    assert_no_credential(text, "stdout")
    print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="print the plan and stop")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="check credentials, decoder, disk and listing without fetching",
    )
    parser.add_argument("--out", type=Path, help="work directory for the corpus")
    parser.add_argument("--revision", help="full 40-character commit sha")
    parser.add_argument("--file-list", type=Path, help="one dataset-relative path per line")
    parser.add_argument("--target-hours", type=float, default=TARGET_HOURS)
    parser.add_argument("--max-bytes", type=int, default=None)
    parser.add_argument(
        "--exclude-client-ids",
        type=Path,
        help="file of corpus-published client_id values to hold out, one per line",
    )
    parser.add_argument("--ffmpeg", help=f"path to {DECODER_NAME}, if it is not on PATH")
    parser.add_argument(
        "--establish-lock",
        action="store_true",
        help="first fetch only: record the digests received, for review",
    )
    # There is deliberately no --token: argv is world-readable on Linux, so a
    # credential passed that way is visible to every process on the machine.
    args = parser.parse_args(argv)

    if args.preflight:
        report = preflight(
            args.out,
            revision=args.revision,
            file_list=args.file_list,
            target_hours=args.target_hours,
            max_bytes=args.max_bytes,
            ffmpeg=args.ffmpeg,
        )
        emit(report)
        if report["ready"]:
            return 0
        return 2 if report["blocked_on_owner_only"] else 1

    if args.plan or not any((args.out, args.revision, args.file_list)):
        emit(plan())
        return 0

    missing = [
        flag
        for flag, value in (
            ("--out", args.out),
            ("--revision", args.revision),
            ("--file-list", args.file_list),
        )
        if not value
    ]
    if missing:
        parser.error(f"a fetch needs {', '.join(missing)}")

    max_bytes = ceiling_bytes(args.target_hours) if args.max_bytes is None else args.max_bytes
    exclude = ()
    if args.exclude_client_ids:
        exclude = tuple(
            line.strip()
            for line in args.exclude_client_ids.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        )

    try:
        result = acquire(
            args.out,
            args.revision,
            read_file_list(args.file_list),
            max_bytes=max_bytes,
            establish_lock=args.establish_lock,
            target_hours=args.target_hours,
            exclude_client_ids=exclude,
            ffmpeg=args.ffmpeg,
        )
    except CredentialRequired as exc:
        print(scrub(str(exc)), file=sys.stderr)
        return 2
    except (Refused, freeze_manifest.ManifestConflict, ValueError, OSError) as exc:
        print(f"REFUSED: {scrub(str(exc))}", file=sys.stderr)
        return 1
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
