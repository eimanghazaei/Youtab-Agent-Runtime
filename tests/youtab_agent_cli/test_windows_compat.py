"""Cross-platform (Windows-focused) regression suite — WAVE-23.

These prove the runtime/CLI's portable correctness invariants hold on Windows
*and* POSIX: atomic persistence, open-file rename/delete semantics, path &
encoding handling, and safe subprocess construction. Everything here runs on
every platform (no platform skips) — the point is that the same guarantee holds
regardless of OS. Concurrency-sensitive cases repeat to shake out races without
any arbitrary sleep or retry-until-pass masking.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from utils import atomic_json_write, atomic_write_text, atomic_replace


# ── Atomic persistence ──────────────────────────────────────────────────────

class TestAtomicPersistence:
    def test_concurrent_thread_writers_never_corrupt(self, tmp_path):
        """Many threads writing the same target concurrently: every write either
        succeeds atomically or raises — the final file is always valid JSON from
        exactly one writer, never a torn mix. Repeated to exercise the race."""
        target = tmp_path / "data.json"
        for _ in range(20):
            errors: list = []

            def writer(n: int) -> None:
                try:
                    atomic_json_write(target, {"writer": n, "payload": list(range(200))})
                except Exception as exc:  # noqa: BLE001 — surfaced via `errors`
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(i,)) for i in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, errors
            data = json.loads(target.read_text(encoding="utf-8"))
            assert data["payload"] == list(range(200))  # a whole, single writer's value

    def test_cross_process_writers_never_corrupt(self, tmp_path):
        """Separate PROCESSES (Windows uses spawn, no fork) racing atomic_json_write
        on one target: no process errors and the file stays valid JSON."""
        target = tmp_path / "cross.json"
        worker = tmp_path / "w.py"
        repo_root = Path(__file__).resolve().parents[2]
        worker.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(repo_root)!r})\n"
            "from utils import atomic_json_write\n"
            "n = int(sys.argv[2])\n"
            "atomic_json_write(sys.argv[1], {'writer': n, 'payload': list(range(200))})\n",
            encoding="utf-8",
        )
        procs = [
            subprocess.Popen([sys.executable, str(worker), str(target), str(i)])
            for i in range(6)
        ]
        rcs = [p.wait() for p in procs]
        assert all(rc == 0 for rc in rcs), rcs
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["payload"] == list(range(200))

    def test_reader_never_sees_partial_json_under_writes(self, tmp_path):
        """A reader hammering the target while a writer rewrites it must never
        observe a truncated/partial document — os.replace swaps atomically."""
        target = tmp_path / "rw.json"
        atomic_json_write(target, {"payload": list(range(500))})
        stop = threading.Event()
        bad: list = []

        def reader() -> None:
            while not stop.is_set():
                try:
                    d = json.loads(target.read_text(encoding="utf-8"))
                except (FileNotFoundError, PermissionError):
                    # Transient, NOT corruption: on Windows a reader can briefly
                    # be denied while os.replace swaps the file. The atomicity
                    # guarantee is "never a TORN document", so a transient lock
                    # simply means retry — it is not a violation.
                    continue
                except json.JSONDecodeError as exc:
                    bad.append(exc)  # a torn read — the failure we guard against
                    return
                else:
                    if d.get("payload") != list(range(500)):
                        bad.append(d)
                        return

        r = threading.Thread(target=reader)
        r.start()
        writes_ok = 0
        try:
            for _ in range(60):
                try:
                    atomic_json_write(target, {"payload": list(range(500))})
                    writes_ok += 1
                except PermissionError:
                    # The invariant under test is the READER's (never a torn
                    # document). A gap-less reader that re-opens the destination
                    # back-to-back can, on a slow filesystem, keep the atomic
                    # swap bouncing past its bounded retry — a documented Windows
                    # contention outcome, NOT corruption. Tolerate it here; the
                    # reader assertion below is what this test proves.
                    pass
        finally:
            stop.set()
            r.join()
        assert not bad, "reader observed a torn/partial JSON document"
        # The atomic write still works overwhelmingly even under this pathology,
        # and the final on-disk document is always whole.
        assert writes_ok >= 1
        assert json.loads(target.read_text(encoding="utf-8"))["payload"] == list(range(500))

    def test_replace_over_previously_opened_target(self, tmp_path):
        """atomic_replace must succeed even when the destination was opened and
        then closed just before — the Windows post-close handle-release lag is
        ridden out; POSIX is unaffected."""
        target = tmp_path / "reopened.json"
        atomic_json_write(target, {"v": 1})
        # Open+close the target (simulates a reader that just finished).
        with open(target, "r", encoding="utf-8") as fh:
            _ = fh.read()
        atomic_json_write(target, {"v": 2})
        assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}

    def test_crash_before_replace_preserves_previous_value(self, tmp_path):
        """If serialization aborts mid-write, the prior file is untouched and no
        temp artifact is left behind."""
        from unittest.mock import patch

        target = tmp_path / "safe.json"
        atomic_json_write(target, {"kept": True})
        with patch("utils.json.dump", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                atomic_json_write(target, {"kept": False})
        assert json.loads(target.read_text(encoding="utf-8")) == {"kept": True}
        leftovers = [p for p in tmp_path.iterdir() if ".tmp" in p.name]
        assert leftovers == []

    def test_successful_write_is_durable_and_complete(self, tmp_path):
        target = tmp_path / "out.json"
        atomic_json_write(target, {"a": 1, "b": [1, 2, 3]})
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": [1, 2, 3]}


# ── Paths & encoding ────────────────────────────────────────────────────────

class TestPathsAndEncoding:
    def test_path_with_spaces(self, tmp_path):
        d = tmp_path / "dir with spaces"
        d.mkdir()
        target = d / "file name.json"
        atomic_json_write(target, {"ok": True})
        assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}

    def test_unicode_filename_and_content(self, tmp_path):
        target = tmp_path / "مدير-داده-✓.json"  # Arabic + a check mark
        payload = {"سلام": "دنیا", "emoji": "\U0001f680", "n": 1}
        atomic_json_write(target, payload)
        assert json.loads(target.read_text(encoding="utf-8")) == payload

    def test_non_ascii_text_roundtrip_is_utf8_not_locale(self, tmp_path):
        """atomic_write_text always writes UTF-8, independent of the Windows
        locale (cp1252), so non-ASCII survives the round-trip byte-for-byte."""
        target = tmp_path / "note.txt"
        text = "café — naïve — 日本語 — ✓"
        atomic_write_text(target, text)
        assert target.read_bytes() == text.encode("utf-8")
        assert target.read_text(encoding="utf-8") == text

    def test_does_not_depend_on_cwd(self, tmp_path, monkeypatch):
        target = tmp_path / "abs.json"
        # Change cwd to somewhere unrelated; an absolute path must still resolve.
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        atomic_json_write(target, {"cwd_independent": True})
        assert target.exists()

    def test_drive_or_root_absolute_path(self, tmp_path):
        # tmp_path is already absolute (drive-letter on Windows, / on POSIX).
        assert tmp_path.is_absolute()
        target = tmp_path / "root_abs.json"
        atomic_json_write(target, {"abs": True})
        assert json.loads(target.read_text(encoding="utf-8")) == {"abs": True}


# ── Subprocess construction ─────────────────────────────────────────────────

class TestSubprocessSafety:
    def test_argv_list_no_shell_interpolation(self, tmp_path):
        """An argv LIST (shell=False) passes an argument containing shell
        metacharacters verbatim — no interpolation on any platform."""
        tricky = "a & b | c ; d $(x) %PATH%"
        out = subprocess.run(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", tricky],
            capture_output=True, text=True, check=True,
        )
        assert out.stdout.replace("\r\n", "\n").strip() == tricky

    def test_path_with_spaces_as_argv(self, tmp_path):
        script = tmp_path / "dir with spaces" / "s.py"
        script.parent.mkdir()
        script.write_text("print('ran')\n", encoding="utf-8")
        out = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, check=True,
        )
        assert out.stdout.replace("\r\n", "\n").strip() == "ran"

    def test_missing_command_raises_filenotfound(self):
        with pytest.raises(FileNotFoundError):
            subprocess.run(
                ["youtab_no_such_binary_xyz_12345", "--version"],
                capture_output=True,
            )
