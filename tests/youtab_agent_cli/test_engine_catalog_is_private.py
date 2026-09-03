"""The engine catalogue is private infrastructure and must not be published.

It is a full inventory of provider and model identifiers. While it was served
from the documentation site at ``/docs/api/model-catalog.json``, with a
raw.githubusercontent fallback, anyone could read which engines sit behind the
product by fetching a URL — no credential, no trace, and no way to notice it
had happened.

It now ships inside the distribution. The data is unchanged and the runtime
still needs it; what changed is that reading it requires having the package
rather than knowing an address.

These tests pin the *absence* of the public surface, which is harder to keep
than its presence: a default URL is one convenience commit away from coming
back, and nothing else in the suite would fail if it did.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def test_the_catalogue_is_not_in_the_published_site_tree():
    """`website/static/` is copied verbatim to the public docs site.

    Anything under it is served. That is what made this file readable, and it
    is why the check is on the directory rather than on one filename.
    """
    published = REPO / "website" / "static"
    if not published.exists():
        pytest.skip("no website/static tree in this checkout")
    offenders = [
        path.relative_to(REPO)
        for path in published.rglob("*.json")
        if "catalog" in path.name.lower() or "model" in path.name.lower()
    ]
    assert not offenders, (
        "an engine/model catalogue is under website/static, which is published "
        f"verbatim to the docs site: {offenders}"
    )


def test_the_catalogue_ships_with_the_package():
    """The positive control.

    Without this, deleting the data entirely would satisfy every other test
    here while breaking the model picker.
    """
    from youtab_agent_cli.model_catalog import BUNDLED_CATALOG_PATH

    assert BUNDLED_CATALOG_PATH.exists(), f"the packaged catalogue is missing: {BUNDLED_CATALOG_PATH}"
    data = json.loads(BUNDLED_CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data, "the packaged catalogue is empty"


def test_the_packaged_catalogue_loads_and_validates():
    from youtab_agent_cli.model_catalog import load_bundled_catalog

    assert load_bundled_catalog() is not None, (
        "the packaged catalogue failed validation, so the runtime would fall "
        "back to an empty catalogue"
    )


def test_packaging_declares_the_data_directory():
    """A sealed build that drops the file degrades silently.

    ``load_bundled_catalog`` returns ``None`` rather than raising, which is the
    right runtime behaviour and the wrong thing to discover in production. The
    package-data declaration is what keeps the file in the wheel.
    """
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'youtab_agent_cli = [' in pyproject
    block = pyproject.split("youtab_agent_cli = [", 1)[1].split("]", 1)[0]
    assert "data/*.json" in block, (
        "youtab_agent_cli package-data does not include data/*.json, so the "
        "engine catalogue would be absent from a built wheel"
    )


def test_no_default_remote_catalogue_url():
    """No address to fetch, and none to leak.

    An operator may configure a private URL; the shipped default must not point
    anywhere, or the catalogue is public again by default.
    """
    from youtab_agent_cli.model_catalog import DEFAULT_CATALOG_FALLBACK_URLS, DEFAULT_CATALOG_URL

    assert DEFAULT_CATALOG_URL == "", f"a default catalogue URL is configured: {DEFAULT_CATALOG_URL!r}"
    assert DEFAULT_CATALOG_FALLBACK_URLS == (), (
        f"default catalogue fallback URLs are configured: {DEFAULT_CATALOG_FALLBACK_URLS!r}"
    )

    from youtab_agent_cli.config_defaults import DEFAULT_CONFIG

    configured = DEFAULT_CONFIG.get("model_catalog", {}).get("url", "")
    assert configured == "", f"the shipped config still points at a catalogue URL: {configured!r}"


def test_no_source_file_still_advertises_a_public_catalogue_url():
    """Source-text sweep, because the constants above are not the only place.

    A comment, a docstring or a test fixture naming the retired address is a
    map back to it for anyone reading the repository, and one of them was how
    the fallback URL survived an earlier removal.
    """
    retired = ("/docs/api/model-catalog.json", "website/static/api/model-catalog.json")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()

    offenders: list[str] = []
    for rel in tracked:
        # git ls-files yields forward-slash paths; compare against a forward-slash
        # form of this file's path so the self-exclusion also holds on Windows
        # (Path.relative_to renders backslashes there).
        if rel.startswith("docs/evidence/") or rel == Path(__file__).relative_to(REPO).as_posix():
            continue  # evidence records the finding; this file names it to forbid it
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(marker in text for marker in retired):
            offenders.append(rel)

    assert not offenders, (
        "these files still reference the retired public catalogue location:\n  "
        + "\n  ".join(sorted(offenders))
    )
