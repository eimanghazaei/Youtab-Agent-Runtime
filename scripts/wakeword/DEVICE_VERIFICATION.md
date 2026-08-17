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
either file.

| platform | automated qualification | device run |
|---|---|---|
| Linux (CI) | done — `wake-word-backends (ubuntu-latest)` | n/a, headless |
| Windows | done — `wake-word-backends (windows-latest)` | **pending** — needs a physical machine |
| macOS (Apple Silicon) | **prepared, off** — the `macos-latest` leg is written into `wake-word-backends` and commented out; enabling it is deleting two comment markers (Owner decision, see below) | **pending** — needs a physical machine |
| macOS (Intel) | **not possible at the shipped pins** — see below | **pending** — needs a physical machine |

The Apple-Silicon leg is no longer a paragraph the Owner has to translate into
YAML. `.github/workflows/youtab-ci.yml` carries it as a commented `include:`
entry inside the `wake-word-backends` matrix; deleting the two `# ` markers is
the whole change, and no step needs editing because every pin in that job has a
`macosx_*_arm64` wheel. `verify_backends.py` needs nothing either: it already
reads Darwin's `ru_maxrss` as bytes rather than kilobytes, and the one field of
the report that legitimately differs there — `environment.runtime_framework`,
which resolves to `tflite` on Apple Silicon and `onnx` elsewhere — is asserted
against `tools.wake_word.default_inference_framework()` rather than a hardcoded
value. So the leg produces the same `backend-report.json` schema as the other
two, on the same fixtures, with the same nine checks.

Two things gate it. The first is cost: this repository is private, where GitHub
bills macOS minutes at 10x, and whether that budget exists is not something the
repository can see. The second is that turning it on is two steps — once the leg
is green on `main`, `wake-word-backends (macos-latest)` has to be added to the
required status checks in the `main-required-gates` ruleset, or it is a leg a
green PR is free to delete again.

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

```bash
python scripts/wakeword/verify_on_device.py --listen --seconds 60 \
    --device "<input>" --report device-report.json
```

Say "hey youtab" a few times, in your normal voice, from where you would
normally sit. Then talk about something else for the rest of the minute and
confirm it stays quiet. Every activation is printed with its timestamp.

## Platform notes that are not permissions

**macOS on Apple Silicon runs a different file.** openWakeWord's ONNX embedding
model returns near-zero scores on ARM64 (dscripka/openWakeWord#336), so
`tools/wake_word.py` selects the tflite backend there and refuses to fall back
to a backend it knows is dead. `--list-devices` prints
`inference_framework`; on an M-series Mac it must say `tflite`. If it says
`onnx`, the listener will arm and never fire, and that is the bug to chase
rather than the model.

**Windows sample rates.** The engine asks for 16 kHz mono. WASAPI shared mode
resamples from whatever the device is running at, which is fine. Exclusive mode
does not, and a device that cannot do 16 kHz natively will fail to open — if
`--check-capture` raises rather than returning silence, that is what happened.

**Both.** The wake word is off by default. These commands construct the engine
directly and do not depend on `wake_word.enabled`, so a passing run here does
not mean the feature is switched on for users.

## Recording the result

Every mode appends to the same `--report` file. Attach it to the qualification
record, along with the machine, OS version and microphone used. The report
includes the resolved inference framework, the model path and the sensitivity
in force, so a result can be tied to the artifact that produced it —
`tools/wakewords/SHA256SUMS` names those bytes.
