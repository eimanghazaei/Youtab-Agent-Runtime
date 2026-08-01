#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
output="${1:-$repo_root/.youtab/evidence}"
mkdir -p "$output"

python3 "$repo_root/scripts/youtab/verify_supply_chain.py" \
  --mode source --output "$output/source-audit.json"
python3 "$repo_root/scripts/youtab/generate_source_sbom.py" \
  --output "$output/source-sbom.cdx.json"
python3 "$repo_root/scripts/youtab/generate_provenance.py" \
  --sbom "$output/source-sbom.cdx.json" \
  --output "$output/source-provenance.intoto.jsonl"

(
  cd "$output"
  sha256sum source-audit.json source-sbom.cdx.json source-provenance.intoto.jsonl \
    > SHA256SUMS
  sha256sum -c SHA256SUMS
)

echo "$output"
