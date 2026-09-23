#!/usr/bin/env bash
# Canonical test runner for youtab-agent-runtime. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then ~/.youtab-agent-runtime/...)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Locate python ───────────────────────────────────────────────────────────
# Probe local venvs first; fall back to the Nix devShell's editable venv
# (YOUTAB_AGENT_PYTHON is exported by the devShell hook and ships [dev] extras:
# pytest, pytest-asyncio, pytest-timeout, ruff, ty).
#
# A candidate must have pytest INSTALLED, not merely exist. The release venv
# at ~/.youtab-agent-runtime/youtab-agent-runtime/venv has bin/activate but no pytest, so an
# existence-only probe selected it in checkouts/worktrees without a local
# .venv — every file then died with "No module named pytest" and the run
# reported "0 tests passed" (which reads green at a glance even though the
# exit code is 1). Skip such a venv and keep probing instead.
VENV=""
VENV_PYTHON=""
SKIPPED_VENVS=""
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$HOME/.youtab-agent-runtime/youtab-agent-runtime/venv"; do
  if [ -f "$candidate/bin/activate" ]; then
    if "$candidate/bin/python" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/bin/python"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
  # Native Windows venv layout: python.exe and activate live under
  # Scripts/, and there is no bin/. Anyone running this script from
  # Git Bash / MSYS with a `python -m venv`- or uv-created venv hits
  # this branch — without it the canonical runner refuses to start.
  if [ -f "$candidate/Scripts/activate" ]; then
    if "$candidate/Scripts/python.exe" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/Scripts/python.exe"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
done

if [ -n "$SKIPPED_VENVS" ]; then
  for skipped in $SKIPPED_VENVS; do
    echo "▶ skipping venv without pytest: $skipped" >&2
  done
fi

if [ -n "$VENV" ]; then
  PYTHON="$VENV_PYTHON"
elif [ -n "${YOUTAB_AGENT_PYTHON:-}" ] && [ -x "$YOUTAB_AGENT_PYTHON" ] \
    && "$YOUTAB_AGENT_PYTHON" -c 'import pytest' 2>/dev/null; then
  # Guard with an import check: YOUTAB_AGENT_PYTHON may point at the RELEASE
  # venv (no pytest) when inherited from a wrapped `youtab` binary rather
  # than the devShell hook.
  PYTHON="$YOUTAB_AGENT_PYTHON"
  echo "▶ no local venv — using Nix dev venv via YOUTAB_AGENT_PYTHON: $PYTHON"
else
  echo "error: no virtualenv with pytest found in $REPO_ROOT/.venv or $REPO_ROOT/venv," >&2
  echo "       and YOUTAB_AGENT_PYTHON is not a python with pytest (enter the Nix devShell or create a venv)" >&2
  if [ -n "$SKIPPED_VENVS" ]; then
    echo "       (skipped for missing pytest:$SKIPPED_VENVS — install dev extras there, or create $REPO_ROOT/.venv)" >&2
  fi
  exit 1
fi


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.youtab-agent-runtime/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.youtab-agent-runtime"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Windows location variables (computed before we drop env) ───────────────
# `env -i` forwards HOME, which is enough on POSIX. Native Windows CPython
# resolves Path.home() from USERPROFILE (or HOMEDRIVE+HOMEPATH), stdlib
# platform paths come from LOCALAPPDATA/APPDATA, ssl/sockets need SYSTEMROOT,
# and tempfile needs TEMP/TMP. Dropping them breaks collection on native
# Windows (issues #67385, #70813). These are location variables, not
# credentials, so forwarding them keeps the isolation intent intact. Each is
# only forwarded when actually set, so POSIX runs are byte-for-byte unchanged.
WIN_ENV=()
for _win_var in USERPROFILE HOMEDRIVE HOMEPATH LOCALAPPDATA APPDATA SYSTEMROOT TEMP TMP; do
  if [ -n "${!_win_var:-}" ]; then
    WIN_ENV+=("$_win_var=${!_win_var}")
  fi
done


# ── Runner configuration variables (computed before we drop env) ───────────
# `run_tests_parallel.py` advertises each of these in its own --help text
# ("env: YOUTAB_AGENT_TEST_FILE_RETRIES", "Env: YOUTAB_AGENT_TEST_SLICE (format:
# I/N)", ...). The `env -i` below starts from an EMPTY environment, so any knob
# not named here never reaches the runner: it falls back to its built-in
# default while the caller believes the override took. That is not a cosmetic
# gap. Setting the retry knob to 0 ahead of this script silently kept
# `_DEFAULT_FILE_RETRIES = 1`, so a run the operator had labelled
# "file-retries=0" still re-ran failing files and still reported files that
# passed only on the retry — a hidden retry produced by the plumbing, not by
# the flag. Keep this list in sync with the runner; the consistency test in
# tests/youtab_runtime/test_ci_runner_contract.py fails if it drifts.
#
# These are test-runner configuration, not credentials, so forwarding them
# leaves the isolation intent intact. Each is forwarded only when actually
# set, so a run with none of them set is byte-for-byte unchanged.
RUNNER_ENV=()
for _runner_var in \
  YOUTAB_AGENT_TEST_FILE_RETRIES \
  YOUTAB_AGENT_TEST_FILE_TIMEOUT \
  YOUTAB_AGENT_TEST_IMAGE \
  YOUTAB_AGENT_TEST_PATHS \
  YOUTAB_AGENT_TEST_SLICE \
  YOUTAB_AGENT_TEST_WORKERS
do
  # -n (not :+) so an explicit "0" forwards: it is a meaningful value here,
  # and it is the exact value whose loss caused the hidden retry.
  if [ -n "${!_runner_var:-}" ]; then
    RUNNER_ENV+=("$_runner_var=${!_runner_var}")
  fi
done

# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
echo "▶ pre-compiling bytecode cache"
"$PYTHON" -m compileall -q -j 0 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

# ── ...and the installed dependencies, for the same reason ──────────────────
# The warm-up above stops at the repo boundary, and the workers do not: the
# first parallel launch has hundreds of subprocesses importing the same
# dependency trees at the same time, and every one of them that finds a
# missing .pyc compiles it and writes it into site-packages.
#
# Whether that window exists at all depends on which installer built the
# environment, which is why this is not redundant with the line above.
# Measured on numpy 2.4.3 into a fresh venv: `pip install` leaves 406 of 406
# .pyc in place before any test runs, `pip install --no-compile` leaves 0 of
# 406 (uv likewise does not byte-compile unless asked for it). So a
# pip-installed CI environment starts warm, while a uv-built or --no-compile
# venv hands the entire dependency tree to the workers to compile
# concurrently. Doing it once here means no worker has to write into
# site-packages at all.
#
# Measured cost, 5981 .py files under site-packages, on a 2-CPU cpuset:
# 3.0s when already compiled (the CI case -- compileall skips up-to-date
# files, so this is a stat walk), 9.1s from completely cold.
#
# purelib+platlib only -- the dependencies, not the stdlib, which every
# interpreter distribution ships pre-compiled.
#
# Strictly best-effort, and quiet about it: a read-only or system-owned
# site-packages, or a dependency shipping .py files whose syntax this
# interpreter rejects, is not a reason for the test suite not to run. `-q -q`
# suppresses the per-error output that would otherwise scroll past, and the
# exit status is discarded on purpose.
DEP_DIRS="$(
  "$PYTHON" -c 'import sysconfig
paths = sysconfig.get_paths()
seen = []
for key in ("purelib", "platlib"):
    p = paths.get(key)
    if p and p not in seen:
        seen.append(p)
print("\n".join(seen))' 2>/dev/null || true
)"
if [ -n "$DEP_DIRS" ]; then
  echo "▶ pre-compiling dependency bytecode cache"
  printf '%s\n' "$DEP_DIRS" | while IFS= read -r _dep_dir; do
    [ -n "$_dep_dir" ] && [ -d "$_dep_dir" ] || continue
    "$PYTHON" -m compileall -q -q -j 0 -- "$_dep_dir" >/dev/null 2>&1 || true
  done
fi

echo "▶ launching test runner"
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  ${WIN_ENV[@]+"${WIN_ENV[@]}"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONUTF8=1 \
  ${YOUTAB_TEST_PG_DSN:+YOUTAB_TEST_PG_DSN="$YOUTAB_TEST_PG_DSN"} \
  ${YOUTAB_AGENT_RUN_SLOW_PET_TESTS:+YOUTAB_AGENT_RUN_SLOW_PET_TESTS="$YOUTAB_AGENT_RUN_SLOW_PET_TESTS"} \
  ${YOUTAB_AGENT_E2E_BROWSER:+YOUTAB_AGENT_E2E_BROWSER="$YOUTAB_AGENT_E2E_BROWSER"} \
  ${RUNNER_ENV[@]+"${RUNNER_ENV[@]}"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
