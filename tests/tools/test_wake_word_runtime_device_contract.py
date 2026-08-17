"""The hardware-only half, made checkable without hardware.

Nothing in this repository can verify a microphone. What it can do is make the
*result* of someone else's microphone run verifiable rather than attested: a
fixed checklist of items with identifiers, a schema the emitted record is
validated against before it is written, a verdict computed from the record's own
numbers, and an exit code that follows the verdict.

So this file tests the parts of ``scripts/wakeword/verify_on_device.py`` that do
not need audio:

* the schema is internally consistent, and the validator actually rejects things;
* every checklist item is derived from data in the record, so re-running
  ``evaluate_checklist`` over an attached report reproduces its verdict;
* an unrun mode reports ``not-run`` and never contributes a pass;
* the frame feeder re-blocks the device's chunks without losing audio at a seam,
  and records the dtype it was given instead of coercing it;
* the engine record names the backend that was actually built, so a report
  cannot label an ONNX run "tflite";
* ``DEVICE_VERIFICATION.md`` and the tool agree about the checklist, and the
  document's CI claims agree with the workflow.

The last one exists because that document has overstated coverage before: it
said Windows and macOS automated qualification were "done" while only
``ubuntu-latest`` had ever executed either artifact.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[2]
WAKEWORDS = REPO / "tools" / "wakewords"
DOC = REPO / "scripts" / "wakeword" / "DEVICE_VERIFICATION.md"
WORKFLOW = REPO / ".github" / "workflows" / "youtab-ci.yml"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location(
        "_verify_on_device", REPO / "scripts" / "wakeword" / "verify_on_device.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── a fully populated, all-passing record to mutate ──────────────────────────


def _artifact_digest(name: str) -> str:
    import hashlib

    return hashlib.sha256((WAKEWORDS / name).read_bytes()).hexdigest()


@pytest.fixture
def record(tool) -> dict:
    """What a complete, passing Owner run emits, built from real artifact bytes."""
    onnx = str(WAKEWORDS / "hey_youtab.onnx")
    return {
        "schema": {"name": tool.SCHEMA_NAME, "version": tool.SCHEMA_VERSION},
        "environment": {
            "platform": "Windows-11-10.0.26200-SP0",
            "system": "Windows",
            "machine": "AMD64",
            "python": "3.12.11",
            "inference_framework": "onnx",
            "platform_default_framework": "onnx",
            "model": onnx,
            "sensitivity": 0.6,
            "confirmation_frames": 3,
            "sample_rate": 16000,
            "frame_samples": 1280,
            "frame_dtype": "int16",
        },
        "engine": {
            "inference_framework": "onnx",
            "model": onnx,
            "model_sha256": _artifact_digest("hey_youtab.onnx"),
            "recorded_sha256": _artifact_digest("hey_youtab.onnx"),
            "matches_recorded_sha256": True,
            "matches_platform_default": True,
            "prime_deterministic": True,
        },
        "devices": {
            "devices": [
                {"index": 1, "name": "Microphone Array", "host_api": "Windows WASAPI",
                 "max_input_channels": 2, "default_samplerate": 48000.0,
                 "is_host_default": True},
            ],
            "configured_input_device": None,
            "selected": 1,
        },
        "capture": {
            "seconds": 5.0,
            "peak": 8123,
            "rms": 812.5,
            "peak_dbfs": -12.1,
            "silent": False,
            "verdict": "live",
            "requested_samplerate": 16000,
            "requested_channels": 1,
            "requested_dtype": "int16",
            "delivered_dtype": "int16",
            "delivered_channels": 1,
            "device_default_samplerate": 48000.0,
            "os_is_resampling": True,
        },
        "fixture_playback": {
            "clips": 33,
            "missed_wake_words": 2,
            "false_activations": 0,
            "near_phrase_activations": 0,
            "easy_positives": 6,
            "missed_easy_positives": 0,
            "frames_scored": 900,
            "delivered_dtype": "int16",
            "results": [],
        },
        "listen": {
            "seconds": 60.0,
            "activations": 3,
            "at_seconds": [4.1, 21.5, 39.9],
            "expected_activations": 1,
            "frames_scored": 750,
            "delivered_dtype": "int16",
        },
    }


# ── the schema, and a validator that really validates ────────────────────────


def test_the_schema_is_serialisable_and_internally_consistent(tool):
    json.loads(json.dumps(tool.REPORT_SCHEMA))          # no non-JSON values

    def _walk(schema, path="$"):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            assert key in properties, f"{path}: required {key!r} has no property schema"
        for key, sub in properties.items():
            if isinstance(sub, dict):
                _walk(sub, f"{path}.{key}")

    _walk(tool.REPORT_SCHEMA)
    assert tool.REPORT_SCHEMA["title"] == tool.SCHEMA_NAME
    assert isinstance(tool.SCHEMA_VERSION, int) and tool.SCHEMA_VERSION >= 1


def test_a_complete_record_validates(tool, record):
    record["checklist"] = tool.evaluate_checklist(record)
    record["verdict"] = {"passed": True, "ran": ["WW-D1"], "failed": [], "not_run": []}
    assert tool.validate(record) == []


@pytest.mark.parametrize("missing", ["schema", "environment", "checklist", "verdict"])
def test_a_record_missing_a_top_level_section_is_rejected(tool, record, missing):
    record["checklist"] = {}
    record["verdict"] = {"passed": False, "ran": [], "failed": [], "not_run": []}
    record.pop(missing)
    problems = tool.validate(record)
    assert any(missing in problem for problem in problems), problems


def test_the_validator_rejects_wrong_types_and_bad_enums(tool, record):
    record["checklist"] = {}
    record["verdict"] = {"passed": False, "ran": [], "failed": [], "not_run": []}
    assert tool.validate(record) == []

    broken = copy.deepcopy(record)
    broken["environment"]["confirmation_frames"] = "three"
    assert tool.validate(broken)

    broken = copy.deepcopy(record)
    broken["environment"]["inference_framework"] = "tensorflow"
    assert tool.validate(broken)

    broken = copy.deepcopy(record)
    broken["capture"]["silent"] = "no"
    assert tool.validate(broken)

    broken = copy.deepcopy(record)
    # ``True`` is an int in Python and is not one in JSON. A validator that let
    # this through would accept a boolean wherever a count belongs.
    broken["fixture_playback"]["false_activations"] = True
    assert tool.validate(broken)

    broken = copy.deepcopy(record)
    broken["schema"]["name"] = "something.else"
    assert tool.validate(broken)


# ── the checklist ────────────────────────────────────────────────────────────


def test_an_empty_run_claims_nothing(tool):
    """No mode run means no item passed — and no overall pass either."""
    empty = {"environment": {"platform_default_framework": "onnx"}}
    checklist = tool.evaluate_checklist(empty)
    assert set(checklist) == set(tool.CHECKLIST)
    assert {item["status"] for item in checklist.values()} == {"not-run"}
    for key, item in checklist.items():
        assert tool.CHECKLIST[key]["mode"].split()[0] in item["detail"]


def test_every_checklist_item_names_a_flag_the_tool_implements(tool):
    source = (REPO / "scripts" / "wakeword" / "verify_on_device.py").read_text(
        encoding="utf-8")
    declared = set(re.findall(r'add_argument\(\s*"(--[a-z][a-z-]+)"', source))
    for key, meta in tool.CHECKLIST.items():
        flags = re.findall(r"--[a-z][a-z-]+", meta["mode"])
        assert flags, f"{key} names no mode"
        for flag in flags:
            assert flag in declared, f"{key} refers to unimplemented {flag}"


def test_every_item_passes_on_a_good_run(tool, record):
    checklist = tool.evaluate_checklist(record)
    assert {k: v["status"] for k, v in checklist.items()} == {
        "WW-D1": "pass", "WW-D2": "pass", "WW-D3": "pass",
        "WW-D4": "pass", "WW-D5": "pass", "WW-D6": "pass",
    }


def test_a_dead_microphone_fails_the_capture_item(tool, record):
    record["capture"].update(silent=True, peak=0, verdict="dead microphone")
    checklist = tool.evaluate_checklist(record)
    assert checklist["WW-D2"]["status"] == "fail"
    assert "dead microphone" in checklist["WW-D2"]["detail"]
    # ...but the format item is about the format, and is still satisfied.
    assert checklist["WW-D3"]["status"] == "pass"


def test_a_float_feed_fails_the_format_item(tool, record):
    """The 32768x-too-quiet case has to be visible in the record.

    A device that hands back float32 makes every score collapse near zero. If
    the report only carried the peak, that would read as a dead microphone and
    the operator would go looking at permissions.
    """
    record["capture"]["delivered_dtype"] = "float32"
    checklist = tool.evaluate_checklist(record)
    assert checklist["WW-D3"]["status"] == "fail"
    assert "float32" in checklist["WW-D3"]["detail"]


def test_a_stereo_delivery_fails_the_format_item(tool, record):
    record["capture"]["delivered_channels"] = 2
    assert tool.evaluate_checklist(record)["WW-D3"]["status"] == "fail"


def test_the_recorded_native_rate_makes_os_resampling_explicit(tool, record):
    """Resampling is allowed; being silent about it is not."""
    checklist = tool.evaluate_checklist(record)
    assert "48000.0" in checklist["WW-D3"]["detail"]
    assert "resampling: True" in checklist["WW-D3"]["detail"]


def test_a_backend_that_is_not_the_platform_default_fails(tool, record):
    """The macOS ARM64 trap, in report form.

    On Apple Silicon the platform default is tflite. A record from that machine
    saying the engine built on onnx is a listener that arms and never fires
    (openWakeWord #336) — a failure, not a footnote.
    """
    record["environment"].update(system="Darwin", machine="arm64",
                                 platform_default_framework="tflite")
    record["engine"].update(inference_framework="onnx",
                            matches_platform_default=False)
    checklist = tool.evaluate_checklist(record)
    assert checklist["WW-D4"]["status"] == "fail"
    assert "onnx" in checklist["WW-D4"]["detail"]
    assert "tflite" in checklist["WW-D4"]["detail"]


def test_an_artifact_that_is_not_the_shipped_one_fails(tool, record):
    record["engine"]["matches_recorded_sha256"] = False
    assert tool.evaluate_checklist(record)["WW-D4"]["status"] == "fail"


def test_a_near_miss_activation_fails_the_acoustic_loop(tool, record):
    record["fixture_playback"].update(false_activations=1, near_phrase_activations=1)
    checklist = tool.evaluate_checklist(record)
    assert checklist["WW-D5"]["status"] == "fail"


def test_missed_noisy_positives_do_not_fail_the_acoustic_loop(tool, record):
    """Stated in the document and enforced here, so the two cannot drift.

    Replaying a 1.1 dB SNR clip through a speaker into a room adds noise on top
    of noise already mixed in. Gating on those would make the check a measure of
    the room.
    """
    record["fixture_playback"].update(missed_wake_words=5, missed_easy_positives=0)
    assert tool.evaluate_checklist(record)["WW-D5"]["status"] == "pass"

    record["fixture_playback"]["missed_easy_positives"] = 1
    assert tool.evaluate_checklist(record)["WW-D5"]["status"] == "fail"


def test_the_live_listen_item_is_checked_in_both_directions(tool, record):
    record["listen"].update(activations=0, expected_activations=1)
    assert tool.evaluate_checklist(record)["WW-D6"]["status"] == "fail"

    # The quiet run: talk about something else and expect nothing.
    record["listen"].update(activations=0, expected_activations=0)
    assert tool.evaluate_checklist(record)["WW-D6"]["status"] == "pass"

    record["listen"].update(activations=2, expected_activations=0)
    assert tool.evaluate_checklist(record)["WW-D6"]["status"] == "pass"


def test_a_device_list_that_resolves_to_nothing_fails(tool, record):
    record["devices"]["selected"] = 7          # not among the listed devices
    assert tool.evaluate_checklist(record)["WW-D1"]["status"] == "fail"

    record["devices"]["selected"] = "Microphone Array"      # by name is fine too
    assert tool.evaluate_checklist(record)["WW-D1"]["status"] == "pass"


# ── the exit code follows the verdict ────────────────────────────────────────


def test_running_no_mode_is_a_failure_not_a_pass(tool, monkeypatch, capsys):
    """The report cannot be made reassuring by asking it nothing."""
    monkeypatch.setattr("sys.argv", ["verify_on_device.py"])
    assert tool.main() == 1
    out = capsys.readouterr().out
    assert "verdict: FAIL" in out
    assert "not-run" in out


def test_print_schema_is_json_and_exits_clean(tool, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["verify_on_device.py", "--print-schema"])
    assert tool.main() == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["title"] == tool.SCHEMA_NAME


def test_the_written_report_is_validated_before_it_is_written(tool, monkeypatch,
                                                             tmp_path, capsys):
    """A malformed record must not reach the filesystem.

    A file that exists gets attached to a qualification record; nobody re-reads
    it to check that its keys are the ones the schema names.
    """
    report = tmp_path / "device-report.json"
    monkeypatch.setattr(
        "sys.argv", ["verify_on_device.py", "--report", str(report)])
    monkeypatch.setattr(tool, "_environment", lambda: {"platform": "x"})
    assert tool.main() == 2
    assert not report.exists()
    assert "does not match its own schema" in capsys.readouterr().out


def test_a_run_that_only_lists_devices_writes_a_valid_report(tool, monkeypatch,
                                                            tmp_path):
    """The one mode that needs no live microphone, end to end.

    ``list_devices`` is stubbed because PortAudio is not present in CI — but
    everything after it, including validation and the written bytes, is real.
    """
    report = tmp_path / "device-report.json"
    monkeypatch.setattr(tool, "list_devices", lambda: {
        "devices": [{"index": 0, "name": "Fake Mic", "host_api": "ALSA",
                     "max_input_channels": 1, "default_samplerate": 44100.0,
                     "is_host_default": True}],
        "configured_input_device": None,
        "selected": 0,
    })
    monkeypatch.setattr(
        "sys.argv",
        ["verify_on_device.py", "--list-devices", "--report", str(report)],
    )
    assert tool.main() == 0

    written = json.loads(report.read_text(encoding="utf-8"))
    assert tool.validate(written) == []
    assert written["schema"] == {"name": tool.SCHEMA_NAME,
                                "version": tool.SCHEMA_VERSION}
    assert written["verdict"]["ran"] == ["WW-D1"]
    assert written["verdict"]["passed"] is True
    # And it still claims nothing about the microphone it never opened.
    for key in ("WW-D2", "WW-D3", "WW-D4", "WW-D5", "WW-D6"):
        assert written["checklist"][key]["status"] == "not-run"


# ── the frame feeder: no audio lost at a callback seam ───────────────────────


class _Recorder:
    def __init__(self):
        self.frames: list[np.ndarray] = []
        self.resets = 0

    def process(self, frame) -> bool:
        self.frames.append(np.asarray(frame).copy())
        return False

    def reset(self) -> None:
        self.resets += 1


def test_the_feeder_loses_no_audio_across_callback_seams(tool):
    """The bug this replaced dropped up to 79 ms at every callback boundary.

    ``range(0, len(block) - FRAME + 1, FRAME)`` scored whole frames and threw
    the remainder away, restarting alignment at zero on the next callback. Over a
    phrase that is most of a frame missing, repeatedly — and it would read as a
    weak model rather than a broken feed.
    """
    rng = np.random.default_rng(3)
    blocks = [rng.integers(-8000, 8000, n, dtype=np.int16)
              for n in (1000, 1500, 700, 1280, 61, 4096)]
    engine = _Recorder()
    feeder = tool._FrameFeeder(engine)

    for block in blocks:
        feeder.feed(block.reshape(-1, 1))

    source = np.concatenate(blocks)
    scored = np.concatenate(engine.frames)
    assert all(len(f) == tool.FRAME for f in engine.frames)
    assert np.array_equal(scored, source[:len(scored)])
    # Everything except a final partial frame was scored, exactly once.
    assert len(source) - len(scored) == len(source) % tool.FRAME
    assert feeder.frames == len(source) // tool.FRAME
    # Non-vacuity: the block sizes really are not frame-aligned.
    assert any(len(b) % tool.FRAME for b in blocks)


def test_the_feeder_reset_drops_the_carry_with_the_engine_state(tool):
    """Between fixture clips nothing may bleed across.

    The carry is audio from the previous clip. Keeping it would prepend the tail
    of clip N to clip N+1 and score a phrase that was never spoken.
    """
    engine = _Recorder()
    feeder = tool._FrameFeeder(engine)
    feeder.feed(np.full(700, 111, dtype=np.int16))
    assert engine.frames == []                      # still carried, not scored

    feeder.reset()
    assert engine.resets == 1
    feeder.feed(np.full(1280, 222, dtype=np.int16))
    assert len(engine.frames) == 1
    assert set(engine.frames[0].tolist()) == {222}, "clip N leaked into clip N+1"


def test_the_feeder_takes_channel_zero_like_the_detector_does(tool):
    """A host that ignores ``channels=1`` must not have its channels interleaved.

    Same choice as ``WakeWordDetector._run``'s ``data[:, 0]``: flattening a
    (frames, 2) buffer would feed alternating samples from two microphones.
    """
    engine = _Recorder()
    feeder = tool._FrameFeeder(engine)
    left = np.full(tool.FRAME, 700, dtype=np.int16)
    right = np.full(tool.FRAME, 31000, dtype=np.int16)
    feeder.feed(np.stack([left, right], axis=1))
    assert len(engine.frames) == 1
    assert set(engine.frames[0].tolist()) == {700}


def test_the_feeder_records_the_dtype_instead_of_coercing_it(tool):
    """``.astype(np.int16)`` on a float32 block truncates everything to zero.

    That was in this file: a mis-configured float feed would have been silently
    turned into silence and reported as a dead microphone. The dtype is now
    carried into the record so WW-D3 can fail on it.
    """
    engine = _Recorder()
    feeder = tool._FrameFeeder(engine)
    loud = np.full(1280, 0.5, dtype=np.float32)      # loud, in float scale
    feeder.feed(loud)
    assert feeder.dtype == "float32"
    assert not np.array_equal(engine.frames[0], np.zeros(1280, dtype=np.int16))

    engine = _Recorder()
    feeder = tool._FrameFeeder(engine)
    feeder.feed(np.full(1280, 900, dtype=np.int16))
    assert feeder.dtype == "int16"


# ── the engine record cannot label an ONNX run "tflite" ──────────────────────


class _FakeEngine:
    def __init__(self, framework: str, model: str):
        self.inference_framework = framework
        self.model_reference = model
        self._prime_deterministic = True


def test_the_engine_record_names_the_backend_that_was_built(tool):
    import tools.wake_word as ww

    for framework in ("onnx", "tflite"):
        engine = _FakeEngine(framework, ww._bundled_wakeword_path(framework))
        entry = tool._engine_record(engine, ww)
        assert entry["inference_framework"] == framework
        assert entry["model"].endswith(f".{framework}")
        assert entry["model_sha256"] == _artifact_digest(f"hey_youtab.{framework}")
        assert entry["matches_recorded_sha256"] is True
        assert entry["matches_platform_default"] == (
            framework == ww.default_inference_framework())


def test_a_downgraded_engine_is_reported_as_what_it_actually_is(tool):
    """The lie this prevents.

    A machine configured for tflite with no tflite runtime builds an ONNX
    engine. If the record took the framework from config it would say "tflite",
    and the ``.tflite`` artifact — which is what Apple Silicon loads in
    production — would appear qualified by a run that never touched it.
    """
    import tools.wake_word as ww

    engine = _FakeEngine("onnx", ww._bundled_wakeword_path("onnx"))
    entry = tool._engine_record(engine, ww)
    assert entry["inference_framework"] == "onnx"
    assert entry["model"].endswith(".onnx")
    assert entry["model_sha256"] != _artifact_digest("hey_youtab.tflite")


def test_a_model_that_is_not_the_shipped_artifact_is_visible(tool, tmp_path):
    import tools.wake_word as ww

    impostor = tmp_path / "hey_youtab.onnx"
    impostor.write_bytes(b"not the shipped model")
    entry = tool._engine_record(_FakeEngine("onnx", str(impostor)), ww)
    assert entry["matches_recorded_sha256"] is False
    assert entry["recorded_sha256"] == _artifact_digest("hey_youtab.onnx")


# ── the document and the tool must agree ─────────────────────────────────────


def test_the_document_carries_every_checklist_id_and_no_others(tool):
    text = DOC.read_text(encoding="utf-8")
    documented = set(re.findall(r"\bWW-D\d+\b", text))
    assert documented == set(tool.CHECKLIST), (
        f"documented but not implemented: {sorted(documented - set(tool.CHECKLIST))}; "
        f"implemented but not documented: {sorted(set(tool.CHECKLIST) - documented)}"
    )


def test_the_document_names_the_schema_the_tool_emits(tool):
    text = DOC.read_text(encoding="utf-8")
    assert tool.SCHEMA_NAME in text
    assert f"version {tool.SCHEMA_VERSION}" in text or \
        f"\"version\": {tool.SCHEMA_VERSION}" in text
    assert "--print-schema" in text


def test_the_document_does_not_claim_a_device_run_that_never_happened(tool):
    """The overstatement class this document has fallen into before.

    No microphone has been opened by anything in this repository, on any
    platform. Until an Owner attaches a report, every device-run cell has to say
    so, and the count of pending platforms has to match the count of platforms
    in the table.
    """
    text = DOC.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines()
            if line.startswith("|") and "|" in line[1:]
            and not set(line) <= set("|-: ")]
    platform_rows = [r for r in rows if "pending" in r or "n/a" in r]
    assert len(platform_rows) >= 4, rows
    for name in ("Windows", "macOS (Apple Silicon)", "macOS (Intel)"):
        row = [r for r in platform_rows if name in r]
        assert row, f"no status row for {name}"
        assert "pending" in row[0], f"{name}'s device run is not marked pending"


def test_the_documents_ci_claim_matches_the_workflow():
    """Doc→CI binding, in the direction the doc got wrong before.

    The document used to describe the macOS leg as written-but-commented-out.
    The leg is now active in the matrix, so that description would be a
    different kind of wrong — understating coverage — and the same drift that
    once overstated it. Either way the fix is to bind the claim to the file.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow[workflow.find("\n  wake-word-backends:"):
                   workflow.find("\n  javascript:")]
    assert "verify_backends.py" in job, "the workflow slice is wrong; re-anchor"

    matrix = re.search(r"^\s*os:\s*\[(?P<list>[^\]]*)\]", job, re.MULTILINE)
    include = re.search(
        r"^\s*include:\s*$(?P<body>(?:\n\s+.*)+?)(?=\n\s*#|\n\s*\w+:)",
        job, re.MULTILINE)
    active = (matrix.group("list") if matrix else "") + (
        include.group("body") if include else "")

    text = DOC.read_text(encoding="utf-8")
    for runner in ("ubuntu-latest", "windows-latest", "macos-latest"):
        if runner in active:
            assert runner in text, (
                f"{runner} runs the wake-word backends job but the device "
                f"document does not credit it"
            )
    if "macos-latest" in active:
        for stale in ("prepared, off", "commented out", "deleting the two"):
            assert stale not in text, (
                f"the document still describes the macOS leg as {stale!r}, but "
                f"it is active in the workflow matrix"
            )
