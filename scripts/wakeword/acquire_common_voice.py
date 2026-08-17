#!/usr/bin/env python3
"""Fetch a bounded slice of Common Voice as recorded negatives — once a token exists.

Nothing here has downloaded anything. The corpus is gated: reaching it needs a
person to accept the dataset's terms under their own account and to mint a
read-scope token, and neither is something this repository can do or should
hold. ``--plan`` therefore runs with no credential and no network and prints
exactly what a fetch would do; the fetch modes refuse to start without a token
rather than prompting for one.

Why the corpus at all
---------------------
Acceptance asks for ≤ 0.2 false activations per hour of recorded speech, and
every recorded negative the model has ever seen is a *single word* from Speech
Commands. The model card says as much in its limitations. Common Voice is read
sentences from tens of thousands of speakers on whatever microphone they had,
which is the distribution an always-on microphone actually hears. Negatives
only: no clip contains the wake phrase, so every one is a true negative by
construction — the same argument that makes Speech Commands safe unlabelled.

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

See COMMON_VOICE.md for the bound, the licence and the Owner action.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import freeze_manifest  # noqa: E402

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

#: Derived in COMMON_VOICE.md: with zero activations in n hours the 95% upper
#: bound on the rate is about 3/n, so 20 hours is what it takes to demonstrate
#: a 0.2/h budget with margin.
TARGET_HOURS = 20

#: Estimates, under the assumptions named in COMMON_VOICE.md. Printed by
#: --plan and never used as a check: the script measures the real figures.
ESTIMATED_DOWNLOAD_BYTES = 290_000_000
ESTIMATED_DECODED_BYTES = 2_300_000_000

_CHUNK = 1 << 20
_TIMEOUT = 600


class CredentialRequired(RuntimeError):
    """The Owner-only step has not been done. Not an error to retry around."""


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
        "Run with --plan to see exactly what a fetch would do, with no "
        "credential and no network."
    )


def read_file_list(path: Path) -> tuple[str, ...]:
    """Repository-relative paths to fetch, one per line, ``#`` for comments."""
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    paths = tuple(line for line in lines if line and not line.startswith("#"))
    if not paths:
        raise ValueError(f"{path.name} lists no files")
    for entry in paths:
        if entry.startswith("/") or ".." in entry.split("/"):
            raise ValueError(f"refusing a path that escapes the dataset: {entry!r}")
    return paths


def _looks_like_a_commit(revision: str) -> bool:
    return len(revision) == 40 and all(c in "0123456789abcdef" for c in revision.lower())


def fetch_one(repo: str, revision: str, path: str, into: Path, token: str) -> Path:
    """Download one file, streaming it to disk, and return where it landed.

    Staged through ``.partial`` and renamed, so an interrupted fetch cannot
    leave a truncated file that hashes into the lock on the next run. Same
    reason ``assets.fetch`` does it.
    """
    target = into / Path(path).name
    if target.exists():
        return target
    url = RESOLVE.format(repo=repo, revision=revision, path=path)
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    partial = target.with_suffix(target.suffix + ".partial")
    partial.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
            with partial.open("wb") as handle:
                while chunk := response.read(_CHUNK):
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
    partial.replace(target)
    return target


def load_lock(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(k): str(v) for k, v in data.get("files", {}).items()}


def write_lock(path: Path, repo: str, revision: str, digests: dict[str, str]) -> None:
    path.write_text(
        json.dumps(
            {
                "dataset": repo,
                "language": LANGUAGE,
                "revision": revision,
                "license": LICENSE,
                "files": dict(sorted(digests.items())),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def acquire(
    out: Path,
    revision: str,
    files: tuple[str, ...],
    *,
    max_bytes: int,
    establish_lock: bool,
    repo: str = REPO_ID,
) -> dict:
    """Fetch, verify against the lock, and freeze — in that order.

    The byte budget is checked *before* each request, so the loop stops at the
    first file that would begin past it. The file that crosses the line is
    still fetched whole — nothing here knows a shard's size before asking for
    it — so set the budget with a shard's worth of headroom. What it rules out
    is the failure this whole file is bounded against: an acquisition that
    quietly keeps going and becomes tens of gigabytes.
    """
    if not _looks_like_a_commit(revision):
        raise ValueError(
            f"--revision must be a full 40-character commit sha, not {revision!r}. "
            "A branch name is not a version: the same name can resolve to "
            "different bytes tomorrow, and the measurement would not know."
        )

    token = token_from_environment()
    clips = out / "clips"
    clips.mkdir(parents=True, exist_ok=True)
    lock_path = out / f"common_voice_{LANGUAGE}.lock.json"
    locked = load_lock(lock_path)
    if not locked and not establish_lock:
        raise CredentialRequired(
            f"no lock file at {lock_path.name} and --establish-lock was not "
            "given. The first fetch has nothing to verify against, which is a "
            "decision rather than a default: run it with --establish-lock and "
            "review the digests it prints."
        )

    digests: dict[str, str] = {}
    total = 0
    for path in files:
        if total >= max_bytes:
            break
        target = fetch_one(repo, revision, path, clips, token)
        digest = freeze_manifest.sha256_file(target)
        expected = locked.get(path)
        if expected and expected != digest:
            raise RuntimeError(
                f"{path}: SHA-256 mismatch against {lock_path.name}\n"
                f"  expected {expected}\n"
                f"  got      {digest}\n"
                "The published bytes changed. Nothing downstream should run "
                "until somebody decides which set the measurements refer to."
            )
        digests[path] = digest
        total += target.stat().st_size

    if establish_lock:
        write_lock(lock_path, repo, revision, {**locked, **digests})

    manifest = freeze_manifest.freeze(
        clips,
        out / f"common_voice_{LANGUAGE}.manifest.json",
        dataset=f"{repo}@{revision[:12]}",
        split="train",
        # Negatives the model is FITTED on. Marked as training so
        # assert_usable_for refuses if anyone later tries to measure on it: a
        # set the model was fitted against is not a measurement of anything.
        usage="training",
        note=f"Common Voice {LANGUAGE}, {LICENSE}; recorded negatives only",
        allow_in_repo=False,
    )
    return {
        "files_fetched": len(digests),
        "bytes_fetched": total,
        "manifest_sha256": manifest["manifest_sha256"],
        "lock": lock_path.name,
    }


def plan() -> dict:
    """What a fetch would do. No credential, no network, no side effects."""
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
        "role": "recorded negatives only; no positive is taken from this corpus",
        "target_hours": TARGET_HOURS,
        "why_that_many_hours": (
            "with zero activations in n hours the 95% upper bound on the rate "
            "is about 3/n, so 20 h demonstrates the 0.2/h budget at 0.15/h"
        ),
        "estimated_download_bytes": ESTIMATED_DOWNLOAD_BYTES,
        "estimated_decoded_bytes": ESTIMATED_DECODED_BYTES,
        "revision_policy": "a full 40-character commit sha; branch names are refused",
        "produces": [
            "clips/… (never committed; the work directory is outside the repo)",
            f"common_voice_{LANGUAGE}.lock.json",
            f"common_voice_{LANGUAGE}.manifest.json  (usage=training, split=train)",
            f"common_voice_{LANGUAGE}.manifest.SHA256SUMS  (coreutils format)",
        ],
        "owner_action": [
            f"accept the dataset terms at https://huggingface.co/datasets/{REPO_ID}",
            f"export a READ-scope token as {' or '.join(TOKEN_VARS)}",
        ],
        "token_present": any(os.environ.get(name, "").strip() for name in TOKEN_VARS),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", action="store_true", help="print the plan and stop")
    parser.add_argument("--out", type=Path, help="work directory for the corpus")
    parser.add_argument("--revision", help="full 40-character commit sha")
    parser.add_argument("--file-list", type=Path, help="one dataset-relative path per line")
    parser.add_argument("--max-bytes", type=int, default=ESTIMATED_DOWNLOAD_BYTES)
    parser.add_argument(
        "--establish-lock",
        action="store_true",
        help="first fetch only: record the digests received, for review",
    )
    args = parser.parse_args(argv)

    if args.plan or not any((args.out, args.revision, args.file_list)):
        print(json.dumps(plan(), indent=2))
        return 0

    missing = [
        flag
        for flag, value in (("--out", args.out), ("--revision", args.revision), ("--file-list", args.file_list))
        if not value
    ]
    if missing:
        parser.error(f"a fetch needs {', '.join(missing)}")

    try:
        result = acquire(
            args.out,
            args.revision,
            read_file_list(args.file_list),
            max_bytes=args.max_bytes,
            establish_lock=args.establish_lock,
        )
    except CredentialRequired as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
