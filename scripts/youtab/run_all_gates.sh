#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
evidence_dir="${1:-$repo_root/.youtab/evidence/local}"
python_bin="${YOUTAB_AGENT_PYTHON:-python}"
mkdir -p "$evidence_dir"
cd "$repo_root"

"$python_bin" scripts/youtab/branding_gate.py --root . --ocr --output "$evidence_dir/branding.json"
"$python_bin" scripts/youtab/secret_gate.py --root . --output "$evidence_dir/secrets.json"
"$python_bin" scripts/youtab/dependency_gate.py --root . --output "$evidence_dir/dependencies.json"
"$python_bin" scripts/youtab/sast_gate.py --root . --output "$evidence_dir/sast.json"
"$python_bin" scripts/youtab/owasp_smoke.py > "$evidence_dir/owasp-smoke.json"
scripts/run_tests.sh tests/youtab_runtime -q | tee "$evidence_dir/unit-integration-e2e.log"
"$python_bin" -m compileall -q youtab_runtime youtab_agent_cli agent tools gateway
"$python_bin" -m ruff check youtab_runtime scripts/youtab tests/youtab_runtime --output-format concise | tee "$evidence_dir/quality-ruff.log"
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/youtab-uv-cache}" uv lock --check --offline | tee "$evidence_dir/uv-lock.log"
git diff --check

printf '{"schema_version":1,"passed":true}\n' > "$evidence_dir/aggregate.json"
printf 'PASS: all local Youtab gates\n'
