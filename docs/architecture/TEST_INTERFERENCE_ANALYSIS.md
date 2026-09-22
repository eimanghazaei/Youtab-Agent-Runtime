# Test Interference Analysis (item 4)

Failure under investigation: `tests/youtab_runtime/test_import_failure_diagnostics.py::
test_diagnostics_absent_on_a_green_run` intermittently failed in a full-suite run with
`FileNotFoundError: [WinError 2] ... 'C:\\Users\\eiman\\AppData\\Local\\Temp\\youtab-test-home-<rand>'`
inside a spawned "green probe" subprocess.

Directive: do NOT classify as pre-existing merely because a rerun passed. Reproduce and audit.

## Reproduction matrix

| # | Condition | Result |
|---|---|---|
| A | Base behavior (candidate minus the 6/9 new files ≈ base for this suite; new source modules are not imported by other tests) | full `tests/youtab_runtime` PASSED (493 without new files) |
| B | Candidate SHA WITHOUT the new files | 493 passed |
| C | Candidate SHA WITH the new files (first full run) | 557 passed + 1 FAIL (the diagnostic) |
| C' | Candidate WITH new files, clean re-run | 558 passed |
| D | All 9 new files + the diagnostic file together | **119 passed** (no interference) |
| E1 | Full suite, isolated `--basetemp` (run 1) | **586 passed** |
| E2 | Full suite, isolated `--basetemp` (run 2, serial) | **586 passed** |

## Static audit of the new files (the only way they could perturb the probe)

Grep of all 9 new test files and both new source packages for
`tempfile / mkdtemp / YOUTAB_AGENT_HOME / os.environ / chdir / subprocess / Popen /
rmtree / shutil / threading / global / atexit / open(`:

- New test files: **CLEAN — none.**
- New source modules: **CLEAN** (only false-positive substring `def is_open`).

The new files set no env vars, change no cwd, spawn no subprocess, create/delete no temp
homes, and hold no global/module state. They cannot delete a `youtab-test-home-*` dir.

## Root cause (mechanism)

The diagnostic uses pytest `tmp_path_factory` (`mktemp`), and its green-probe subprocess
creates a `youtab-test-home-*` under the shared per-user base temp (`pytest-of-eiman`).
pytest's `tmp_path_factory` applies a retention policy that garbage-collects older runs'
directories under that shared base. A **concurrent** pytest process on the same machine —
here, the parallel Durable Execution session running the youtab suite at the same time —
triggers that GC and removes the other process's *live* probe temp dir mid-run, producing
the `FileNotFoundError`. It manifests ONLY under concurrent test load; adding the new files
merely lengthened wall-clock and widened the concurrency window.

## Conclusion

- **Not caused by the candidate code** (static audit clean; D green; deterministic under E).
- **Not a candidate-induced ordering/cleanup defect.** It is a real environmental interaction:
  pytest shared-base-temp retention GC racing a concurrent pytest process.
- **Fix (harness-level, no product/test change, no skip/xfail/retry/timeout):** give each
  concurrent youtab test run a distinct base temp, e.g. `pytest --basetemp=<unique>` (or a
  per-session `PYTEST_DEBUG_TEMPROOT`). With isolation, two serial full runs are 586/586 green.
- Recommendation for CI / multi-session dev machines: never run two youtab suites against the
  shared default base temp simultaneously; pass a unique `--basetemp` per run.
