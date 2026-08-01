#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 /explicit/backup/directory" >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel)"
destination="$1"
if [[ "$destination" != /* ]]; then
  echo "backup destination must be an absolute path" >&2
  exit 2
fi
mkdir -p "$destination"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
head="$(git -C "$repo_root" rev-parse --short=12 HEAD)"
bundle="$destination/youtab-agent-runtime-all-refs-${stamp}-${head}.bundle"

git -C "$repo_root" bundle create "$bundle" --all
git bundle verify "$bundle"
sha256sum "$bundle" > "$bundle.sha256"
git -C "$repo_root" for-each-ref --format='%(objectname) %(refname)' \
  | LC_ALL=C sort > "$bundle.refs"
sha256sum "$bundle.refs" >> "$bundle.sha256"
printf '%s\n' "$bundle"
