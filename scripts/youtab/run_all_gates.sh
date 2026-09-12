#!/usr/bin/env bash
#
# Run every Youtab gate and report the whole picture.
#
# Deliberately not `set -e`. Under `set -e` this script stopped at the first
# red gate, and since the branding gate runs first, one failure there meant a
# CI run reported nothing at all about secrets, dependencies, SAST, OWASP, the
# runtime test suite, compileall, ruff or the lock file -- the job exited in
# forty seconds having measured one thing out of ten. Every gate now runs, its
# exit code is recorded, and the script fails at the end if any of them failed.
#
# A gate that cannot run (missing binary, exit 127) is reported as a failure
# like any other. Not being able to check something is never a pass.
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
evidence_dir="${1:-$repo_root/.youtab/evidence/local}"
python_bin="${YOUTAB_AGENT_PYTHON:-python}"
mkdir -p "$evidence_dir" || exit 1
cd "$repo_root" || exit 1

gate_names=()
gate_codes=()

# Call immediately after a gate, as `record <name> $?`, so the status captured
# is the gate's own. With `pipefail` set this is correct for piped gates too.
record() {
  gate_names+=("$1")
  gate_codes+=("$2")
  if [ "$2" -eq 0 ]; then
    printf -- '----- %s: PASS\n' "$1"
  else
    printf -- '----- %s: FAIL (exit %d)\n' "$1" "$2"
  fi
}

begin() {
  printf -- '\n===== gate: %s =====\n' "$1"
}

begin branding
"$python_bin" scripts/youtab/branding_gate.py --root . --ocr --output "$evidence_dir/branding.json"
record branding $?

begin secrets
"$python_bin" scripts/youtab/secret_gate.py --root . --output "$evidence_dir/secrets.json"
"$python_bin" scripts/youtab/dependency_exception_gate.py
record secrets $?

begin dependencies
"$python_bin" scripts/youtab/dependency_gate.py --root . --output "$evidence_dir/dependencies.json"
record dependencies $?

begin sast
"$python_bin" scripts/youtab/sast_gate.py --root . --output "$evidence_dir/sast.json"
record sast $?

# Control-plane isolation. Structured, not textual: it parses the Compose and
# Dockerfile so a comment explaining why host networking was removed cannot
# fail the build, and an input it cannot resolve fails closed rather than
# being assumed benign.
begin container-control-plane-isolation
"$python_bin" scripts/youtab/container_isolation_gate.py --root . \
  --output "$evidence_dir/container-isolation.json"
record container-control-plane-isolation $?

begin router-policy
# The authorization policy is only a control while it describes the router the
# application actually builds. This fails closed on nine conditions, including
# a route added without a rule and a socket that accepts without a scope --
# both of which are invisible to any test asserting on a hand-written list.
"$python_bin" scripts/youtab/router_policy_gate.py \
  --json "$evidence_dir/router-policy.json"
record router-policy $?

begin owasp-smoke
"$python_bin" scripts/youtab/owasp_smoke.py > "$evidence_dir/owasp-smoke.json"
record owasp-smoke $?

begin unit-integration-e2e
# `tests/youtab_runtime` alone did not reach the dashboard's own surface, so
# the gateway lifecycle contract — the truthful start/stop/restart result and
# the authorization that guards it — was committed but never executed here.
# A test that CI does not run is documentation.
#
# The whole of `tests/youtab_agent_cli`, not a named subset. The subset was a
# deliberate narrowing while that directory's inherited failures were unowned,
# and it cost real coverage: the authorization flip shipped two rules written
# as fixed-segment patterns against `:path` routes, and both 403'd every real
# request. Nothing in the 19 selected files touched them. `test_web_server.py`,
# `test_plugin_runtime_disable_gate.py` and `test_web_server_gateway_topology.py`
# all caught it immediately.
#
# `run_tests.sh` runs each file in its own subprocess, which is what makes the
# widening viable: the inherited suite's failures were overwhelmingly
# cross-test pollution and disappear under per-file isolation.
# `tests/tools` joins them. It was the largest body of production-relevant
# coverage CI did not run, and that gap was not theoretical: the home-fold
# repair in `tools/approval.py` — without which `/root/.ssh/authorized_keys`
# never folded to `~/.ssh/authorized_keys` and every dangerous-command pattern
# anchored on `~/` stopped firing for a root install — is proved by
# `tests/tools/test_approval.py`, which nothing in CI executed. So are the
# file-write safety, browser secret-exfil and yolo-mode suites.
#
# Nothing is deselected. `test_bundled_hey_youtab_model_ships_on_disk` was
# excluded by name while `tools/wakewords/` shipped no model — the rebrand
# renamed the expected filename, but a text rebrand cannot rename a trained
# model, so the binaries were dropped in the transplant and the advertised
# default detector could not load at all. It could not be closed by renaming
# another model in either: an openWakeWord model only detects the phrase it
# was trained on. The model is now genuinely trained (`scripts/wakeword/`),
# both artifacts ship, and the test runs here like every other.
#
# `--file-retries 0` is load-bearing, not tidiness. The runner defaults to one
# automatic re-run of any failing FILE (`_DEFAULT_FILE_RETRIES = 1`): a file
# that fails then passes is counted as passed and reported as FLAKY. That is a
# reasonable default for a developer's local loop, but in the REQUIRED gate it
# means green can be reached on the second attempt — and a test that only
# passes on retry is not genuinely green. Every test here must pass on its
# first execution. Do not remove this flag to quiet an intermittent failure;
# fix the flake at its root cause instead.
scripts/run_tests.sh \
  tests/youtab_runtime \
  tests/youtab_agent_cli \
  tests/tools \
  --file-retries 0 \
  -q | tee "$evidence_dir/unit-integration-e2e.log"
record unit-integration-e2e $?

begin compileall
"$python_bin" -m compileall -q youtab_runtime youtab_agent_cli agent tools gateway
record compileall $?

begin ruff
# The two lifecycle files are listed individually for the same reason they are
# listed individually above: `youtab_agent_cli` as a whole is inherited code
# whose lint debt is not this gate's subject, but Youtab-owned modules are.
"$python_bin" -m ruff check \
  youtab_runtime scripts/youtab tests/youtab_runtime \
  youtab_agent_cli/gateway_lifecycle.py \
  tests/youtab_agent_cli/test_gateway_lifecycle.py \
  cron/lifecycle_guard.py \
  youtab_agent_cli/dashboard_auth/cloudflare_access.py \
  tests/youtab_agent_cli/test_cloudflare_access_provider.py \
  tests/youtab_agent_cli/test_access_end_to_end.py \
  tests/youtab_agent_cli/test_csrf_and_origin.py \
  --output-format concise | tee "$evidence_dir/quality-ruff.log"
record ruff $?

begin uv-lock
UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/youtab-uv-cache}" uv lock --check --offline | tee "$evidence_dir/uv-lock.log"
record uv-lock $?

begin whitespace
git diff --check
record whitespace $?

failed=0
for code in "${gate_codes[@]}"; do
  [ "$code" -eq 0 ] || failed=$((failed + 1))
done

{
  printf '{\n'
  printf '  "schema_version": 2,\n'
  if [ "$failed" -eq 0 ]; then
    printf '  "passed": true,\n'
  else
    printf '  "passed": false,\n'
  fi
  printf '  "gates_total": %d,\n' "${#gate_names[@]}"
  printf '  "gates_failed": %d,\n' "$failed"
  printf '  "gates": {\n'
  last=$(( ${#gate_names[@]} - 1 ))
  for i in "${!gate_names[@]}"; do
    if [ "$i" -eq "$last" ]; then
      printf '    "%s": %d\n' "${gate_names[$i]}" "${gate_codes[$i]}"
    else
      printf '    "%s": %d,\n' "${gate_names[$i]}" "${gate_codes[$i]}"
    fi
  done
  printf '  }\n'
  printf '}\n'
} > "$evidence_dir/aggregate.json"

printf -- '\n===== gate summary =====\n'
for i in "${!gate_names[@]}"; do
  if [ "${gate_codes[$i]}" -eq 0 ]; then
    printf '  PASS  %s\n' "${gate_names[$i]}"
  else
    printf '  FAIL  %s (exit %d)\n' "${gate_names[$i]}" "${gate_codes[$i]}"
  fi
done

if [ "$failed" -ne 0 ]; then
  printf '\nFAIL: %d of %d Youtab gates failed\n' "$failed" "${#gate_names[@]}"
  exit 1
fi

printf '\nPASS: all %d Youtab gates\n' "${#gate_names[@]}"
