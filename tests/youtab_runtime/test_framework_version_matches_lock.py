"""The router framework under test is the one production runs.

This is not hygiene, it is the root cause of two defects in this branch. The
production image installs from ``uv.lock`` via ``uv sync --frozen``; the
``python-security`` job installed ``.[dev]``, which pins Starlette but not
FastAPI -- the core dependency is ``fastapi>=0.104.0,<1``, so pip resolved
whatever was newest.

The two versions do not agree about the router. A newer FastAPI keeps an
included router and its prefix in a wrapper object instead of flattening its
routes into the parent, so a route walker written against one sees 294
route+methods and against the other sees 136. The authorization registry that
is being built compares against exactly this walk, and a registry validated
against a router the deployment does not have would be checking the wrong
surface -- confidently.

So the version is asserted rather than assumed, against the lock rather than
against a number written here, because a constant in a test is one more thing
that can drift from the lock it is supposed to mirror.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LOCK = REPO / "uv.lock"
PYPROJECT = REPO / "pyproject.toml"

#: Packages whose version changes what a route walk returns.
_ROUTER_PACKAGES = ("fastapi", "starlette")

#: Everything the served application is built on. Uvicorn does not change the
#: route walk, but it is the server the image runs, so drifting it is the same
#: class of unreproducibility and it is governed by the same lock.
_GOVERNED_PACKAGES = ("fastapi", "starlette", "uvicorn")


def _locked_version(package: str) -> str | None:
    """The version ``uv.lock`` pins, which is what the image installs."""
    if not LOCK.is_file():
        return None
    text = LOCK.read_text(encoding="utf-8")
    match = re.search(
        rf'^name = "{re.escape(package)}"\nversion = "([^"]+)"',
        text,
        re.MULTILINE,
    )
    return match.group(1) if match else None


def _installed_version(package: str) -> str:
    from importlib.metadata import version

    return version(package)


@pytest.mark.parametrize("package", _GOVERNED_PACKAGES)
def test_the_installed_framework_matches_the_lock(package):
    locked = _locked_version(package)
    assert locked, f"{package} is not pinned in uv.lock; production has no exact version"
    installed = _installed_version(package)
    assert installed == locked, (
        f"{package} {installed} is installed but production builds "
        f"{locked} from uv.lock. A route walk is not portable across these: "
        "the authorization registry would be validated against a router the "
        "deployment does not have. Install the governed extra rather than "
        "loosening this assertion."
    )


def test_the_web_extra_pins_the_router_framework_exactly():
    """A range would let any environment installing it drift again.

    The `web` extra is what CI installs to get production's FastAPI, so it has
    to be an exact pin, not a floor.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    web = data["project"]["optional-dependencies"]["web"]
    pins = {
        entry.split("==")[0].split("[")[0]: entry
        for entry in web
    }
    for package in _ROUTER_PACKAGES:
        assert package in pins, f"the web extra does not carry {package}"
        assert "==" in pins[package], (
            f"{pins[package]!r} is a range; the deployed version must be exact"
        )


def test_the_core_dependency_range_is_not_what_gets_tested():
    """Records why the pin has to come from an extra.

    The core dependency is deliberately a range so downstream consumers can
    resolve their own compatible FastAPI. That flexibility is fine for a
    library and wrong for the qualification environment, which must match the
    image. If the core dependency is ever pinned exactly, this test should be
    deleted along with the `web` extra workaround it explains.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    core = [d for d in data["project"]["dependencies"] if d.startswith("fastapi")]
    assert core, "fastapi is no longer a core dependency; revisit this contract"
    assert "==" not in core[0], (
        "fastapi is now pinned exactly in core; the web-extra indirection in CI "
        "is redundant and should be simplified"
    )


@pytest.mark.parametrize("package", _GOVERNED_PACKAGES)
def test_the_resolved_module_is_reported_not_merely_its_version(package):
    """Version metadata and the imported module can disagree.

    A stale distribution left beside a newer one reports one version through
    ``importlib.metadata`` while ``import`` resolves the other, so the version
    alone is not proof of what the process is running. Asserting the module
    imports and resolves to a real file is what makes the version claim
    attributable to something on disk.
    """
    import importlib

    module = importlib.import_module(package)
    origin = getattr(module, "__file__", None)
    assert origin, f"{package} imported with no file origin"
    assert Path(origin).is_file(), f"{package} resolves to a missing file: {origin}"


def test_required_ci_installs_the_governed_extra():
    """The pin only helps if the job that tests actually installs it.

    `.[dev]` pins Starlette but not FastAPI, and the core dependency is a
    range, so that job resolved whatever was newest and walked a different
    router than production builds -- 136 of 294 route+methods. This fails
    closed if the install path is ever changed back, rather than waiting for
    the next inventory mismatch to reveal it.
    """
    workflow = (REPO / ".github" / "workflows" / "youtab-ci.yml").read_text(
        encoding="utf-8"
    )
    installs = [
        line.strip()
        for line in workflow.splitlines()
        if "pip install -e" in line and not line.strip().startswith("#")
    ]
    assert installs, "no editable install found in the required workflow"
    for line in installs:
        assert "web" in line, (
            f"{line!r} does not install the governed `web` extra; the job would "
            "resolve FastAPI freely from the core range again"
        )


def test_the_lock_is_the_only_place_versions_are_written():
    """No hand-maintained copy of a version anywhere in this file.

    A duplicated expected version is one more thing that drifts from the lock
    it is supposed to mirror, and it drifts silently -- the test keeps passing
    against its own stale constant.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    body = chr(10).join(
        line for line in source.splitlines()
        if not line.strip().startswith("#") and '"""' not in line
    )
    for locked in (_locked_version(p) for p in _GOVERNED_PACKAGES):
        assert locked, "a governed package is missing from uv.lock"
        assert locked not in body, (
            f"the version {locked!r} is written into this test; it must come "
            "from uv.lock so the two cannot disagree"
        )
