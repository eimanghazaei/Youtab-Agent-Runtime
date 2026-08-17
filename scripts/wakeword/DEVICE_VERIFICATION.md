# Verifying "hey youtab" on a real microphone

Everything between the model file and the answer is qualified without
hardware. `tests/tools/test_wake_word_model_assets.py` runs the shipped ONNX
and tflite artifacts on committed held-out audio in required CI, and
`scripts/wakeword/evaluate_model.py` measures them over fifteen hours of
held-out speech through the product's own engine.

What none of that touches is the **capture path**, and that is where wake words
fail in the field. A microphone can open successfully and return silence. An OS
can resample 48 kHz to 16 kHz badly, or not at all. A permission dialog can be
dismissed once and never shown again. None of those are model defects and none
of them are visible to any test that starts from a WAV file.

This is the procedure for checking that path on the two desktop platforms.
Run it on real hardware, on each OS, and keep the JSON report.

## Status

"Automated qualification" here means: **on that operating system**, both
shipped artifacts were loaded, scored over the committed fixtures, checked
against each other, and timed. Until the `wake-word-backends` job existed this
column said "done" everywhere while only `ubuntu-latest` had ever executed
either file. Read the column as being about *that* claim only — it says nothing
about a microphone.

| platform | automated qualification | device run |
|---|---|---|
| Linux (CI) | done — `wake-word-backends (ubuntu-latest)` | n/a, headless |
| Windows | done — `wake-word-backends (windows-latest)` | **pending** — needs a physical machine |
| macOS (Apple Silicon) | done — `wake-word-backends (macos-latest)`, active in the matrix | **pending** — needs a physical machine |
| macOS (Intel) | **not possible at the shipped pins** — see below | **pending** — needs a physical machine |

**No microphone has ever been opened by anything in this repository, on any
platform.** Every device-run cell above is pending, and the only thing that can
change one is an attached report from §5 below. Nothing in CI can produce one:
the runners have no input device and no speaker.

The Apple-Silicon leg is now active. `.github/workflows/youtab-ci.yml` carries
`macos-latest` as an `include:` entry inside the `wake-word-backends` matrix —
this document used to describe it as written-and-commented-out, which stopped
being true when the leg was enabled. No step needed editing, because every pin
in that job has a `macosx_*_arm64` wheel, and `verify_backends.py` needed
nothing either: it already reads Darwin's `ru_maxrss` as bytes rather than
kilobytes, and the one field of the report that legitimately differs there —
`environment.runtime_framework`, which resolves to `tflite` on Apple Silicon and
`onnx` elsewhere — is asserted against
`tools.wake_word.default_inference_framework()` rather than a hardcoded value.
So the leg produces the same `backend-report.json` schema as the other two, on
the same fixtures, with the same nine checks.

One thing about it is still outstanding, and it is not the YAML: the required
status checks in the `main-required-gates` ruleset are
`wake-word-backends (ubuntu-latest)` and `wake-word-backends (windows-latest)`.
`wake-word-backends (macos-latest)` runs but does **not** gate, so a green PR is
free to delete it. Adding it to the ruleset is an Owner action outside the
repository. Cost is the reason it was deferred rather than an oversight: this
repository is private, where GitHub bills macOS minutes at 10x.

## The checklist, and what the result looks like

The device run is the one part of this that a person has to perform, so its
result is emitted as data rather than described in prose. Every item below has an
identifier that appears in the report, a mode that fills it in, and a pass
condition computed from the report's own numbers — so re-running
`evaluate_checklist` over an attached report reproduces the verdict instead of
taking it on trust.

| id | mode | passes when |
|---|---|---|
| `WW-D1` | `--list-devices` | The OS offers at least one input device and the product's selection resolves to one of them. |
| `WW-D2` | `--check-capture` | The selected device opens and delivers audio above `tools.wake_word._SILENCE_PEAK`. |
| `WW-D3` | `--check-capture` | The samples come back as **int16** at the requested 16 kHz mono. The device's native rate is recorded too, so any OS resampling is stated rather than assumed. |
| `WW-D4` | `--play-fixture` / `--listen` | The engine really built on this platform's expected backend, and the artifact it loaded matches `tools/wakewords/SHA256SUMS`. |
| `WW-D5` | `--play-fixture` | No `near_phrase` clip fires, and every `positive_clean` / `positive_reverberant` clip does. Missed `positive_noisy` clips are counted but do not gate — see §3. |
| `WW-D6` | `--listen` | The run saw at least `--expect-activations` activations. Run it twice: once saying the phrase with `--expect-activations 1`, once talking about something else with `--expect-activations 0`. |

An item that was not run reports `not-run` and contributes nothing;
`verdict.passed` is computed over the items that did run, and is false when
nothing ran at all. The process exit code follows `verdict.passed`, so a run can
be gated on in a script.

The record is a JSON document named `youtab.wakeword.device-verification`
(currently version 2), validated against its own schema before it is written — a
malformed record is refused rather than saved, because a file that exists gets
attached to a qualification and nobody re-reads its keys. To see the schema:

```bash
python scripts/wakeword/verify_on_device.py --print-schema
```

Its shape, in brief:

```json
{
  "schema":      {"name": "youtab.wakeword.device-verification", "version": 2},
  "environment": {"system": "...", "machine": "...",
                  "inference_framework": "onnx|tflite",
                  "platform_default_framework": "onnx|tflite",
                  "model": "...", "sensitivity": 0.6, "confirmation_frames": 3,
                  "sample_rate": 16000, "frame_samples": 1280,
                  "frame_dtype": "int16"},
  "engine":      {"inference_framework": "onnx|tflite", "model": "...",
                  "model_sha256": "...", "matches_recorded_sha256": true,
                  "matches_platform_default": true,
                  "prime_deterministic": true},
  "devices":     {"devices": [], "configured_input_device": null, "selected": 0},
  "capture":     {"peak": 0, "rms": 0.0, "silent": false,
                  "requested_samplerate": 16000, "delivered_dtype": "int16",
                  "device_default_samplerate": 48000.0,
                  "os_is_resampling": true},
  "fixture_playback": {"clips": 33, "near_phrase_activations": 0,
                       "missed_easy_positives": 0, "results": []},
  "listen":      {"activations": 0, "at_seconds": [],
                  "expected_activations": 1},
  "checklist":   {"WW-D1": {"status": "pass|fail|not-run", "detail": "..."}},
  "verdict":     {"passed": true, "ran": [], "failed": [], "not_run": []}
}
```

`environment.inference_framework` is a **config resolution**, not proof: it says
which backend this machine would ask for. `engine.inference_framework` is what
was actually built, after the macOS ARM64 coercion and after any downgrade for a
missing tflite runtime. Those two can legitimately differ, and only the second
one licenses the word "tflite" in a result. That is why `WW-D4` is judged on the
engine record and not on the environment.

macOS Intel is not a scheduling problem. `onnxruntime==1.27.0` and
`ai-edge-litert==2.1.6`, the versions the `wake` extra pins, publish **no macOS
x86_64 wheel**: onnxruntime dropped its `universal2` build after 1.22.0 and
ai-edge-litert has only ever shipped `macosx_*_arm64` for Darwin. `pip install
-e ".[wake]"` therefore cannot succeed on an Intel Mac, so there is nothing to
qualify there until that pin changes. That is a product decision, recorded here
rather than worked around.

The device runs are pending because the build environment has no audio
hardware: there is no input device to open and no speaker to play at it. Every
step that does not require a microphone has been completed and is enforced by
CI. Nothing below is a workaround for a missing model — the model is trained,
measured and shipping.

## 0. Both artifacts, before any hardware

```bash
python scripts/wakeword/verify_backends.py --report backend-report.json
```

No microphone, no speaker, no network. It loads `hey_youtab.onnx` and
`hey_youtab.tflite`, scores the 33 committed fixtures with each, and fails if
they disagree, if a positive is missed, if a negative fires, or if p95
inference latency reaches openWakeWord's 80 ms rescoring interval. It also
records per-backend load time and peak RSS, which is what makes a device
budget a measurement rather than an assumption.

Run this first. It is the same code CI runs, so a failure here is about this
machine — a wheel built for another architecture, a runtime that lowers a
kernel differently — and not about the capture path everything below tests.

**On an Apple Silicon Mac this is the step that matters most**, because it is
the only platform where `hey_youtab.tflite` is the file the product loads. Until
a macOS runner is enabled, one local run of this command, with its
`backend-report.json` attached to the qualification record, is the evidence.

## Before you start

```bash
uv pip install -e ".[wake,voice]"
```

`sounddevice` needs PortAudio, which the wheel bundles on both platforms. If
the import fails, the script says so and stops rather than pretending.

## 1. What the OS is offering

```bash
python scripts/wakeword/verify_on_device.py --list-devices --report device-report.json
```

Prints every input device with its host API, and which one the product would
pick — `wake_word.input_device` from config if set, otherwise the host default.

**Windows.** Expect several entries per physical microphone, one per host API:
MME, Windows DirectSound, and WASAPI. Prefer WASAPI: it is the modern path and
the only one that reports the true device sample rate. MME silently resamples
and caps at 44.1 kHz, which is survivable but adds latency. If a microphone is
missing entirely, it is disabled in Sound settings, not broken.

**macOS.** Expect one entry per device under Core Audio. A Bluetooth headset
appears twice — once as an output-only A2DP profile and once as a
bidirectional HFP/SCO profile that drops the microphone to 8 or 16 kHz and
sounds like a telephone. The wake word works on it, but measurably worse than
on the built-in microphone; verify both if your users use both.

## 2. Prove the device is actually delivering audio

```bash
python scripts/wakeword/verify_on_device.py --check-capture --seconds 5 \
    --device "<name or index>" --report device-report.json
```

Speak or tap the desk while it records. It reports peak and RMS level and calls
the device **dead** if the peak never exceeds `tools.wake_word._SILENCE_PEAK`
— the same threshold the product uses for its own dead-microphone warning.

A dead result here is a permission problem in almost every case:

**Windows.** Settings → Privacy & security → Microphone. Both *Microphone
access* and *Let desktop apps access your microphone* must be on; the second is
separate and is the one that catches people, because a terminal is a desktop
app. Also check the device is not muted in Sound → Recording → Properties →
Levels, where a muted device still opens without error.

**macOS.** System Settings → Privacy & Security → Microphone, and grant the
**terminal application** — Terminal, iTerm2, VS Code, whichever is the parent
process — not Python. macOS attributes the request to the enclosing app bundle.
A process denied the entitlement receives a stream of zeros rather than an
error, which is exactly the "opens fine, hears nothing" case. If the app is not
listed, the prompt was dismissed; `tccutil reset Microphone` makes it ask
again.

## 3. The acoustic loop

```bash
python scripts/wakeword/verify_on_device.py --play-fixture \
    --device "<input>" --output-device "<speaker>" --report device-report.json
```

Plays each of the 33 committed fixture clips through the speaker, captures them
back through the microphone, and scores them with the real engine. These are
the model's own held-out clips, so what they should do is already known: the
manifest carries the expected outcome per clip, and the script prints
`ok`/`MISMATCH` per row.

Do this in a quiet room at a normal listening level, with the speaker roughly
where a person's mouth would be. It is a check on the capture path, not a
repeat of the false-accept measurement — playing 33 clips at a microphone
cannot measure a per-hour rate, and this document does not claim it can.

Expected: every `positive_*` clip fires and nothing else does. A few misses on
the `positive_noisy` clips (down to 1.1 dB SNR) are acceptable over a real
acoustic path, since the speaker and room add their own noise on top of the
noise already mixed in. False activations on the `near_phrase` clips are not
acceptable and should be reported.

## 4. Say it yourself

Two runs, because "it fires when I say it" and "it stays quiet when I don't" are
different claims and each has to be checkable on its own.

```bash
python scripts/wakeword/verify_on_device.py --listen --seconds 60 \
    --expect-activations 1 --device "<input>" --report device-said.json
```

Say "hey youtab" a few times, in your normal voice, from where you would
normally sit. Every activation is printed with its timestamp.

```bash
python scripts/wakeword/verify_on_device.py --listen --seconds 60 \
    --expect-activations 0 --device "<input>" --report device-quiet.json
```

Now talk about something else — ordinary conversation, at the same distance —
and confirm it stays quiet. Keep both reports; `WW-D6` is judged separately in
each.

## Platform notes that are not permissions

**macOS on Apple Silicon runs a different file.** openWakeWord's ONNX embedding
model returns near-zero scores on ARM64 (dscripka/openWakeWord#336), so
`tools/wake_word.py` selects the tflite backend there and refuses to fall back to
a backend it knows is dead.

Read the right field. Every mode prints `environment.inference_framework`, and on
an M-series Mac it will say `tflite` — but that is the *config resolution*, and
it says `tflite` whether or not the tflite runtime is installed. It is not
evidence that the backend loaded. The field that is evidence is
`engine.inference_framework`, which only the modes that build an engine
(`--play-fixture`, `--listen`) can report. So on Apple Silicon, `--list-devices`
alone cannot qualify the backend; step 3 or step 4 is what does, and `WW-D4` is
the item that records it.

If the tflite runtime is missing on that machine the engine does not quietly fall
back — it raises, naming `pip install ai-edge-litert`. Elsewhere (Linux, Windows)
a missing tflite runtime downgrades to ONNX with a warning, and
`engine.inference_framework` then reads `onnx`: a run in that state is an ONNX
result and the report says so.

**Windows sample rates.** The engine asks for 16 kHz mono. WASAPI shared mode
resamples from whatever the device is running at, which is fine. Exclusive mode
does not, and a device that cannot do 16 kHz natively will fail to open — if
`--check-capture` raises rather than returning silence, that is what happened.

**Both.** The wake word is off by default. These commands construct the engine
directly and do not depend on `wake_word.enabled`, so a passing run here does
not mean the feature is switched on for users.

## Recording the result

Each invocation writes a complete record to its `--report` path — it is not
appended to, so use one file per run (or run several modes in one invocation and
get one file covering them all).

Attach the reports to the qualification record, along with the machine, OS
version and microphone used. Everything needed to tie a result to the bytes that
produced it is in the file: `engine.model`, `engine.model_sha256` and
`engine.matches_recorded_sha256`, checked against `tools/wakewords/SHA256SUMS`.

To check a report someone else produced, without a microphone:

```python
import importlib.util, json
spec = importlib.util.spec_from_file_location(
    "v", "scripts/wakeword/verify_on_device.py")
tool = importlib.util.module_from_spec(spec); spec.loader.exec_module(tool)

record = json.loads(open("device-report.json", encoding="utf-8").read())
assert tool.validate(record) == []                       # conforms to the schema
assert tool.evaluate_checklist(record) == record["checklist"]   # verdict recomputes
```

If the second assertion fails, the checklist in the file was not derived from the
measurements in the file.
