"""Repo-wide guard: no production subprocess call decodes child output with the
locale codec (WAVE-25 c1 regression prevention).

``subprocess.run(..., text=True)`` without an explicit ``encoding=`` decodes
with ``locale.getpreferredencoding()`` — cp1252 / cp936 on Windows — which
raises ``UnicodeDecodeError`` on non-ASCII child output (git paths, tool
messages). ``scripts/check-windows-footguns.py`` catches the single-line form;
this AST guard is multi-line-aware, so a call whose ``text=True`` sits on its
own line (as ``tools/working_diff.py`` did, #52649) cannot slip through.

A call that spreads ``**kwargs`` is skipped: the encoding may be injected
conditionally there (e.g. ``cron/scheduler.py`` adds it on win32), which the
static check cannot see — matching the footgun linter's own accept-false-
negatives philosophy for that pattern.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXCLUDED_DIRS = {
    ".git", ".venv", ".venv-qual", ".venv-recorder", "node_modules",
    "__pycache__", ".claude", "vendor", "tests",
}
SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}


def _iter_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        if EXCLUDED_DIRS & set(path.parts):
            continue
        yield path


def _violations_in(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in SUBPROCESS_FUNCS):
            continue
        has_spread = any(kw.arg is None for kw in node.keywords)
        if has_spread:
            continue
        kwargs = {kw.arg: kw for kw in node.keywords if kw.arg}
        texty = any(
            key in kwargs
            and isinstance(kwargs[key].value, ast.Constant)
            and kwargs[key].value.value is True
            for key in ("text", "universal_newlines")
        )
        if not texty or "encoding" in kwargs:
            continue
        out.append(node.lineno)
    return out


def test_no_subprocess_text_true_without_encoding():
    offenders = []
    for path in _iter_python_files():
        for lineno in _violations_in(path):
            rel = path.relative_to(REPO_ROOT).as_posix()
            offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "subprocess text=True/universal_newlines=True without encoding= "
        "(decodes child output with the Windows locale codec — pass "
        'encoding="utf-8", errors="replace"):\n  ' + "\n  ".join(sorted(offenders))
    )
