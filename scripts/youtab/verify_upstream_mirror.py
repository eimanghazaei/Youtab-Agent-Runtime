#!/usr/bin/env python3
"""Prove that the quarantined local mirror exactly matches upstream refs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        text=True,
        encoding="utf-8",
        errors="replace",
    ).strip()


def remote_refs(remote: str, kind: str) -> dict[str, str]:
    prefix = f"refs/{kind}/"
    output = git("ls-remote", f"--{kind}", remote)
    refs: dict[str, str] = {}
    for line in output.splitlines():
        sha, ref = line.split("\t", 1)
        # Annotated tag peel refs are evidence about the tag object, but the
        # actual mirrored ref is the non-^{} record.
        if ref.endswith("^{}"):
            continue
        refs[ref.removeprefix(prefix)] = sha
    return refs


def local_refs(namespace: str) -> dict[str, str]:
    output = git("for-each-ref", "--format=%(objectname) %(refname)", namespace)
    refs: dict[str, str] = {}
    for line in output.splitlines():
        if not line:
            continue
        sha, ref = line.split(" ", 1)
        refs[ref.removeprefix(namespace)] = sha
    return refs


def diff(expected: dict[str, str], actual: dict[str, str]) -> dict[str, object]:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(k for k in expected.keys() & actual.keys() if expected[k] != actual[k])
    return {"missing": missing, "extra": extra, "changed": changed}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote", default="upstream")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    expected_heads = remote_refs(args.remote, "heads")
    expected_tags = remote_refs(args.remote, "tags")
    actual_heads = local_refs("refs/youtab-upstream/heads/")
    actual_tags = local_refs("refs/youtab-upstream/tags/")
    head_diff = diff(expected_heads, actual_heads)
    tag_diff = diff(expected_tags, actual_tags)
    exact = all(not values for values in head_diff.values()) and all(
        not values for values in tag_diff.values()
    )
    result = {
        "schema_version": 1,
        "remote": args.remote,
        "remote_url": git("remote", "get-url", args.remote),
        "remote_head_count": len(expected_heads),
        "mirrored_head_count": len(actual_heads),
        "remote_tag_count": len(expected_tags),
        "mirrored_tag_count": len(actual_tags),
        "head_diff": head_diff,
        "tag_diff": tag_diff,
        "exact": exact,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if exact else 1


if __name__ == "__main__":
    sys.exit(main())
