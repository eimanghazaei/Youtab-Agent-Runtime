"""Exercise generated argv and real shell tools with option-like user data."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tools.file_operations import ShellFileOperations
from tools.environments.local import _apply_windows_msys_bash_env_defaults


@pytest.fixture
def bash():
    if os.name == "nt":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        executable = str(candidate) if candidate.is_file() else None
    else:
        executable = shutil.which("bash")
    if not executable:
        pytest.skip("Bash is required for shell file operations")
    return executable


class BashEnvironment:
    def __init__(self, bash, root, prefix=""):
        self.bash = bash
        self.cwd = str(root)
        self.prefix = prefix
        self.executions = []

    def execute(self, command, cwd=None, timeout=None, stdin_data=None):
        run_env = os.environ.copy()
        _apply_windows_msys_bash_env_defaults(run_env)
        completed = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", self.prefix + command],
            cwd=cwd or self.cwd,
            input=stdin_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            env=run_env,
            timeout=timeout or 10,
        )
        self.executions.append((command, completed.stdout, completed.returncode))
        return {"output": completed.stdout, "returncode": completed.returncode}


@pytest.mark.parametrize("method,tool", [("_search_with_rg", "rg"), ("_search_with_grep", "grep")])
@pytest.mark.parametrize("pattern", ["--pre=/bin/sh", "-e", "--", "-needle.*", "quote'$(synthetic)"])
def test_actual_search_argv_keeps_pattern_and_path_as_data(bash, tmp_path, method, tool, pattern):
    # Replace only the search binary with an inert shell function. Execute the
    # generated shell command itself, including escaping and its head pipeline.
    env = BashEnvironment(bash, tmp_path, f'{tool}() {{ printf "%s\\0" "$@"; }}; ')
    ops = ShellFileOperations(env)
    getattr(ops, method)(pattern, "--pre=/synthetic/program", "--include", 7, 2, "content", 1)
    command, output, exit_code = env.executions[-1]
    argv = output.rstrip("\0").split("\0")
    assert exit_code == 0
    assert argv[-4:] == ["-e", pattern, "--", "--pre=/synthetic/program"]
    assert f"{'--glob' if tool == 'rg' else '--include'}=--include" in argv
    assert "| head -n 209" in command


def test_file_search_both_rg_variants_delimit_paths(bash, tmp_path):
    env = BashEnvironment(bash, tmp_path, 'rg() { printf "%s\\0" "$@"; }; ')
    ops = ShellFileOperations(env)
    # NUL-only output is nonempty, so explicitly drive the retry with an empty
    # first result while retaining the real argv observed by Bash.
    original = env.execute

    def execute(command, **kwargs):
        result = original(command, **kwargs)
        return {"output": "", "returncode": result["returncode"]}

    env.execute = execute
    ops._search_files_rg("--pre=synthetic", "--no-ignore", 5, 0)
    assert len(env.executions) == 2
    for _, output, exit_code in env.executions:
        argv = output.rstrip("\0").split("\0")
        assert exit_code == 0
        assert argv[-3:] == ["--glob=*--pre=synthetic", "--", "--no-ignore"]


@pytest.mark.parametrize("method,tool", [("_search_with_rg", "rg"), ("_search_with_grep", "grep")])
@pytest.mark.parametrize("mode", ["content", "count", "files_only"])
def test_real_search_preserves_regex_modes_and_pagination(bash, tmp_path, method, tool, mode):
    env = BashEnvironment(bash, tmp_path)
    if env.execute(f"command -v {tool}")["returncode"]:
        pytest.skip(f"{tool} is not available")
    root = tmp_path / "--no-ignore"
    root.mkdir()
    (root / "sample.txt").write_text("-needle one\nother\n-needle two\n", encoding="utf-8")
    (root / "excluded.py").write_text("-needle ignored\n", encoding="utf-8")
    ops = ShellFileOperations(env)
    result = getattr(ops, method)("-needle.*", root.name, "*.txt", 1, 1 if mode == "content" else 0, mode, 0)
    assert result.error is None
    if mode == "content":
        assert [(m.line_number, m.content) for m in result.matches] == [(3, "-needle two")]
        assert result.total_count == 2
    elif mode == "count":
        assert list(result.counts.values()) == [2]
    else:
        assert len(result.files) == 1
        assert result.files[0].endswith("sample.txt")


@pytest.mark.parametrize("method,tool", [("_search_with_rg", "rg"), ("_search_with_grep", "grep")])
def test_real_search_matches_execution_option_as_literal_pattern(bash, tmp_path, method, tool):
    env = BashEnvironment(bash, tmp_path)
    if env.execute(f"command -v {tool}")["returncode"]:
        pytest.skip(f"{tool} is not available")
    (tmp_path / "sample.txt").write_text("--pre=/bin/sh\n", encoding="utf-8")
    result = getattr(ShellFileOperations(env), method)("--pre=/bin/sh", "sample.txt", None, 10, 0, "content", 0)
    assert result.error is None
    assert [m.content for m in result.matches] == ["--pre=/bin/sh"]


@pytest.mark.parametrize("root_name", ["-delete", "!", "("])
def test_find_fallback_treats_expression_like_root_as_directory(bash, tmp_path, monkeypatch, root_name):
    root = tmp_path / root_name
    root.mkdir()
    file = root / "sample.txt"
    file.write_text("synthetic\n", encoding="utf-8")
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("keep\n", encoding="utf-8")
    ops = ShellFileOperations(BashEnvironment(bash, tmp_path))
    monkeypatch.setattr(ops, "_has_command", lambda tool: tool == "find")
    result = ops.search("*.txt", path=root_name, target="files")
    assert result.error is None
    assert result.files == [f"./{root_name}/sample.txt"]
    assert file.read_text(encoding="utf-8") == "synthetic\n"
    assert sibling.read_text(encoding="utf-8") == "keep\n"


def test_read_helpers_treat_option_like_filename_as_file(bash, tmp_path):
    (tmp_path / "--help").write_text("first\nsecond\n", encoding="utf-8")
    ops = ShellFileOperations(BashEnvironment(bash, tmp_path))
    assert ops.read_file("--help", offset=2, limit=1).content == "2|second\n3|"
    assert ops.read_file_raw("--help").content == "first\nsecond\n"
    assert ops._detect_file_line_ending("--help") == "\n"
    assert ops._file_has_bom("--help") is False


def test_read_sed_filename_cannot_be_an_extra_script(bash, tmp_path):
    name = "-e1eprintf injected"
    (tmp_path / name).write_text("synthetic content\n", encoding="utf-8")
    result = ShellFileOperations(BashEnvironment(bash, tmp_path)).read_file(name)
    assert result.error is None
    assert "synthetic content" in result.content
    assert "injected" not in result.content


def test_read_suggestions_accept_option_like_directory(bash, tmp_path):
    directory = tmp_path / "--color=always"
    directory.mkdir()
    (directory / "notes.txt").write_text("synthetic\n", encoding="utf-8")
    result = ShellFileOperations(BashEnvironment(bash, tmp_path)).read_file("--color=always/note.txt")
    assert result.similar_files == [os.path.join("--color=always", "notes.txt")]
