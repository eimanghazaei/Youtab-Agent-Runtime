#!/usr/bin/env python3
"""Freeze a dataset: hash every file, hash the list, and refuse to drift.

Why
---
Every external byte this pipeline downloads is pinned by SHA-256 in
``assets.py``, and a rerun that fetches different bytes fails at stage 0 rather
than quietly training a different model. Datasets that are *not* downloaded get
none of that. A folder of recordings is exactly as authoritative as whatever is
in it today: a re-export with different loudness normalisation, a file dropped
by a sync client, a take deleted because it sounded bad — none of those leave a
trace, and all of them silently change what a measurement means.

Two failures this is built to prevent, both of which are quiet:

**A measurement that cannot be tied to an input.** "5/5 on the E002 set" is a
claim about specific bytes. Without a frozen list there is no way to prove,
later, which bytes.

**A sealed evaluation set being trained on.** This is the expensive one. A
single-use evaluation speaker is worth exactly as much as its unseenness, and
it is destroyed by one convenient ``--data-dir`` pointed at the wrong folder.
So usage is not a comment: it is a field, and ``assert_usable_for`` raises on
the combination rather than trusting the caller to have read the README.

What "frozen" means here
------------------------
* **Content-addressed.** ``manifest_sha256`` is computed over the canonical
  serialization of the manifest body, so reformatting the JSON does not change
  the identity and a single edited digest does.
* **Refuses to overwrite.** Re-freezing an unchanged dataset is a no-op.
  Re-freezing a *changed* one raises and says what moved, because silently
  rewriting the manifest is the same as not having one.
* **Independently verifiable.** Alongside the manifest it writes coreutils
  ``SHA256SUMS`` files, so the same claim can be checked by ``sha256sum -c``,
  ``shasum -a 256 -c`` or ``certutil`` without running any of this code. A
  verifier that is the only thing able to verify its own output proves less
  than it appears to.
* **Says nothing about where the data lives.** Only the root's *basename* is
  recorded, never its absolute path, and nothing printed includes one. A
  manifest is meant to be attachable to a report; the capture root is not.

This is written from the requirements rather than ported from the equivalent
tool in the training tree, so the two are not assumed to interoperate:
``schema_version`` is checked on read and a manifest from another
implementation is rejected rather than half-understood.

Usage::

    freeze_manifest.py freeze --root DIR --out M.json \\
        --dataset E003 --split evaluate --usage sealed-evaluation
    freeze_manifest.py verify --root DIR --manifest M.json [--strict]
    freeze_manifest.py usable --manifest M.json --for training
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

#: Bumped when the manifest body changes shape. A manifest carrying a version
#: this tool does not know is refused, not guessed at.
SCHEMA_VERSION = 1

#: How the data may be consumed. ``sealed-evaluation`` is the one that has to
#: survive contact with a hurried command line.
USAGES = ("training", "validation", "sealed-evaluation")

#: Purposes a caller can ask about, and the usages each one permits.
_PERMITTED: dict[str, tuple[str, ...]] = {
    "training": ("training",),
    "validation": ("validation",),
    "evaluation": ("sealed-evaluation",),
}

#: Files that are never part of a dataset: editor and sync-client litter, and
#: this tool's own output.
_SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", "SHA256SUMS"})

_CHUNK = 1 << 20


class ManifestConflict(RuntimeError):
    """An existing manifest describes a different dataset."""


class SealedDatasetError(RuntimeError):
    """A sealed evaluation set was requested for something other than measuring."""


@dataclass(frozen=True)
class Entry:
    """One file, as frozen."""

    path: str  # POSIX, relative to the dataset root
    bytes: int
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(body: dict) -> bytes:
    """The bytes a manifest's identity is computed over.

    Sorted keys and no incidental whitespace, so the digest describes the
    content and not the formatting. ``ensure_ascii=False`` keeps a non-ASCII
    filename as itself rather than as an escape sequence, which matters because
    the same name has to hash identically on a machine with a different locale.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def scan(root: Path, include: tuple[str, ...] = ()) -> list[Entry]:
    """Hash every file under ``root``, in a stable order.

    Sorted by relative POSIX path rather than by directory-walk order: two
    machines enumerate a directory differently, and a manifest whose order
    depends on the filesystem would produce a different digest for identical
    content.
    """
    if not root.is_dir():
        # The basename, like every other path this module emits. An absolute
        # capture root in an exception is the same leak as one in a manifest,
        # and it is the form that actually reaches a log: the drive is not
        # mounted, so this is the first thing that fails.
        raise NotADirectoryError(f"dataset root {root.name!r} does not exist")
    entries: list[Entry] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if path.name in _SKIP_NAMES or path.name.startswith("."):
            continue
        if include and not any(path.match(pattern) for pattern in include):
            continue
        entries.append(Entry(rel, path.stat().st_size, sha256_file(path)))
    entries.sort(key=lambda entry: entry.path)
    return entries


def build(
    root: Path,
    *,
    dataset: str,
    split: str,
    usage: str,
    include: tuple[str, ...] = (),
    note: str = "",
) -> dict:
    """The manifest body plus its own digest, without touching the disk."""
    if usage not in USAGES:
        raise ValueError(f"usage must be one of {USAGES}, not {usage!r}")
    if not dataset.strip():
        raise ValueError("a dataset name is required; a manifest nobody can name is not evidence")

    entries = scan(root, include)
    if not entries:
        raise ValueError(
            f"no files found under {root.name}. Freezing an empty manifest would "
            "record that a dataset was verified when nothing was."
        )

    body = {
        "schema_version": SCHEMA_VERSION,
        "dataset": dataset,
        "split": split,
        "usage": usage,
        # The basename only. A manifest travels with reports; the absolute
        # capture root does not travel anywhere.
        "root_name": root.resolve().name,
        "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tool": "scripts/wakeword/freeze_manifest.py",
        "file_count": len(entries),
        "total_bytes": sum(entry.bytes for entry in entries),
        "note": note,
        "files": [{"path": e.path, "bytes": e.bytes, "sha256": e.sha256} for e in entries],
    }
    return {**body, "manifest_sha256": manifest_digest(body)}


def manifest_digest(manifest: dict) -> str:
    """Digest of a manifest body, ignoring any digest already recorded in it."""
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    # `created_utc` is inside the digest on purpose: two freezes of identical
    # bytes at different times are different records of different moments, and
    # the equality check in `freeze` compares the file list, not the digest.
    return hashlib.sha256(_canonical(body)).hexdigest()


def _same_contents(left: dict, right: dict) -> bool:
    """Do two manifests describe the same files, split and usage?"""
    keys = ("dataset", "split", "usage", "files")
    return all(left.get(key) == right.get(key) for key in keys)


def _describe_difference(existing: dict, fresh: dict) -> str:
    old = {entry["path"]: entry["sha256"] for entry in existing.get("files", [])}
    new = {entry["path"]: entry["sha256"] for entry in fresh.get("files", [])}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(path for path in set(old) & set(new) if old[path] != new[path])
    parts = []
    for label, paths in (("added", added), ("removed", removed), ("changed", changed)):
        if paths:
            shown = ", ".join(paths[:5]) + ("…" if len(paths) > 5 else "")
            parts.append(f"{len(paths)} {label} ({shown})")
    for field in ("dataset", "split", "usage"):
        if existing.get(field) != fresh.get(field):
            parts.append(f"{field}: {existing.get(field)!r} -> {fresh.get(field)!r}")
    return "; ".join(parts) or "no file differences, but the records disagree"


def freeze(
    root: Path,
    out: Path,
    *,
    dataset: str,
    split: str,
    usage: str,
    include: tuple[str, ...] = (),
    note: str = "",
    allow_in_repo: bool = False,
) -> dict:
    """Write the manifest and its sidecars, refusing to overwrite a different one.

    Idempotent when nothing changed, so this is safe to run at the top of a
    session. When something *has* changed the right answer is never to
    overwrite: either the dataset was edited after being frozen, which is a
    finding, or a new manifest is wanted, which is a new filename.
    """
    _refuse_repo_destination(out, allow_in_repo)
    fresh = build(root, dataset=dataset, split=split, usage=usage, include=include, note=note)

    if out.exists():
        existing = load(out)
        if _same_contents(existing, fresh):
            return existing  # already frozen, byte-for-byte
        raise ManifestConflict(
            f"{out.name} already describes a different dataset: "
            f"{_describe_difference(existing, fresh)}.\n"
            "Refusing to overwrite it. If the dataset legitimately changed, "
            "freeze it under a new name — the old manifest is what an earlier "
            "measurement referred to."
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" everywhere this writes. Python's text mode translates "\n"
    # to the platform separator, and a CRLF checksum file is not a checksum
    # file: GNU sha256sum reads the carriage return as part of the filename and
    # reports every line as "FAILED open or read". The independent verification
    # is the whole point of the sidecars, so it cannot be Windows-only broken.
    # It also keeps the manifest's own file digest identical across platforms.
    out.write_text(
        json.dumps(fresh, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    _write_sidecars(out, fresh)
    return fresh


def _refuse_repo_destination(out: Path, allow_in_repo: bool) -> None:
    """A manifest lists filenames, and filenames of private speech are private.

    The repository has a gate that fails on tracked transcripts and speaker
    paths (``tests/tools/test_wakeword_no_human_data_committed.py``). This is
    the same rule one step earlier: do not write the file into the checkout in
    the first place.
    """
    if allow_in_repo:
        return
    repo = Path(__file__).resolve().parents[2]
    resolved = out.resolve()
    if repo == resolved or repo in resolved.parents:
        raise ValueError(
            f"refusing to write a manifest inside the repository ({resolved.name}). "
            "Manifests list filenames, and filenames of recordings are part of "
            "what must stay on local disk. Write it beside the dataset. Pass "
            "allow_in_repo=True only for a public corpus."
        )


def _coreutils_line(digest: str, path: str) -> str:
    """One sha256sum line, escaped the way coreutils escapes one.

    GNU coreutils prefixes the whole line with a backslash and escapes a
    backslash and a newline in the filename when either is present; sha256sum
    -c reverses exactly that. Writing the raw bytes instead emits a listing
    with more lines than the manifest has files, so the independent check --
    which is the entire reason this sidecar exists -- reports "FAILED open or
    read" on a tree that is intact.
    """
    if "\\" in path or "\n" in path:
        escaped = path.replace("\\", "\\\\").replace("\n", "\\n")
        return "\\" + digest + "  " + escaped + "\n"
    return digest + "  " + path + "\n"


def _write_sidecars(out: Path, manifest: dict) -> tuple[Path, Path]:
    """coreutils-format checksums, so something other than this tool can check.

    ``SHA256SUMS`` is relative to the dataset root and verifies the data;
    ``<manifest>.sha256`` verifies the manifest file itself. Both are the exact
    format ``sha256sum -c`` and ``shasum -a 256 -c`` read.
    """
    sums = out.with_name(out.stem + ".SHA256SUMS")
    sums.write_text(
        "".join(_coreutils_line(entry["sha256"], entry["path"]) for entry in manifest["files"]),
        encoding="utf-8",
        newline="\n",
    )
    self_sum = out.with_name(out.name + ".sha256")
    self_sum.write_text(f"{sha256_file(out)}  {out.name}\n", encoding="utf-8", newline="\n")
    return sums, self_sum


def load(path: Path) -> dict:
    """Read a manifest, refusing one this tool does not understand."""
    manifest = json.loads(path.read_text(encoding="utf-8"))
    version = manifest.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} is schema_version {version!r}; this tool writes and "
            f"reads {SCHEMA_VERSION}. Refusing to interpret it — a manifest read "
            "under the wrong schema verifies the wrong thing."
        )
    return manifest


def verify(manifest_path: Path, root: Path, *, strict: bool = False) -> dict:
    """Re-hash everything and report what no longer matches.

    Both halves, because they fail differently. The manifest's own digest
    catches an edited record — the case where somebody "fixed" a hash. The
    per-file hashes catch edited data. Neither implies the other.
    """
    manifest = load(manifest_path)
    recorded = manifest.get("manifest_sha256", "")
    recomputed = manifest_digest(manifest)

    missing: list[str] = []
    changed: list[str] = []
    for entry in manifest["files"]:
        path = root / entry["path"]
        if not path.is_file():
            missing.append(entry["path"])
            continue
        if sha256_file(path) != entry["sha256"]:
            changed.append(entry["path"])

    listed = {entry["path"] for entry in manifest["files"]}
    on_disk = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name not in _SKIP_NAMES and not p.name.startswith(".")
    }
    extra = sorted(on_disk - listed)

    return {
        "dataset": manifest.get("dataset"),
        "split": manifest.get("split"),
        "usage": manifest.get("usage"),
        "files_checked": len(manifest["files"]),
        "manifest_digest_matches": recorded == recomputed,
        "missing": missing,
        "changed": changed,
        "extra": extra,
        "passed": (
            recorded == recomputed
            and not missing
            and not changed
            and (not extra or not strict)
        ),
    }


def assert_usable_for(manifest: dict, purpose: str) -> None:
    """Refuse a dataset for a purpose its usage does not allow.

    The sealed-evaluation case is the reason this exists. A single-use
    evaluation speaker is worth exactly its unseenness, and nothing about a
    directory of WAV files signals that pointing a trainer at it destroys the
    only unbiased measurement available.
    """
    if purpose not in _PERMITTED:
        raise ValueError(f"purpose must be one of {tuple(_PERMITTED)}, not {purpose!r}")
    usage = manifest.get("usage")
    if usage in _PERMITTED[purpose]:
        return
    if usage == "sealed-evaluation":
        raise SealedDatasetError(
            f"{manifest.get('dataset')!r} is a sealed evaluation set and cannot be "
            f"used for {purpose}. Consuming it here would spend the only "
            "measurement nobody has tuned against, and it cannot be replaced by "
            "re-recording the same speaker — they would no longer be unseen."
        )
    raise ValueError(
        f"{manifest.get('dataset')!r} is marked usage={usage!r}, which does not "
        f"permit {purpose}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    freezer = sub.add_parser("freeze", help="write a manifest for a dataset")
    freezer.add_argument("--root", type=Path, required=True)
    freezer.add_argument("--out", type=Path, required=True)
    freezer.add_argument("--dataset", required=True)
    freezer.add_argument("--split", required=True, help="train | validate | evaluate")
    freezer.add_argument("--usage", required=True, choices=USAGES)
    freezer.add_argument("--include", action="append", default=[], help="glob, repeatable")
    freezer.add_argument("--note", default="")
    freezer.add_argument(
        "--allow-in-repo",
        action="store_true",
        help="permit writing inside the checkout (public corpora only)",
    )

    verifier = sub.add_parser("verify", help="re-hash a dataset against its manifest")
    verifier.add_argument("--root", type=Path, required=True)
    verifier.add_argument("--manifest", type=Path, required=True)
    verifier.add_argument("--strict", action="store_true", help="unlisted files fail too")

    usable = sub.add_parser("usable", help="assert a manifest may be used for a purpose")
    usable.add_argument("--manifest", type=Path, required=True)
    usable.add_argument("--for", dest="purpose", required=True, choices=tuple(_PERMITTED))

    args = parser.parse_args(argv)

    if args.command == "freeze":
        manifest = freeze(
            args.root,
            args.out,
            dataset=args.dataset,
            split=args.split,
            usage=args.usage,
            include=tuple(args.include),
            note=args.note,
            allow_in_repo=args.allow_in_repo,
        )
        print(
            f"{manifest['dataset']}  {manifest['file_count']} files, "
            f"{manifest['total_bytes'] / 1e6:.1f} MB, usage={manifest['usage']}"
        )
        print(f"  manifest_sha256 {manifest['manifest_sha256']}")
        print(f"  wrote {args.out.name}, {args.out.stem}.SHA256SUMS, {args.out.name}.sha256")
        print("  independent check:  sha256sum -c <root>/../" + f"{args.out.stem}.SHA256SUMS")
        return 0

    if args.command == "verify":
        report = verify(args.manifest, args.root, strict=args.strict)
        print(
            f"{report['dataset']}  {report['files_checked']} files  "
            f"usage={report['usage']}  split={report['split']}"
        )
        print(f"  manifest digest: {'ok' if report['manifest_digest_matches'] else 'MISMATCH'}")
        for label in ("missing", "changed", "extra"):
            names = report[label]
            if names:
                shown = ", ".join(names[:5]) + ("…" if len(names) > 5 else "")
                print(f"  {label}: {len(names)} ({shown})")
        print("  PASS" if report["passed"] else "  FAIL")
        return 0 if report["passed"] else 1

    manifest = load(args.manifest)
    try:
        assert_usable_for(manifest, args.purpose)
    except (SealedDatasetError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(f"{manifest['dataset']} (usage={manifest['usage']}) may be used for {args.purpose}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
