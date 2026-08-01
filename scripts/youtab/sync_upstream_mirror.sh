#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

remote="${1:-upstream}"
expected_url="https://github.com/NousResearch/hermes-agent.git"
actual_url="$(git remote get-url "$remote")"
if [[ "$actual_url" != "$expected_url" && "$actual_url" != "${expected_url%.git}" ]]; then
  echo "refusing unexpected upstream URL: $actual_url" >&2
  exit 2
fi

# This remote is read-only by policy. A disabled push URL also prevents an
# accidental command from writing to the upstream project.
git remote set-url --push "$remote" DISABLED

git fetch --prune "$remote" \
  '+refs/heads/*:refs/remotes/upstream/*' \
  '+refs/heads/*:refs/youtab-upstream/heads/*' \
  '+refs/tags/*:refs/youtab-upstream/tags/*'

python3 scripts/youtab/verify_upstream_mirror.py --remote "$remote"
