"""Backend and model selection: no silent fallback, in either direction.

A wake-word measurement is only worth what its label is worth. If a run asked
for tflite and quietly got onnx, then "tflite p95 latency", "tflite parity" and
"tflite qualified on this machine" are all statements about a file that was
never loaded — and this repository ships both files side by side, so nothing
about the run would look wrong.

There are three separate downgrade mechanisms in play, and only the first is
ours:

1. ``resolve_inference_framework`` COERCES an explicit ``onnx`` to ``tflite`` on
   macOS ARM64, because openWakeWord's ONNX embedding model never lets a phrase
   cross the threshold there (dscripka/openWakeWord#336). Deliberate, warned
   about once, and the opposite of a silent downgrade — it replaces a listener
   that arms and never fires.
2. ``_OpenWakeWordEngine.__init__`` DOWNGRADES a tflite request to onnx when no
   tflite runtime can be found, everywhere except macOS ARM64, where it raises
   instead. Logged at warning level.
3. openWakeWord ITSELF downgrades, silently apart from a log line, inside
   ``Model.__init__`` (0.6.0 model.py 112-128): when its ``import
   tflite_runtime.interpreter`` fails and an ``.onnx`` exists beside the
   requested ``.tflite``, it rewrites the path and switches framework. Both
   files exist in ``tools/wakewords/``, so that branch is live for this repo.
   The runtime pre-empts it by resolving the tflite runtime *before*
   constructing ``Model``.

openWakeWord's ``Model`` keeps no record of which framework it ended up on — it
has no ``inference_framework`` attribute at all — so the engine records its own,
after every coercion and downgrade above. These tests hold that record honest:
the central assertion is that what the engine claims always equals what
openWakeWord was actually driven with, and that the artifact extension agrees
with both.

The stand-in ``Model`` is faithful where it matters: it reproduces upstream's
extension/framework guards (which raise both ways) and upstream's silent swap,
and ``test_the_upstream_silent_swap_is_real`` proves the stand-in can still
perform that swap — otherwise every no-fallback test here would pass for free.
"""

from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path

import pytest

import tools.wake_word as ww

WAKEWORDS = Path(ww.__file__).parent / "wakewords"


# ── the environment the engine is built in ───────────────────────────────────


def _platform(monkeypatch, system: str, machine: str) -> None:
    monkeypatch.setattr(ww.sys, "platform", system)
    monkeypatch.setattr("platform.machine", lambda: machine)


def _with_tflite_runtime(monkeypatch) -> None:
    """A machine where ``import tflite_runtime.interpreter`` succeeds.

    Installed into ``sys.modules`` rather than faked at
    ``ensure_tflite_runtime``, so the real resolver runs and the stand-in
    ``Model`` sees the same import upstream would.
    """
    pkg = types.ModuleType("tflite_runtime")
    pkg.__path__ = []
    interpreter = types.ModuleType("tflite_runtime.interpreter")
    interpreter.Interpreter = object
    monkeypatch.setitem(sys.modules, "tflite_runtime", pkg)
    monkeypatch.setitem(sys.modules, "tflite_runtime.interpreter", interpreter)


def _without_tflite_runtime(monkeypatch) -> None:
    """A machine with no tflite runtime at all.

    Both halves are needed. The ``sys.modules`` entries are deleted because
    ``ensure_tflite_runtime`` aliases ``ai_edge_litert`` in permanently when it
    succeeds, and this test environment HAS ai-edge-litert (the model-asset
    tests need it) — so without the delete an earlier test in the same process
    would decide this one's outcome.
    """
    monkeypatch.delitem(sys.modules, "tflite_runtime.interpreter", raising=False)
    monkeypatch.delitem(sys.modules, "tflite_runtime", raising=False)
    monkeypatch.setattr(ww, "ensure_tflite_runtime", lambda: False)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: None)


def _install_fake_openwakeword(monkeypatch):
    """A stand-in ``openwakeword`` that keeps upstream's fallback behaviour.

    Returns a list of records, one per ``Model`` construction, holding what was
    requested and what upstream would actually have run.
    """
    built: list[dict] = []
    downloaded: list[list[str]] = []

    class _FakeModel:
        def __init__(self, wakeword_models, inference_framework="onnx"):
            requested = inference_framework
            models = list(wakeword_models)

            # Upstream model.py 0.6.0, lines 112-128 — verbatim in behaviour.
            if inference_framework == "tflite":
                try:
                    import tflite_runtime.interpreter  # noqa: F401,PLC0415
                except ImportError:
                    if models and all(".onnx" in m for m in models):
                        inference_framework = "onnx"
                    elif models and all(
                        os.path.exists(m.replace(".tflite", ".onnx")) for m in models
                    ):
                        inference_framework = "onnx"
                        models = [m.replace(".tflite", ".onnx") for m in models]
                    else:
                        raise ValueError(
                            "Tried to import the tflite runtime for provided tflite "
                            "models, but it was not found."
                        )

            # Upstream's own guards, which raise both ways.
            for path in models:
                if inference_framework == "onnx" and ".tflite" in path:
                    raise ValueError(
                        "The onnx inference framework is selected, but tflite "
                        "models were provided!"
                    )
                if inference_framework == "tflite" and ".onnx" in path:
                    raise ValueError(
                        "The tflite inference framework is selected, but onnx "
                        "models were provided!"
                    )

            built.append({
                "requested_framework": requested,
                "framework_actually_used": inference_framework,
                "models_actually_loaded": models,
            })
            self.models = {"hey_youtab": object()}

        def predict(self, frame):
            return {"hey_youtab": 0.0}

        def reset(self):
            pass

    oww = types.ModuleType("openwakeword")
    oww.utils = types.SimpleNamespace(
        download_models=lambda names=[]: downloaded.append(list(names))
    )
    model_mod = types.ModuleType("openwakeword.model")
    model_mod.Model = _FakeModel
    monkeypatch.setitem(sys.modules, "openwakeword", oww)
    monkeypatch.setitem(sys.modules, "openwakeword.model", model_mod)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: None)
    return built, downloaded


def _engine(monkeypatch, framework=None, model=None, **cfg):
    sub = {}
    if framework is not None:
        sub["inference_framework"] = framework
    if model is not None:
        sub["model"] = model
    return ww._OpenWakeWordEngine({"provider": "openwakeword", "openwakeword": sub, **cfg})


# ── the trap this whole file exists for ──────────────────────────────────────


def test_the_upstream_silent_swap_is_real_and_reachable_here(monkeypatch):
    """Non-vacuity: the stand-in can still perform upstream's silent swap.

    Both artifacts ship in the same directory, which is precisely the condition
    upstream's second fallback branch tests for — so a tflite request with no
    tflite runtime does not fail, it quietly becomes an ONNX run of a different
    file. If this test ever stops passing, the stand-in has been softened and
    every no-fallback assertion below is proving nothing.
    """
    assert (WAKEWORDS / "hey_youtab.tflite").is_file()
    assert (WAKEWORDS / "hey_youtab.onnx").is_file(), (
        "the .onnx beside the .tflite is what makes upstream's swap silent"
    )

    built, _ = _install_fake_openwakeword(monkeypatch)
    _without_tflite_runtime(monkeypatch)
    from openwakeword.model import Model  # the stand-in

    Model(
        wakeword_models=[str(WAKEWORDS / "hey_youtab.tflite")],
        inference_framework="tflite",
    )
    assert built[0]["requested_framework"] == "tflite"
    assert built[0]["framework_actually_used"] == "onnx"
    assert built[0]["models_actually_loaded"][0].endswith(".onnx")


# ── what the engine claims must be what openWakeWord ran ─────────────────────


def test_a_tflite_request_loads_tflite_and_says_so(monkeypatch):
    built, downloaded = _install_fake_openwakeword(monkeypatch)
    _with_tflite_runtime(monkeypatch)
    _platform(monkeypatch, "linux", "x86_64")

    engine = _engine(monkeypatch, framework="tflite")

    assert engine.inference_framework == "tflite"
    assert engine.model_reference == ww._bundled_wakeword_path("tflite")
    assert engine.model_reference.endswith(".tflite")
    assert built == [{
        "requested_framework": "tflite",
        "framework_actually_used": "tflite",
        "models_actually_loaded": [ww._bundled_wakeword_path("tflite")],
    }]
    assert downloaded == [[ww._bundled_wakeword_path("tflite")]]


def test_an_onnx_request_loads_onnx_and_says_so(monkeypatch):
    built, _ = _install_fake_openwakeword(monkeypatch)
    _platform(monkeypatch, "linux", "x86_64")

    engine = _engine(monkeypatch, framework="onnx")

    assert engine.inference_framework == "onnx"
    assert engine.model_reference.endswith(".onnx")
    assert built[0]["framework_actually_used"] == "onnx"


def test_a_tflite_request_that_cannot_be_honoured_reports_onnx_not_tflite(
    monkeypatch, caplog
):
    """THE no-silent-fallback case: the label has to follow the downgrade.

    On Linux and Windows a missing tflite runtime is survivable — ONNX works
    there — so the engine downgrades rather than refusing. What it must not do
    is keep calling itself tflite, because then
    ``scripts/wakeword/verify_on_device.py`` writes "inference_framework:
    tflite" into a JSON record of an ONNX run.
    """
    built, downloaded = _install_fake_openwakeword(monkeypatch)
    _without_tflite_runtime(monkeypatch)
    _platform(monkeypatch, "linux", "x86_64")

    with caplog.at_level(logging.WARNING, logger="tools.wake_word"):
        engine = _engine(monkeypatch, framework="tflite")

    assert engine.inference_framework == "onnx"
    assert engine.inference_framework != "tflite"
    assert engine.model_reference.endswith(".onnx")
    # Downgraded before Model was built, so upstream's silent swap never got a
    # chance to happen behind us.
    assert built[0]["requested_framework"] == "onnx"
    assert built[0]["framework_actually_used"] == "onnx"
    assert downloaded == [[ww._bundled_wakeword_path("onnx")]]
    # ...and it was said out loud.
    assert any(
        "no tflite runtime" in r.getMessage() for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize(
    ("system", "machine", "requested", "runtime", "expected"),
    [
        ("linux", "x86_64", None, False, "onnx"),
        ("linux", "x86_64", "", False, "onnx"),
        ("linux", "x86_64", "onnx", False, "onnx"),
        ("linux", "x86_64", "tflite", True, "tflite"),
        ("linux", "x86_64", "tflite", False, "onnx"),      # downgraded
        ("win32", "AMD64", None, False, "onnx"),
        ("win32", "AMD64", "tflite", True, "tflite"),
        ("win32", "AMD64", "tflite", False, "onnx"),       # downgraded
        ("darwin", "arm64", None, True, "tflite"),
        ("darwin", "arm64", "tflite", True, "tflite"),
        ("darwin", "arm64", "onnx", True, "tflite"),       # coerced, #336
        ("darwin", "x86_64", None, False, "onnx"),
    ],
)
def test_the_engine_never_claims_a_backend_openwakeword_did_not_use(
    monkeypatch, system, machine, requested, runtime, expected
):
    """One assertion, both directions, across every platform combination.

    ``engine.inference_framework`` is what a report prints; the record from the
    stand-in is what openWakeWord actually ran. Any silent fallback — ours or
    upstream's, tflite to onnx or onnx to tflite — separates the two.
    """
    monkeypatch.setattr(ww, "_warned_onnx_coerced", False)
    built, _ = _install_fake_openwakeword(monkeypatch)
    if runtime:
        _with_tflite_runtime(monkeypatch)
    else:
        _without_tflite_runtime(monkeypatch)
    _platform(monkeypatch, system, machine)

    engine = _engine(monkeypatch, framework=requested)

    assert engine.inference_framework == expected
    assert engine.inference_framework == built[0]["framework_actually_used"]
    # The artifact has to agree too: these two files are not interchangeable
    # and upstream refuses a mismatched pair.
    assert engine.model_reference.endswith(f".{expected}")
    assert built[0]["models_actually_loaded"] == [engine.model_reference]


def test_macos_arm64_refuses_a_downgrade_it_knows_is_dead(monkeypatch):
    """No engine at all, rather than one that arms and can never fire.

    This is the one platform where the fallback target is broken, so refusing is
    the only honest option — and the message has to name the fix.
    """
    built, _ = _install_fake_openwakeword(monkeypatch)
    _without_tflite_runtime(monkeypatch)
    _platform(monkeypatch, "darwin", "arm64")

    with pytest.raises(RuntimeError, match="ai-edge-litert"):
        _engine(monkeypatch, framework="tflite")

    assert built == [], "a model was built after the runtime refused to build one"


def test_macos_arm64_coerces_an_explicit_onnx_pin_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(ww, "_warned_onnx_coerced", False)
    _install_fake_openwakeword(monkeypatch)
    _with_tflite_runtime(monkeypatch)
    _platform(monkeypatch, "darwin", "arm64")

    with caplog.at_level(logging.WARNING, logger="tools.wake_word"):
        assert ww.resolve_inference_framework(
            {"openwakeword": {"inference_framework": "onnx"}}
        ) == "tflite"
        assert ww.resolve_inference_framework(
            {"openwakeword": {"inference_framework": "onnx"}}
        ) == "tflite"

    coerced = [r for r in caplog.records if "openWakeWord #336" in r.getMessage()]
    assert len(coerced) == 1, "the coercion notice must not repeat per resolve"


def test_the_platform_default_matrix(monkeypatch):
    """``default_inference_framework`` is the whole platform policy."""
    for system, machine, expected in (
        ("darwin", "arm64", "tflite"),
        ("darwin", "x86_64", "onnx"),
        ("linux", "x86_64", "onnx"),
        ("linux", "aarch64", "onnx"),
        ("win32", "AMD64", "onnx"),
    ):
        _platform(monkeypatch, system, machine)
        assert ww.default_inference_framework() == expected, (system, machine)
        assert ww.resolve_inference_framework({}) == expected


# ── model selection ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("alias", sorted(ww._BUNDLED_MODEL_ALIASES))
def test_every_bundled_alias_resolves_to_the_shipped_artifact(monkeypatch, alias):
    """The wake word works out of the box, and keeps working.

    ``""``/``hey_youtab``/``hey youtab``/``youtab`` all mean the shipped model.
    An alias that stopped resolving would send openWakeWord to its pretrained
    catalog for a name that is not in it.
    """
    built, downloaded = _install_fake_openwakeword(monkeypatch)
    _platform(monkeypatch, "linux", "x86_64")

    engine = _engine(monkeypatch, model=alias)

    assert engine.model_reference == ww._bundled_wakeword_path("onnx")
    assert Path(engine.model_reference).is_file()
    assert Path(engine.model_reference).stat().st_size > 100_000
    assert built[0]["models_actually_loaded"] == [engine.model_reference]
    assert downloaded == [[engine.model_reference]]


def test_the_bundled_alias_follows_the_backend(monkeypatch):
    """A bundled alias picks the artifact for the backend in force.

    On Apple Silicon that is the ``.tflite`` — the one platform where it is the
    file users load, so a selection bug there ships a dead ear.
    """
    _install_fake_openwakeword(monkeypatch)
    _with_tflite_runtime(monkeypatch)
    _platform(monkeypatch, "darwin", "arm64")
    engine = _engine(monkeypatch)
    assert engine.model_reference == ww._bundled_wakeword_path("tflite")
    assert Path(engine.model_reference).is_file()


def test_a_builtin_name_and_a_custom_path_are_passed_through(monkeypatch):
    built, downloaded = _install_fake_openwakeword(monkeypatch)
    _platform(monkeypatch, "linux", "x86_64")

    _engine(monkeypatch, model="hey_jarvis")
    assert built[0]["models_actually_loaded"] == ["hey_jarvis"]
    # The shared feature models are fetched for a custom path too, which is the
    # regression `test_wake_word.py` records.
    assert downloaded[0] == ["hey_jarvis"]

    built.clear()
    downloaded.clear()
    custom = os.path.join("models", "other_phrase.onnx")
    engine = _engine(monkeypatch, model=custom)
    assert engine.model_reference == custom
    assert built[0]["models_actually_loaded"] == [custom]


def test_resolve_model_reference_is_the_engine_s_own_resolution(monkeypatch):
    """A report must not reconstruct the model path — it must read the same one.

    ``verify_on_device.py`` printed ``_bundled_wakeword_path(...)``
    unconditionally, which names the wrong file whenever ``openwakeword.model``
    is set. Both now go through one function.
    """
    _install_fake_openwakeword(monkeypatch)
    _platform(monkeypatch, "linux", "x86_64")

    for cfg in (
        {},
        {"openwakeword": {"model": "hey_jarvis"}},
        {"openwakeword": {"model": "/models/custom.onnx"}},
        {"openwakeword": {"model": "youtab"}},
    ):
        engine = ww._OpenWakeWordEngine({"provider": "openwakeword", **cfg})
        assert ww.resolve_model_reference(cfg) == engine.model_reference


def test_the_shipped_artifacts_are_both_selectable_and_distinct():
    """The current model has to stay operational until a replacement qualifies."""
    onnx = Path(ww._bundled_wakeword_path("onnx"))
    tflite = Path(ww._bundled_wakeword_path("tflite"))
    assert onnx.is_file() and tflite.is_file()
    assert onnx.parent == tflite.parent == WAKEWORDS
    assert onnx.read_bytes() != tflite.read_bytes()
    assert ww._bundled_wakeword_path("TFLite") == str(tflite), "case-insensitive"
    assert ww._bundled_wakeword_path("nonsense") == str(onnx), "unknown → onnx"


# ── the deterministic prime must not be able to look successful when it failed ─


def test_a_preprocessor_without_embeddings_is_reported_not_hidden(monkeypatch, caplog):
    """An engine that cannot prime deterministically still hears — and says so.

    If a future openWakeWord renames ``_get_embeddings``, scores go back to
    depending on an unseeded random draw. That must not be silent, or a parity
    figure measured afterwards would be quietly unreproducible.
    """
    _platform(monkeypatch, "linux", "x86_64")

    class _NoPreprocessorModel:
        def __init__(self, wakeword_models, inference_framework="onnx"):
            self.models = {"hey_youtab": object()}
            self.preprocessor = types.SimpleNamespace()      # no _get_embeddings

        def predict(self, frame):
            return {"hey_youtab": 0.0}

        def reset(self):
            pass

    oww = types.ModuleType("openwakeword")
    oww.utils = types.SimpleNamespace(download_models=lambda names=[]: None)
    model_mod = types.ModuleType("openwakeword.model")
    model_mod.Model = _NoPreprocessorModel
    monkeypatch.setitem(sys.modules, "openwakeword", oww)
    monkeypatch.setitem(sys.modules, "openwakeword.model", model_mod)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: None)

    with caplog.at_level(logging.WARNING, logger="tools.wake_word"):
        engine = ww._OpenWakeWordEngine({"provider": "openwakeword"})

    assert engine._prime_deterministic is False
    assert any("randomly primed" in r.getMessage() for r in caplog.records)
    # It still works as a listener.
    assert engine.process([0] * ww._OpenWakeWordEngine.frame_length) is False
