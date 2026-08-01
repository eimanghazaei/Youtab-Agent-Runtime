#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
python_lock="${YOUTAB_PYTHON_LOCK:-$repo_root/.youtab/supply-chain/python-all.requirements.lock}"
wheelhouse="${YOUTAB_WHEELHOUSE:?set YOUTAB_WHEELHOUSE to the approved wheel directory}"
npm_cache="${YOUTAB_NPM_CACHE:?set YOUTAB_NPM_CACHE to the approved npm cache directory}"

[[ -f "$python_lock" ]] || { echo "missing Python lock: $python_lock" >&2; exit 2; }
[[ -d "$wheelhouse" ]] || { echo "missing wheelhouse: $wheelhouse" >&2; exit 2; }
[[ -d "$npm_cache" ]] || { echo "missing npm cache: $npm_cache" >&2; exit 2; }

export PIP_NO_INDEX=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export npm_config_offline=true
export npm_config_audit=false
export npm_config_fund=false
export npm_config_cache="$npm_cache"

python -m pip install \
  --no-index \
  --only-binary=:all: \
  --find-links "$wheelhouse" \
  --require-hashes \
  -r "$python_lock"
python -m pip install --no-index --no-deps -e "$repo_root"

cd "$repo_root"
npm ci --offline --ignore-scripts=false
(cd website && npm ci --offline --ignore-scripts=false)
(cd scripts/whatsapp-bridge && npm ci --offline --ignore-scripts=false)
(cd plugins/platforms/photon/sidecar && npm ci --offline --ignore-scripts=false)

echo "offline dependency installation completed without registry access"
