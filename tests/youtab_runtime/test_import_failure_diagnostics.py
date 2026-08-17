"""The import-failure diagnostics must fire precisely, and must change nothing.

Background
----------
CI run 32003878520 died with ``ModuleNotFoundError: No module named
'numpy._utils'`` while collecting a test file that imports numpy at module
scope, while three sibling files doing the same thing passed in the same job
milliseconds apart. It has never reproduced. ``scripts/import_failure_diagnostics.py``
captures the state a human would need to tell a transient filesystem error apart
from a real missing install, at the moment the failure is observed.

That instrumentation is only worth having if four properties hold, and each one
gets a test here:

1. it fires on an import-shaped failure;
2. it is silent on a green run;
3. it is silent on an ordinary assertion failure -- including one whose message
   and captured stdout deliberately quote a ``ModuleNotFoundError``, which is the
   discrimination that matters;
4. the exit status is byte-identical with and without it, and the failing file is
   not re-run.

Plus the secrecy property: no credential-shaped environment variable may reach
the output, proven with an ``HF_TOKEN``-shaped value planted in the runner's
environment.

Mutation checks
---------------
* Make ``detect_import_failure`` return a signal unconditionally (fire on
  everything) -> ``test_diagnostics_absent_on_a_plain_assertion_failure`` and
  ``test_diagnostics_absent_on_a_green_run`` go red.
* Make the runner zero the return code when it captures diagnostics ->
  ``test_import_shaped_failure_still_fails`` and
  ``test_exit_status_is_identical_with_and_without_diagnostics`` go red.
* Delete the credential filter from ``safe_env_snapshot`` ->
  ``test_an_allowlisted_but_credential_shaped_name_is_still_dropped`` goes red.

The pytest output samples below are verbatim captures from pytest 9.0.2 under
the governed interpreter, not hand-written guesses at the format.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PY = REPO_ROOT / "scripts" / "run_tests_parallel.py"
DIAGNOSTICS_PY = REPO_ROOT / "scripts" / "import_failure_diagnostics.py"

#: Planted in the runner's environment under a credential-shaped name. If this
#: string ever shows up in runner output, the filter has failed.
CREDENTIAL_SENTINEL = "hf_zzz_sentinel_value_that_must_never_be_logged"

#: A module name that cannot exist, used to provoke a real collection-time
#: ``ModuleNotFoundError`` without touching any real dependency. Nothing here
#: breaks, hides, skips or de-collects anything that ships.
ABSENT_MODULE = "youtab_absent_module_for_import_diagnostics"


def _load_diagnostics():
    """Load the diagnostics module by path, the way the runner does.

    ``scripts/`` is not a package, so there is no import name to use. Registering
    the module in ``sys.modules`` before ``exec_module`` is required, not
    cosmetic: ``@dataclass`` resolves ``sys.modules[cls.__module__]`` while
    processing the class and raises ``AttributeError`` when it is absent.
    """
    spec = importlib.util.spec_from_file_location(
        "_import_failure_diagnostics_under_test", DIAGNOSTICS_PY
    )
    assert spec is not None and spec.loader is not None, DIAGNOSTICS_PY
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


diagnostics = _load_diagnostics()


# ── Verbatim pytest 9.0.2 output samples ────────────────────────────────────

SAMPLE_COLLECTION_IMPORT_ERROR = """
==================================== ERRORS ====================================
_______________ ERROR collecting tests/tools/test_wake_word.py _________________
ImportError while importing test module '/home/runner/work/r/tests/tools/test_wake_word.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
/usr/lib/python3.12/importlib/__init__.py:90: in import_module
    return _bootstrap._gcd_import(name[level:], package, level)
tests/tools/test_wake_word.py:5: in <module>
    import numpy as np
E   ModuleNotFoundError: No module named 'numpy._utils'
=========================== short test summary info ============================
ERROR tests/tools/test_wake_word.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 1.95s
"""

SAMPLE_IN_TEST_IMPORT_ERROR = """
=================================== FAILURES ===================================
____________________________________ test_y ____________________________________

    def test_y():
>       from numpy import definitely_not_here_xyz
E       ImportError: cannot import name 'definitely_not_here_xyz' from 'numpy' (/tmp/govenv/lib/python3.12/site-packages/numpy/__init__.py)

test_intest_import.py:2: ImportError
1 failed in 1.97s
"""

#: The discrimination case, and the one that motivated the anchoring: an
#: assertion failure whose message AND captured stdout both read exactly like an
#: import error.
SAMPLE_ASSERTION_QUOTING_AN_IMPORT_ERROR = """
=================================== FAILURES ===================================
____________________________________ test_z ____________________________________

    def test_z():
        print("ModuleNotFoundError: No module named 'numpy._utils'")
>       assert False, "ModuleNotFoundError: No module named 'numpy._utils'"
E       AssertionError: ModuleNotFoundError: No module named 'numpy._utils'
E       assert False

test_assert_mentions.py:3: AssertionError
----------------------------- Captured stdout call -----------------------------
ModuleNotFoundError: No module named 'numpy._utils'
=========================== short test summary info ============================
FAILED test_assert_mentions.py::test_z - AssertionError: ModuleNotFoundError:...
1 failed in 1.76s
"""

#: Nastier still: the failing test prints a whole fake traceback, so the
#: column-0 rule would match if it were not stood down by the "E" gutter.
SAMPLE_ASSERTION_PRINTING_A_FAKE_TRACEBACK = """
=================================== FAILURES ===================================
____________________________________ test_q ____________________________________
>       assert reported == expected
E       AssertionError: assert 'a' == 'b'

----------------------------- Captured stdout call -----------------------------
Traceback (most recent call last):
  File "/app/thing.py", line 3, in <module>
    import numpy
ModuleNotFoundError: No module named 'numpy._utils'
=========================== short test summary info ============================
FAILED test_q.py::test_q - AssertionError: assert 'a' == 'b'
1 failed in 0.31s
"""

#: A plain assertion failure with nothing import-shaped in it at all.
SAMPLE_PLAIN_ASSERTION = """
=================================== FAILURES ===================================
____________________________________ test_a ____________________________________
>       assert 1 == 2
E       assert 1 == 2

test_a.py:2: AssertionError
1 failed in 0.11s
"""

#: The process died before pytest could render anything -- a conftest that fails
#: to import. No "E" gutter anywhere, real traceback header.
SAMPLE_RAW_INTERPRETER_TRACEBACK = """
Traceback (most recent call last):
  File "/tmp/govenv/bin/pytest", line 5, in <module>
    from pytest import console_main
  File "/tmp/govenv/lib/python3.12/site-packages/pytest/__init__.py", line 9, in <module>
    import numpy
ModuleNotFoundError: No module named 'numpy._utils'
"""

#: Only the short summary survives under --tb=no.
SAMPLE_SHORT_SUMMARY_ONLY = """
=========================== short test summary info ============================
FAILED tests/tools/test_wake_word.py - ModuleNotFoundError: No module named 'numpy._utils'
1 failed in 0.42s
"""


# ── Probe fixtures ──────────────────────────────────────────────────────────


def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture(scope="module")
def probes(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    """Three single-file probe directories: import failure, green, assertion.

    Each lives in its own directory and is handed to the runner POSITIONALLY --
    ``--files``/``--paths`` are colon-separated lists, which a Windows absolute
    path splits down the drive letter.
    """
    root = tmp_path_factory.mktemp("import_diagnostics_probes")
    imp = root / "imp"
    imp_off = root / "imp_off"
    imp_call = root / "imp_call"
    green = root / "green"
    assertfail = root / "assertfail"
    for directory in (imp, imp_off, imp_call, green, assertfail):
        directory.mkdir()

    # The marker records one line per IMPORT of the probe module. With
    # --file-retries 0 the file must be imported exactly once: a second line
    # would mean something re-ran it, which the diagnostics are forbidden to do.
    # The diagnostics-off run gets its OWN copy of the probe in its own
    # directory, so it cannot add a line to this marker and blunt that check.
    marker = root / "probe-imports.log"
    _write(
        imp / "test_import_failure_probe.py",
        f"""
        from pathlib import Path

        with Path({str(marker)!r}).open("a", encoding="utf-8") as _fh:
            _fh.write("imported\\n")

        import {ABSENT_MODULE}  # noqa: E402,F401  -- raises at collection


        def test_never_collected():
            assert True
        """,
    )
    _write(
        imp_off / "test_import_failure_probe.py",
        f"""
        import {ABSENT_MODULE}  # noqa: F401  -- raises at collection


        def test_never_collected():
            assert True
        """,
    )
    # An import failure INSIDE a test, not at collection. Needed because a
    # collection error collects zero tests, and the runner's pre-existing
    # "nothing ran anywhere" guard would fail that run for its own reasons --
    # which would mask a green-washing regression. Here one test is collected
    # and fails, so the exit code can only come from the failure itself.
    _write(
        imp_call / "test_import_failure_in_test_probe.py",
        f"""
        def test_import_fails_inside_the_test():
            import {ABSENT_MODULE}  # noqa: F401

            assert {ABSENT_MODULE}
        """,
    )
    _write(
        green / "test_green_probe.py",
        """
        def test_passes():
            assert True
        """,
    )
    _write(
        assertfail / "test_assertion_probe.py",
        """
        def test_fails_while_quoting_an_import_error():
            print("ModuleNotFoundError: No module named 'numpy._utils'")
            assert False, "ModuleNotFoundError: No module named 'numpy._utils'"
        """,
    )
    return SimpleNamespace(
        imp=imp,
        imp_off=imp_off,
        imp_call=imp_call,
        green=green,
        assertfail=assertfail,
        marker=marker,
    )


def _run_runner(
    probe_dir: Path, *extra: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the real per-file runner over one probe directory.

    ``--file-retries 0`` on purpose: the retry is a pre-existing feature and
    would re-run the probe by itself, which would make the "nothing re-ran the
    file" assertion meaningless.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER_PY),
            str(probe_dir),
            "--file-retries",
            "0",
            "-j",
            "1",
            "-q",
            "-p",
            "no:cacheprovider",
            *extra,
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env=env,
        check=False,
    )


@pytest.fixture(scope="module")
def import_failure_run(probes: SimpleNamespace) -> subprocess.CompletedProcess[str]:
    """Import-shaped failure, diagnostics ON, credential planted."""
    return _run_runner(probes.imp, env_extra={"HF_TOKEN": CREDENTIAL_SENTINEL})


@pytest.fixture(scope="module")
def import_failure_run_no_diagnostics(
    probes: SimpleNamespace,
) -> subprocess.CompletedProcess[str]:
    """The same failure with the capture switched off.

    Its own probe directory (byte-identical failure, no marker write) so this
    run cannot interfere with the "imported exactly once" assertion.
    """
    return _run_runner(probes.imp_off, "--no-import-diagnostics")


@pytest.fixture(scope="module")
def import_failure_in_test_run(
    probes: SimpleNamespace,
) -> subprocess.CompletedProcess[str]:
    """An import failure inside a test body, so tests ARE collected."""
    return _run_runner(probes.imp_call)


@pytest.fixture(scope="module")
def green_run(probes: SimpleNamespace) -> subprocess.CompletedProcess[str]:
    return _run_runner(probes.green)


@pytest.fixture(scope="module")
def assertion_failure_run(
    probes: SimpleNamespace,
) -> subprocess.CompletedProcess[str]:
    return _run_runner(probes.assertfail)


# ── 1. Diagnostics fire on an import-shaped failure ─────────────────────────


def test_diagnostics_are_emitted_on_a_simulated_import_failure(
    import_failure_run: subprocess.CompletedProcess[str],
) -> None:
    out = import_failure_run.stdout
    assert ABSENT_MODULE in out, (
        "the probe did not fail the way this test needs it to; the run proves "
        f"nothing:\n{out}"
    )
    assert diagnostics.DIAGNOSTICS_HEADER in out, (
        f"no diagnostics section for an import-shaped failure:\n{out}"
    )
    assert diagnostics.MARKER in out, (
        f"the failing file's output carries no pointer to the capture:\n{out}"
    )
    assert "pytest-traceback-exception-line" in out, (
        f"the capture does not say which rule matched:\n{out}"
    )


def test_the_capture_records_every_required_field(
    import_failure_run: subprocess.CompletedProcess[str],
) -> None:
    """Every field the capture exists to record is actually in the output.

    Without this the diagnostic could quietly degrade to a header and a shrug.
    """
    out = import_failure_run.stdout
    required = [
        # interpreter identity
        "sys.executable",
        "version_info",
        "implementation",
        "prefix",
        "search path entries",
        # the module and its distribution
        "module looked up",
        "distribution",
        "module resolution",
        # the directory whose cached listing the failed lookup would have used
        "cached-listing directory",
        "os.stat",
        "os.listdir",
        "listing sample",
        "__pycache__",
        # filesystem / error state
        "disk",
        "statvfs",
        "mount",
        "access",
        "RLIMIT_NOFILE",
        "open fds",
        # runner identity
        "platform",
        "machine / arch",
        "libc",
        "os.name / sys.platform",
        "cpu_count",
        "env policy",
    ]
    missing = [field for field in required if field not in out]
    assert not missing, f"capture is missing {missing}:\n{out}"


def test_the_failing_file_is_not_re_run(
    import_failure_run: subprocess.CompletedProcess[str],
    probes: SimpleNamespace,
) -> None:
    """Diagnostics must observe, never retry.

    The probe appends a line every time it is imported. With ``--file-retries 0``
    exactly one line may exist; two would mean the capture re-ran the file.
    """
    assert probes.marker.is_file(), (
        "the probe never got imported at all, so this test proves nothing: "
        f"{probes.marker}"
    )
    imports = probes.marker.read_text(encoding="utf-8").splitlines()
    assert imports == ["imported"], (
        f"the probe file was imported {len(imports)} times; something re-ran it "
        f"even though --file-retries 0 was passed:\n{import_failure_run.stdout}"
    )


def test_import_shaped_failure_still_fails(
    import_failure_run: subprocess.CompletedProcess[str],
) -> None:
    """A captured failure is still a failure."""
    assert import_failure_run.returncode != 0, (
        "the runner exited 0 on an import-shaped failure -- the diagnostics "
        f"laundered a failure into green:\n{import_failure_run.stdout}"
    )


def test_an_in_test_import_failure_is_captured_and_still_fails(
    import_failure_in_test_run: subprocess.CompletedProcess[str],
) -> None:
    """The no-green-washing property, on a run whose exit code can only come
    from the failing test itself.

    The collection-error probe above collects zero tests, so the runner's
    pre-existing "nothing ran anywhere" guard would fail that run even if the
    diagnostics had zeroed the return code. This probe collects one test and
    fails it, closing that hole.

    Mutation check: set ``rc = 0`` next to the pointer append in
    ``_run_one_file_once`` and this goes red.
    """
    out = import_failure_in_test_run.stdout
    assert "1 tests passed, 1 failed" in out or "0 tests passed, 1 failed" in out, (
        f"the probe did not produce exactly one collected, failing test:\n{out}"
    )
    assert diagnostics.DIAGNOSTICS_HEADER in out, (
        f"an import failure inside a test body was not captured:\n{out}"
    )
    assert import_failure_in_test_run.returncode != 0, (
        "the runner exited 0 on a failing test whose failure was an import "
        f"error -- the diagnostics laundered a failure into green:\n{out}"
    )


# ── 2. Silent on a green run ────────────────────────────────────────────────


def test_diagnostics_absent_on_a_green_run(
    green_run: subprocess.CompletedProcess[str],
) -> None:
    out = green_run.stdout
    assert green_run.returncode == 0, f"the green probe did not pass:\n{out}"
    assert "1 tests passed" in out, (
        f"the green probe did not actually run a test; vacuous:\n{out}"
    )
    assert diagnostics.DIAGNOSTICS_HEADER not in out, (
        f"diagnostics fired on a passing run:\n{out}"
    )
    assert diagnostics.MARKER not in out, (
        f"a diagnostics pointer appeared on a passing run:\n{out}"
    )


# ── 3. Silent on a non-import failure (the discrimination) ──────────────────


def test_diagnostics_absent_on_a_plain_assertion_failure(
    assertion_failure_run: subprocess.CompletedProcess[str],
) -> None:
    """An assertion failure must not trigger a capture.

    The probe stacks the deck: its assertion message and its captured stdout
    both spell out a ``ModuleNotFoundError``. Detection anchors on the position
    of the exception name, not on the words appearing somewhere, so this stays
    silent.
    """
    out = assertion_failure_run.stdout
    assert assertion_failure_run.returncode != 0, (
        f"the assertion probe did not fail:\n{out}"
    )
    assert "AssertionError" in out and "No module named 'numpy._utils'" in out, (
        "the assertion probe's misleading text is not in the output, so the "
        f"discrimination was never actually challenged:\n{out}"
    )
    assert diagnostics.DIAGNOSTICS_HEADER not in out, (
        f"diagnostics fired on an ordinary assertion failure:\n{out}"
    )
    assert diagnostics.MARKER not in out, (
        f"a diagnostics pointer appeared on an ordinary assertion failure:\n{out}"
    )


# ── 4. Exit status is unchanged by the instrumentation ──────────────────────


def test_exit_status_is_identical_with_and_without_diagnostics(
    import_failure_run: subprocess.CompletedProcess[str],
    import_failure_run_no_diagnostics: subprocess.CompletedProcess[str],
) -> None:
    with_diag = import_failure_run
    without_diag = import_failure_run_no_diagnostics
    assert with_diag.returncode == without_diag.returncode, (
        f"exit status changed with diagnostics: {with_diag.returncode} with, "
        f"{without_diag.returncode} without"
    )
    assert with_diag.returncode != 0, "both runs passed; the comparison is vacuous"
    assert diagnostics.DIAGNOSTICS_HEADER in with_diag.stdout
    assert diagnostics.DIAGNOSTICS_HEADER not in without_diag.stdout, (
        "--no-import-diagnostics did not turn the capture off:\n"
        f"{without_diag.stdout}"
    )


# ── 5. No credential-shaped value reaches the output ───────────────────────


def test_no_credential_shaped_value_reaches_the_output(
    import_failure_run: subprocess.CompletedProcess[str],
) -> None:
    """An ``HF_TOKEN``-shaped variable is in the runner's environment, and must
    appear nowhere in the capture -- neither its value nor its name."""
    out = import_failure_run.stdout
    assert diagnostics.DIAGNOSTICS_HEADER in out, (
        f"no capture happened, so this test proves nothing:\n{out}"
    )
    assert CREDENTIAL_SENTINEL not in out, (
        "a credential-shaped env var's VALUE reached the diagnostics output"
    )
    assert "HF_TOKEN" not in out, (
        f"a credential-shaped env var's NAME reached the diagnostics output:\n{out}"
    )


def test_an_allowlisted_but_credential_shaped_name_is_still_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The filter is the second gate, not the only gate.

    Even if a credential-shaped name is explicitly allowlisted -- the mistake a
    future edit would make -- its value must not be read.
    """
    monkeypatch.setenv("HF_TOKEN", CREDENTIAL_SENTINEL)
    monkeypatch.setenv("RUNNER_OS", "Linux")
    snapshot = dict(
        diagnostics.safe_env_snapshot({"HF_TOKEN", "RUNNER_OS"})
    )
    assert snapshot == {"RUNNER_OS": "Linux"}, (
        f"credential-shaped name survived an allowlist entry: {sorted(snapshot)}"
    )


def test_credential_policy_is_a_superset_of_the_conftest_policy() -> None:
    """One policy, not two.

    ``tests/conftest.py`` owns the repo's notion of a credential-shaped env var
    name (the ``_TOKEN``-suffix family). The diagnostics predicate is
    deliberately broader -- substring rather than suffix -- and this test binds
    it to conftest's live constants so the two cannot drift into disagreement.
    """
    from tests.conftest import _CREDENTIAL_NAMES, _CREDENTIAL_SUFFIXES

    assert _CREDENTIAL_NAMES and _CREDENTIAL_SUFFIXES, (
        "conftest's credential constants are empty -- this test would be vacuous"
    )
    assert diagnostics.looks_like_credential("HF_TOKEN"), (
        "the HF_TOKEN shape this whole exercise is about is not caught"
    )

    disagreements = sorted(
        name for name in _CREDENTIAL_NAMES if not diagnostics.looks_like_credential(name)
    )
    assert not disagreements, (
        "conftest treats these names as credential-shaped but the diagnostics "
        f"filter does not: {disagreements}. Widen _CREDENTIAL_SUBSTRINGS in "
        "scripts/import_failure_diagnostics.py."
    )

    suffix_disagreements = sorted(
        suffix
        for suffix in _CREDENTIAL_SUFFIXES
        if not diagnostics.looks_like_credential(f"PROBE{suffix}")
    )
    assert not suffix_disagreements, (
        "conftest's credential suffixes are not all covered by the diagnostics "
        f"filter: {suffix_disagreements}"
    )

    # And the allowlist itself must be clean today.
    dirty = sorted(
        name
        for name in diagnostics._ENV_ALLOWLIST
        if diagnostics.looks_like_credential(name)
    )
    assert not dirty, f"credential-shaped names sit in the allowlist: {dirty}"


def test_the_only_environment_read_is_the_filtered_one() -> None:
    """No second door into ``os.environ``.

    A grep over the module's own source: exactly one ``os.environ`` reference may
    exist, and it is the filtered read inside ``safe_env_snapshot``. This is the
    cheapest possible guarantee that no future field quietly dumps the
    environment.
    """
    source = DIAGNOSTICS_PY.read_text(encoding="utf-8")
    reads = [line.strip() for line in source.splitlines() if "os.environ" in line]
    assert len(reads) == 1, (
        f"expected exactly one os.environ reference, found {len(reads)}: {reads}"
    )
    assert reads[0] == "value = os.environ.get(name)", reads


# ── Detection unit tests ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sample", "expected_module", "expected_rule"),
    [
        (
            SAMPLE_COLLECTION_IMPORT_ERROR,
            "numpy._utils",
            "pytest-traceback-exception-line",
        ),
        (
            SAMPLE_IN_TEST_IMPORT_ERROR,
            "numpy",
            "pytest-traceback-exception-line",
        ),
        (
            SAMPLE_RAW_INTERPRETER_TRACEBACK,
            "numpy._utils",
            "raw-interpreter-traceback",
        ),
        (
            SAMPLE_SHORT_SUMMARY_ONLY,
            "numpy._utils",
            "pytest-short-summary-line",
        ),
    ],
)
def test_detects_import_shaped_output(
    sample: str, expected_module: str, expected_rule: str
) -> None:
    signal = diagnostics.detect_import_failure(sample)
    assert signal is not None, f"missed an import-shaped failure:\n{sample}"
    assert signal.module == expected_module
    assert signal.rule == expected_rule


@pytest.mark.parametrize(
    "sample",
    [
        SAMPLE_PLAIN_ASSERTION,
        SAMPLE_ASSERTION_QUOTING_AN_IMPORT_ERROR,
        SAMPLE_ASSERTION_PRINTING_A_FAKE_TRACEBACK,
        "",
        "1 passed in 0.10s\n",
    ],
    ids=[
        "plain-assertion",
        "assertion-quoting-an-import-error",
        "assertion-printing-a-fake-traceback",
        "empty",
        "green",
    ],
)
def test_does_not_detect_non_import_output(sample: str) -> None:
    """Mutation check: return a signal unconditionally and this goes red."""
    assert diagnostics.detect_import_failure(sample) is None, (
        f"fired on output that is not an import failure:\n{sample}"
    )


def test_collection_and_call_failures_are_distinguished() -> None:
    collection = diagnostics.detect_import_failure(SAMPLE_COLLECTION_IMPORT_ERROR)
    call = diagnostics.detect_import_failure(SAMPLE_IN_TEST_IMPORT_ERROR)
    assert collection is not None and call is not None
    assert collection.at_collection is True
    assert call.at_collection is False
    assert call.attribute == "definitely_not_here_xyz"
    assert call.reported_path is not None and call.reported_path.endswith(
        "numpy/__init__.py"
    )


# ── Resolution walk: filesystem only, no imports ───────────────────────────


def test_the_capture_reports_the_parent_package_directory() -> None:
    """For ``a.b.c`` the report must show ``a/b`` -- the directory whose cached
    listing the failed lookup for ``c`` would have come from.

    Anchored on a stdlib package (``email.mime``) rather than numpy so the test
    does not depend on the dependency the flake happens to involve.
    """
    output = "E   ModuleNotFoundError: No module named 'email.mime.absent_xyz'\n"
    result = diagnostics.capture(output, source="tests/probe.py")
    assert result is not None
    _signal, report = result
    assert "cached-listing directory (parent package)" in report
    assert os.path.join("email", "mime") in report
    assert re.search(r"os\.listdir\s+ok, \d+ entries", report), report
    assert re.search(r"entries matching 'absent_xyz'\s+NONE", report), report


def test_the_capture_does_not_import_the_module_it_reports_on() -> None:
    """Resolution is a filesystem walk. Importing the failed module inside the
    runner would be a side effect on the process observing the failure -- and
    would also mask the interesting case, where the import works on the retry."""
    assert "tabnanny" not in sys.modules, (
        "pick a different never-imported module for this test"
    )
    output = "E   ModuleNotFoundError: No module named 'tabnanny.absent_xyz'\n"
    result = diagnostics.capture(output, source="tests/probe.py")
    assert result is not None
    assert "tabnanny" not in sys.modules, (
        "the diagnostics imported the module it was reporting on"
    )


def test_a_genuinely_missing_module_says_so_and_a_present_one_says_that() -> None:
    """The verdict must not read like a fix in either direction.

    ``email.mime.text`` is the shape of the real flake: an import that failed for
    a module which is demonstrably on disk. That is the case where the capture
    has to be most careful with its wording -- "it resolves now" is evidence
    about the filesystem, not a repair.
    """
    present = diagnostics.capture(
        "E   ModuleNotFoundError: No module named 'email.mime.text'\n",
        source="tests/probe.py",
    )
    absent = diagnostics.capture(
        f"E   ModuleNotFoundError: No module named '{ABSENT_MODULE}'\n",
        source="tests/probe.py",
    )
    assert present is not None and absent is not None
    assert "resolves on disk NOW" in present[1]
    assert "still unresolved" in present[1], (
        "the verdict for a transient-looking failure must not imply a fix"
    )
    assert "genuinely absent from every search path" in absent[1]


def test_capture_never_raises_on_hostile_input() -> None:
    """A diagnostic that crashes the runner would be worse than no diagnostic."""
    for hostile in ("E   ImportError: \x00\x01", "E   ImportError: " + "x" * 20000):
        assert diagnostics.capture(hostile, source="tests/probe.py") is not None
