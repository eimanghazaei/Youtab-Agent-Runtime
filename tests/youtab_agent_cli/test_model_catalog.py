"""Tests for youtab_agent_cli.model_catalog — remote manifest fetch + cache + fallback."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# The engine catalogue now ships inside the package, so "the network failed"
# no longer means "there is no catalogue". That is the point of bundling it --
# but these tests are about the fetch path, and with the floor in place they
# would silently stop exercising it.
#
# This fixture removes the floor for exactly those tests, preserving their
# original meaning: nothing cached, nothing fetchable, nothing bundled. The
# floor itself is asserted separately, in test_engine_catalog_is_private.py and
# in TestBundledFloor below.
@pytest.fixture
def no_bundled_catalog():
    """Restore the pre-bundling world for tests about the fetch path.

    Two things changed underneath these tests. The catalogue now ships in the
    package, so "the network failed" no longer means "no catalogue"; and the
    shipped default URL is empty, so `get_catalog` skips the network entirely
    rather than fetching an empty address.

    Both are the intended new behaviour and are asserted in TestBundledFloor.
    This fixture puts back what these particular tests are about -- an
    operator-configured URL, and no floor beneath it -- so they keep covering
    the fetch chain instead of quietly passing without exercising it.
    """
    from unittest.mock import patch as _patch

    from youtab_agent_cli import model_catalog as _mc

    with _patch.object(_mc, "load_bundled_catalog", return_value=None), _patch.object(
        _mc,
        "_load_catalog_config",
        return_value={
            "enabled": True,
            "url": "https://catalogue.invalid/private/engine_catalog.json",
            "ttl_hours": 1,
            "providers": {},
            "provider_overrides": {},
        },
    ):
        yield



@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Isolate YOUTAB_AGENT_HOME + reset any module-level catalog cache per test."""
    home = tmp_path / ".youtab-agent-runtime"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("YOUTAB_AGENT_HOME", str(home))

    # Force a fresh catalog module state for each test.
    import importlib
    from youtab_agent_cli import model_catalog
    importlib.reload(model_catalog)
    yield home
    model_catalog.reset_cache()


def _valid_manifest() -> dict:
    return {
        "version": 1,
        "updated_at": "2026-04-25T22:00:00Z",
        "metadata": {"source": "test"},
        "providers": {
            "openrouter": {
                "metadata": {"display_name": "OpenRouter"},
                "models": [
                    {"id": "anthropic/claude-opus-4.7", "description": "recommended"},
                    {"id": "openai/gpt-5.4", "description": ""},
                    {"id": "openrouter/elephant-alpha", "description": "free"},
                ],
            },
            "youtab": {
                "metadata": {"display_name": "Youtab Portal"},
                "models": [
                    {"id": "anthropic/claude-opus-4.7"},
                    {"id": "moonshotai/kimi-k2.6"},
                ],
            },
        },
    }


class TestValidation:
    def test_accepts_well_formed_manifest(self, isolated_home):
        from youtab_agent_cli.model_catalog import _validate_manifest
        assert _validate_manifest(_valid_manifest()) is True

    def test_rejects_non_dict(self, isolated_home):
        from youtab_agent_cli.model_catalog import _validate_manifest
        assert _validate_manifest("string") is False
        assert _validate_manifest([]) is False
        assert _validate_manifest(None) is False


    def test_rejects_non_string_model_id(self, isolated_home):
        from youtab_agent_cli.model_catalog import _validate_manifest
        m = _valid_manifest()
        m["providers"]["openrouter"]["models"][0] = {"id": 42}
        assert _validate_manifest(m) is False


class TestFetchSuccess:
    def test_fetch_and_cache_writes_disk(self, isolated_home, no_bundled_catalog):
        from youtab_agent_cli import model_catalog
        manifest = _valid_manifest()
        with patch.object(
            model_catalog, "_fetch_manifest", return_value=manifest
        ) as fetch:
            result = model_catalog.get_catalog(force_refresh=True)

        assert result == manifest
        assert fetch.called

        cache_file = model_catalog._cache_path()
        assert cache_file.exists()
        with open(cache_file, encoding="utf-8") as fh:
            assert json.load(fh) == manifest


class TestFetchFailure:
    def test_network_failure_returns_empty_when_no_cache(self, isolated_home, no_bundled_catalog):
        from youtab_agent_cli import model_catalog
        with patch.object(model_catalog, "_fetch_manifest", return_value=None):
            result = model_catalog.get_catalog(force_refresh=True)
        assert result == {}

    def test_network_failure_falls_back_to_disk_cache(self, isolated_home, no_bundled_catalog):
        from youtab_agent_cli import model_catalog
        # Prime disk cache with a fresh copy.
        manifest = _valid_manifest()
        with patch.object(model_catalog, "_fetch_manifest", return_value=manifest):
            model_catalog.get_catalog(force_refresh=True)

        # Now wipe in-process cache and simulate network failure on refetch.
        model_catalog.reset_cache()
        with patch.object(model_catalog, "_fetch_manifest", return_value=None):
            result = model_catalog.get_catalog(force_refresh=True)

        assert result == manifest

    def test_fetch_failure_falls_back_to_stale_cache(self, isolated_home):
        from youtab_agent_cli import model_catalog
        manifest = _valid_manifest()
        # Write stale cache directly (mtime in the past).
        cache = model_catalog._cache_path()
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        old = time.time() - 30 * 24 * 3600  # 30 days ago
        import os as _os
        _os.utime(cache, (old, old))

        with patch.object(model_catalog, "_fetch_manifest", return_value=None):
            result = model_catalog.get_catalog()

        # Stale cache is better than nothing.
        assert result == manifest


class TestFallbackChain:
    """``_fetch_manifest_with_fallback`` walks ``DEFAULT_CATALOG_FALLBACK_URLS``
    when the primary URL fails. Regression: the Docusaurus site behind Vercel
    occasionally returns HTTP 403 + x-vercel-mitigated: challenge for urllib;
    without a fallback URL the user's disk cache freezes and new model
    releases (opus 4.8, etc.) never reach the picker.
    """

    # Placeholder operator-configured URLs. The shipped default is empty --
    # the catalogue is packaged, not published -- so these exist only to
    # exercise the fallback plumbing that a private URL would still use.
    PRIMARY = "https://catalogue.invalid/private/engine_catalog.json"
    FALLBACK = (
        "https://raw.githubusercontent.com/eimanghazaei/Youtab-Agent-Runtime"
        "/private/engine_catalog.fallback.json"
    )

    def test_uses_primary_when_it_succeeds(self, isolated_home):
        from youtab_agent_cli import model_catalog
        calls: list[str] = []

        def fake_fetch(url, timeout):
            calls.append(url)
            return _valid_manifest()

        with patch.object(model_catalog, "_fetch_manifest", side_effect=fake_fetch):
            result = model_catalog._fetch_manifest_with_fallback(self.PRIMARY, 5.0)

        assert result is not None
        assert calls == [self.PRIMARY], "fallback URLs must not be touched on primary success"

    def test_falls_through_to_the_configured_fallback_on_primary_failure(
        self, isolated_home, no_bundled_catalog
    ):
        """The chain still works; what changed is that it ships empty.

        This used to assert a hard-coded raw.githubusercontent fallback. That
        URL published the engine catalogue to anyone who knew it, so it was
        retired along with the primary. The plumbing it exercised is still
        real -- an operator with a privately hosted catalogue and a mirror
        needs it -- so the fallback is supplied here instead of assumed.
        """
        from youtab_agent_cli import model_catalog
        calls: list[str] = []

        def fake_fetch(url, timeout):
            calls.append(url)
            if url == self.PRIMARY:
                return None  # simulate Vercel 403
            return _valid_manifest()

        # Passed explicitly rather than patched onto the module: the parameter
        # is a default argument bound at definition time, so rebinding the
        # module attribute would not reach it and the test would pass while
        # walking an empty chain.
        with patch.object(model_catalog, "_fetch_manifest", side_effect=fake_fetch):
            result = model_catalog._fetch_manifest_with_fallback(
                self.PRIMARY, 5.0, fallback_urls=(self.FALLBACK,)
            )

        assert result is not None
        assert calls == [self.PRIMARY, self.FALLBACK]

    def test_no_fallback_url_ships_by_default(self):
        """The retired public mirror must not come back as a default."""
        from youtab_agent_cli import model_catalog

        assert model_catalog.DEFAULT_CATALOG_FALLBACK_URLS == ()


    def test_get_catalog_uses_fallback_chain(self, isolated_home, no_bundled_catalog):
        """End-to-end: ``get_catalog`` routes through the fallback helper so
        a primary URL failure transparently produces a working catalog."""
        from youtab_agent_cli import model_catalog
        manifest = _valid_manifest()
        calls: list[str] = []

        def fake_fetch(url, timeout):
            calls.append(url)
            if url == self.PRIMARY:
                return None
            return manifest

        real_chain = model_catalog._fetch_manifest_with_fallback

        def chain_with_fallback(primary, timeout, fallback_urls=None):
            return real_chain(primary, timeout, fallback_urls=(self.FALLBACK,))

        with patch.object(model_catalog, "_fetch_manifest", side_effect=fake_fetch), patch.object(
            model_catalog, "_fetch_manifest_with_fallback", side_effect=chain_with_fallback
        ):
            result = model_catalog.get_catalog(force_refresh=True)

        assert result == manifest
        assert self.FALLBACK in calls


class TestCuratedAccessors:
    def test_openrouter_returns_tuples(self, isolated_home, no_bundled_catalog):
        from youtab_agent_cli import model_catalog
        with patch.object(
            model_catalog, "_fetch_manifest", return_value=_valid_manifest()
        ):
            result = model_catalog.get_curated_openrouter_models()
        assert result == [
            ("anthropic/claude-opus-4.7", "recommended"),
            ("openai/gpt-5.4", ""),
            ("openrouter/elephant-alpha", "free"),
        ]


    def test_youtab_returns_none_when_catalog_empty(self, isolated_home, no_bundled_catalog):
        from youtab_agent_cli import model_catalog
        with patch.object(model_catalog, "_fetch_manifest", return_value=None):
            assert model_catalog.get_curated_youtab_models() is None


class TestDefaultModelFromCache:
    """get_default_model_from_cache reads the '"default": true' label without
    ever hitting the network."""

    def _manifest_with_default(self) -> dict:
        m = _valid_manifest()
        m["providers"]["openrouter"]["models"][1]["default"] = True  # gpt-5.4
        m["providers"]["youtab"]["models"][1]["default"] = True  # kimi-k2.6
        return m

    def test_reads_label_from_disk_cache(self, isolated_home):
        from youtab_agent_cli import model_catalog
        cache = isolated_home / "cache"
        cache.mkdir()
        (cache / "model_catalog.json").write_text(
            json.dumps(self._manifest_with_default()),
            encoding="utf-8",
        )
        with patch.object(model_catalog, "_fetch_manifest") as fetch:
            assert (
                model_catalog.get_default_model_from_cache("openrouter")
                == "openai/gpt-5.4"
            )
            assert (
                model_catalog.get_default_model_from_cache("youtab")
                == "moonshotai/kimi-k2.6"
            )
            fetch.assert_not_called()

    def test_no_label_returns_none(self, isolated_home):
        from youtab_agent_cli import model_catalog
        cache = isolated_home / "cache"
        cache.mkdir()
        (cache / "model_catalog.json").write_text(json.dumps(_valid_manifest()), encoding="utf-8")
        with patch.object(model_catalog, "_fetch_manifest") as fetch:
            assert model_catalog.get_default_model_from_cache("openrouter") is None
            fetch.assert_not_called()


    def test_shipped_manifest_labels_glm52_default(self, isolated_home):
        """Contract with the in-repo manifest: both provider blocks label the
        same default entry the code constant points at."""
        import youtab_agent_cli.model_catalog as model_catalog
        from youtab_agent_cli.models import PREFERRED_SILENT_DEFAULT_MODEL

        repo_root = Path(model_catalog.__file__).resolve().parent.parent
        manifest = json.loads(
            (repo_root / "youtab_agent_cli" / "data" / "engine_catalog.json").read_text(encoding="utf-8")
        )
        for provider in ("openrouter", "youtab"):
            block = manifest["providers"][provider]
            labeled = [m["id"] for m in block["models"] if m.get("default")]
            assert labeled == [PREFERRED_SILENT_DEFAULT_MODEL], (
                f"{provider}: exactly one entry must be labeled default and it "
                f"must match PREFERRED_SILENT_DEFAULT_MODEL"
            )


class TestProviderOverride:
    def test_override_url_takes_precedence(self, isolated_home):
        from youtab_agent_cli import model_catalog

        override_payload = {
            "version": 1,
            "providers": {
                "openrouter": {
                    "models": [
                        {"id": "override/model", "description": "custom"},
                    ]
                }
            },
        }

        def fake_fetch(url, timeout):
            if "override" in url:
                return override_payload
            return _valid_manifest()

        with patch.object(
            model_catalog,
            "_load_catalog_config",
            return_value={
                "enabled": True,
                "url": "http://master",
                "ttl_hours": 24.0,
                "providers": {"openrouter": {"url": "http://override"}},
            },
        ):
            with patch.object(model_catalog, "_fetch_manifest", side_effect=fake_fetch):
                result = model_catalog.get_curated_openrouter_models()

        assert result == [("override/model", "custom")]


class TestIntegrationWithModelsModule:
    """Exercise the fallback paths via the real callers in youtab_agent_cli.models."""



    def test_picker_youtab_row_uses_curated_list(self, tmp_path, monkeypatch):
        """The /model picker surfaces the curated ``_PROVIDER_MODELS["youtab"]``
        list in curated order — matching the ``youtab model`` CLI — not the live
        ``/v1/models`` catalog or the manifest. Portal free/paid recommendations
        are unioned in when reachable; offline (as here, with the Portal calls
        stubbed out) it's exactly the curated list.
        """
        # We deliberately do NOT use the ``isolated_home`` fixture here:
        # that fixture monkeypatches ``Path.home`` to ``tmp_path``, which
        # trips the auth-store seat-belt in ``_auth_file_path()`` because
        # ``YOUTAB_AGENT_HOME / auth.json`` then resolves to the same path the
        # seat-belt thinks is the "real" user store. Use the autouse
        # ``_hermetic_environment`` YOUTAB_AGENT_HOME directly instead.
        import importlib
        from youtab_agent_cli import model_catalog
        from youtab_agent_cli.models import get_curated_youtab_model_ids
        importlib.reload(model_catalog)
        try:
            from youtab_agent_cli.model_switch import list_picker_providers

            active_home = Path(os.environ["YOUTAB_AGENT_HOME"])
            (active_home / "auth.json").write_text(
                json.dumps(
                    {
                        "providers": {"youtab": {"access_token": "fake"}},
                        "credential_pool": {},
                    }
                ),
                encoding="utf-8",
            )

            # Stub the Portal recommendation union so the row is deterministic
            # (the curated list alone) and never touches the network. ``expected``
            # is computed from the same source the picker uses internally
            # (``curated["youtab"] = get_curated_youtab_model_ids()``), so the test
            # stays an invariant — it can't rot as the curated/manifest list grows.
            with patch.object(
                model_catalog, "_fetch_manifest", return_value=_valid_manifest()
            ), patch("youtab_agent_cli.models.check_youtab_free_tier", return_value=False), patch(
                "youtab_agent_cli.models.union_with_portal_free_recommendations",
                side_effect=lambda ids, *a, **k: (ids, {}),
            ), patch(
                "youtab_agent_cli.models.union_with_portal_paid_recommendations",
                side_effect=lambda ids, *a, **k: (ids, {}),
            ):
                expected = get_curated_youtab_model_ids()
                picker = list_picker_providers(
                    current_provider="youtab", max_models=99
                )
        finally:
            model_catalog.reset_cache()

        youtab_row = next((r for r in picker if r["slug"] == "youtab"), None)
        assert youtab_row is not None, "youtab row must appear when authed"
        assert youtab_row["models"] == expected

    def test_picker_max_models_cap_semantics(self, tmp_path, monkeypatch):
        """The cap argument has three distinct meanings on the real slicing
        path: ``None`` = unlimited (the cap-removal fix, #48297), ``0`` = no
        models (preserved for slug-only callers), an int N = first N. Guards
        the ``is not None`` distinction the cap-removal follow-up introduced —
        a ``if max_models`` (falsy) check would conflate ``0`` with unlimited.
        """
        import importlib
        from youtab_agent_cli import model_catalog
        from youtab_agent_cli.models import get_curated_youtab_model_ids
        importlib.reload(model_catalog)
        try:
            from youtab_agent_cli.model_switch import (
                list_authenticated_providers,
                list_picker_providers,
            )

            active_home = Path(os.environ["YOUTAB_AGENT_HOME"])
            (active_home / "auth.json").write_text(
                json.dumps(
                    {
                        "providers": {"youtab": {"access_token": "fake"}},
                        "credential_pool": {},
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                model_catalog, "_fetch_manifest", return_value=_valid_manifest()
            ), patch("youtab_agent_cli.models.check_youtab_free_tier", return_value=False), patch(
                "youtab_agent_cli.models.union_with_portal_free_recommendations",
                side_effect=lambda ids, *a, **k: (ids, {}),
            ), patch(
                "youtab_agent_cli.models.union_with_portal_paid_recommendations",
                side_effect=lambda ids, *a, **k: (ids, {}),
            ):
                expected = get_curated_youtab_model_ids()
                full = list_picker_providers(current_provider="youtab", max_models=None)
                one = list_picker_providers(current_provider="youtab", max_models=1)
                # 0 is exercised on list_authenticated_providers (the slug-only
                # path); the picker variant drops empty-model rows entirely, so
                # the empty-list contract lives on the auth-providers call.
                zero = list_authenticated_providers(
                    current_provider="youtab", max_models=0
                )
        finally:
            model_catalog.reset_cache()

        def _youtab(rows):
            return next((r for r in rows if r["slug"] == "youtab"), None)

        # Only meaningful when the curated list actually exceeds 1 entry.
        assert len(expected) > 1, "test needs a multi-model curated youtab list"

        full_row = _youtab(full)
        assert full_row is not None and full_row["models"] == expected

        one_row = _youtab(one)
        assert one_row is not None and one_row["models"] == expected[:1]

        zero_row = _youtab(zero)
        # 0 means an empty model list — NOT unlimited. total_models still real.
        assert zero_row is not None
        assert zero_row["models"] == []
        assert zero_row["total_models"] == len(expected)


# -----------------------------------------------------------------------------
# Drift guard — prevent the in-repo curated lists from going out of sync with
# the packaged manifest at youtab_agent_cli/data/engine_catalog.json.
#
# History: qwen/qwen3.6-plus was added to _PROVIDER_MODELS["youtab"] in commit
# 9dd6e5510 but youtab_agent_cli/data/engine_catalog.json was not regenerated for
# weeks, so free-tier users on a new install fetched a stale manifest and the
# free-tier picker showed "No free models currently available." even though
# the Portal was serving qwen/qwen3.6-plus as free. CI must catch this.
# -----------------------------------------------------------------------------


class TestManifestMatchesInRepoLists:
    """Fail if the on-disk manifest is out of date relative to in-repo lists."""

    @staticmethod
    def _strip_volatile(catalog: dict) -> dict:
        """Drop fields that always change (timestamps) for diff comparison."""
        out = dict(catalog)
        out.pop("updated_at", None)
        return out

    def test_in_repo_lists_match_manifest(self):
        """``scripts/build_model_catalog.py`` output must match the committed file.

        If this fails, run ``python scripts/build_model_catalog.py`` and
        commit the regenerated ``youtab_agent_cli/data/engine_catalog.json``.
        """
        # Resolve the repo root from this test file's location.
        repo_root = Path(__file__).resolve().parents[2]
        manifest_path = repo_root / "youtab_agent_cli" / "data" / "engine_catalog.json"

        if not manifest_path.exists():
            pytest.skip(f"manifest missing at {manifest_path}")

        # Build expected catalog using the same script CI would.
        import importlib.util
        script_path = repo_root / "scripts" / "build_model_catalog.py"
        spec = importlib.util.spec_from_file_location("_build_model_catalog", script_path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        expected = mod.build_catalog()

        with open(manifest_path, encoding="utf-8") as fh:
            actual = json.load(fh)

        assert self._strip_volatile(actual) == self._strip_volatile(expected), (
            "youtab_agent_cli/data/engine_catalog.json is out of sync with "
            "_PROVIDER_MODELS['youtab'] / OPENROUTER_MODELS. "
            "Run: python scripts/build_model_catalog.py && "
            "git add youtab_agent_cli/data/engine_catalog.json"
        )


class TestBundledFloor:
    """The packaged catalogue is the floor when nothing else is available.

    This is what makes retiring the public URL safe: with no cache, no
    configured URL and no network, the picker still has a catalogue. Without
    it, "the catalogue is private" would have quietly meant "there is no
    catalogue on a fresh install".
    """

    def test_no_cache_and_no_network_still_yields_a_catalogue(self, isolated_home):
        from unittest.mock import patch

        from youtab_agent_cli import model_catalog

        model_catalog._catalog_cache = None
        model_catalog._catalog_cache_source_mtime = None
        with patch.object(model_catalog, "_fetch_manifest", return_value=None):
            catalog = model_catalog.get_catalog(force_refresh=True)

        assert catalog, "a fresh install with no network got an empty catalogue"

    def test_the_floor_is_what_ships_in_the_package(self, isolated_home):
        from youtab_agent_cli import model_catalog

        assert model_catalog.load_bundled_catalog() is not None

    def test_no_network_call_is_attempted_without_a_configured_url(self, isolated_home):
        """Empty URL must mean "do not fetch", not "fetch an empty address"."""
        from unittest.mock import patch

        from youtab_agent_cli import model_catalog

        model_catalog._catalog_cache = None
        model_catalog._catalog_cache_source_mtime = None
        with patch.object(model_catalog, "_fetch_manifest") as fetch:
            with patch.object(
                model_catalog, "_load_catalog_config",
                return_value={
                    "enabled": True, "url": "", "ttl_hours": 1,
                    "providers": {}, "provider_overrides": {},
                },
            ):
                model_catalog.get_catalog(force_refresh=True)
        assert not fetch.called, "a fetch was attempted with no catalogue URL configured"
