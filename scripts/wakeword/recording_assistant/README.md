# Compact "Hey Youtab" recording assistant

A small, **local** Windows tool that walks one human speaker through a short
(~12-16 minute) guided recording sitting for the wake-word project. It shows
each exact phrase one at a time, optionally reads the instruction aloud, records
only the human voice, names every file automatically, remembers progress across
restarts, and **never uploads anything**.

It is an operator tool, not part of the shipped runtime. Two people record with
it: labels **E001** and **E002**.

## What it records

Five independently-resumable sections, ~55 files total:

| Section | Contents | Files |
|--------:|----------|------:|
| 1 | "Hey Youtab" close-mic, five deliveries (normal/slow/fast/quiet/loud) x2 | 10 |
| 2 | Far-field x3, plus two real-noise sources x3 each | 9 |
| 3 | The 34-row canonical near-phrase battery, x1 each | 34 |
| 4 | Free speech, one continuous file (~2-3 min) | 1 |
| 5 | Background only, one continuous file (~1-2 min) | 1 |

Noise sources are split between the two speakers so the pair cover all four:
**E001 -> tv, kitchen**; **E002 -> street, fan**.

The phrase list and every folder/file name come from
`speaker_recording_spec.py`, so a folder recorded here is named exactly the way
`validate_speaker_submission.py` and `import_speaker.py` expect. You never type a
filename.

## Install

Use an **isolated operator venv** on Python 3.12 so the operator libraries never
touch the project's locked runtime deps. From the repo root, once:

```
py -3.12 -m venv .venv-recorder
.\.venv-recorder\Scripts\python.exe -m pip install -r scripts/wakeword/recording_assistant/requirements-recording-assistant.txt numpy
```

- `sounddevice` - microphone capture and playback.
- `pyttsx3` - offline Windows SAPI5 voice guidance (optional; if it is not
  installed the on-screen phrase is still shown).
- `numpy` - already present in the project env; installed into the operator venv
  so the assistant runs entirely out of `.venv-recorder`.

> Do **not** launch with the bare `py`/`python` launcher: on this machine that is
> Python 3.14 with none of these libraries, and `import numpy` fails before the
> window can open. Always run out of `.venv-recorder` (the launchers below do).

## Run

One-click launchers (recommended) - double-click, or from PowerShell at the repo
root:

```
.\scripts\wakeword\recording_assistant\Record-E001.ps1
.\scripts\wakeword\recording_assistant\Record-E002.ps1
```

They run the assistant out of `.venv-recorder`, from the repo root, at 48 kHz
mono 16-bit, and pass extra flags through (`.\Record-E001.ps1 --list-devices`).
The equivalent explicit command:

```
.\.venv-recorder\Scripts\python.exe -m scripts.wakeword.recording_assistant.app --speaker E001 --rate 48000
```

Buttons: **Record**, **Stop**, **Replay**, **Keep**, **Redo**, **Pause**,
**Resume**, plus a voice-guidance toggle. Record speaks the prompt (if guidance
is on) and *then* opens the microphone - the two never overlap. Stop ends the
take; Keep saves it; Redo throws away the just-captured take (only an un-kept
one) and re-records the same step; Replay plays the last take back.

### Use headphones

Turn voice guidance on for hands-free prompts, and **wear headphones**. The
spoken guidance goes to the output device and is never captured or written -
headphones keep it out of the room so it cannot reach the microphone either.

### Microphone setup and device selection

The assistant records at **48 kHz mono 16-bit** by default (any rate the mic
supports at or above the 16 kHz floor; nothing is ever resampled). It picks a
**raw hardware microphone** automatically and deliberately **skips virtual and
"AI noise-cancelling" inputs** - the dataset must be the unprocessed microphone.

Before the first sitting, confirm the mic on this machine:

```
.\Record-E001.ps1 --list-devices     # every capture device; marks the auto-selected mic
.\Record-E001.ps1 --check-gui        # opens and closes the window; proves the GUI works
.\Record-E001.ps1 --mic-check 4      # records 4 s from the mic, writes a WAV under
                                     #   <incoming>/_devicecheck, and reports the level
```

`--mic-check` is the gate: it must print **"OK: real audio captured"** with a
sensible peak level. If it reports almost no signal, Windows has no active
recording device - **connect the microphone/headset you will record with** (a USB
mic, or a 3.5 mm mic into the pink jack), then in **Settings -> System -> Sound ->
Input** confirm it is present and, for a truly raw capture, turn **Audio
enhancements off** on it. Re-run `--mic-check` until it is green.

Override the auto-selected device any time:

```
.\Record-E001.ps1 --input-device 15            # by index (from --list-devices)
.\Record-E001.ps1 --input-device "USB"         # by name fragment
```

### Headless modes (no microphone, no display)

```
py -m scripts.wakeword.recording_assistant.app --speaker E001 --dry
py -m scripts.wakeword.recording_assistant.app --speaker E001 --self-test
```

- `--dry` prints the plan and the exact file each step produces, then exits.
- `--self-test` runs the whole pipeline against a generated sine tone - record,
  quality checks, keep, redo, pause/resume, replay, finalize, manifest and a
  validator pass - proving the flow works before a real sitting. Run this on the
  target machine first.

## Where files go

The app writes under a per-speaker folder in the capture root:

```
<incoming-root>/E001/
  originals/
    positive_normal/hey-youtab_normal_001.wav
    positive_noise_tv/hey-youtab_noise-tv_001.wav
    near_phrase/hey-you-tab_001.wav
    negative_freespeech/freespeech_001.wav
    background_only/background_001.wav
  .recording_state.json     (progress; removed at handoff)
```

The capture root defaults to the `Youtab-Wakeword-Human\incoming` folder on the
`G:` drive, outside the git checkout. Override it with `--incoming <path>` or the
`WAKEWORD_INCOMING_ROOT` environment variable. Nothing under that drive is ever
committed. The signed consent PDF is the coordinator's to place, under the
separate private consent root (`--consent` / `WAKEWORD_CONSENT_ROOT`); the app
never writes audio or consent there.

## Resume, quality, and preservation

- **Resume:** progress is reconciled against the files actually on disk, so
  relaunching continues exactly where the recordings stopped - a state note that
  disagrees with the folder loses to the folder.
- **Quality findings:** each take is checked for silence, too-short, clipping and
  unreadable capture, and the finding is *shown* ("that came out silent - Redo?").
  Nothing is ever auto-deleted or auto-corrected.
- **Originals preserved:** a kept take is written once and never rewritten,
  trimmed, normalised or resampled. A difficult take the speaker chose to keep -
  quiet, distant, noisy, clipped - is retained. Only an explicit Redo of an
  un-kept take discards audio.

## Validate before handoff

Press **Validate** in the app (or read the tail of `--self-test`) to run the
**compact validator** - a purpose-built check that holds the folder to the
compact plan it was recorded from. A complete, correct compact submission comes
back **GREEN** with zero problems; otherwise it lists *only* real faults - a
missing or misnamed take, a wrong-format or sub-16 kHz file, a silent / too-short
/ clipped / unreadable take, a checksum mismatch, or a missing consent/metadata
file. It opens every WAV to do this, because it runs on the machine that just
captured the audio.

This is deliberately separate from `scripts/wakeword/validate_speaker_submission.py`,
which is the **E003-E007 full round's** validator: run against a compact
E001/E002 folder it reports dozens of canonical-count "shortfalls" that are not
faults, so it is the wrong tool for this round. Do not use it to check a compact
folder.

## Manual fallback: seven continuous files

If the GUI cannot be used, record the whole sitting as **seven continuous WAV
files** by hand and let `segment_fallback.py` split the derived copies. Record
each file in one take, leaving **2-3 seconds of clear silence between responses**.
Format: **PCM WAV, mono, preferably 48 kHz (16 kHz floor), 16- or 24-bit** - no
MP3/OGG/FLAC, no processing. A 24-bit source is preserved and its derived takes
stay 24-bit (byte-identical slices; nothing is ever down-converted). Mono is
required - a stereo->mono downmix would be a conversion, so a stereo file is
refused rather than converted.

| File | What to record | Responses |
|------|----------------|----------:|
| `positive_close.wav` | "Hey Youtab" close: normal x2, slow x2, fast x2, quiet x2, loud x2 | 10 |
| `positive_farfield.wav` | "Hey Youtab" x3 from >=5 m or the next room | 3 |
| `positive_noise_A.wav` | "Hey Youtab" x3 with the first assigned noise running (E001 tv / E002 street) | 3 |
| `positive_noise_B.wav` | "Hey Youtab" x3 with the second assigned noise (E001 kitchen / E002 fan) | 3 |
| `near_phrases.wav` | The 34 canonical near-phrases, once each, in order (see `--dry`) | 34 |
| `freespeech.wav` | 2-3 min natural speech, no wake/near phrase | 1 (whole) |
| `background.wav` | 1-2 min of the room with nobody speaking | 1 (whole) |

Put the seven files in `incoming\E001\_continuous` (inside the speaker folder,
next to `originals/`). The compact validator tolerates this `_continuous/`
directory as a preserved raw-source archive and excludes it from the take set and
the manifest, so the derived tree still validates GREEN. Then:

```
.\.venv-recorder\Scripts\python.exe -m scripts.wakeword.recording_assistant.segment_fallback --speaker E001 --dry-run
.\.venv-recorder\Scripts\python.exe -m scripts.wakeword.recording_assistant.segment_fallback --speaker E001 --yes
```

`--dry-run` prints every proposed boundary (index, start, end, duration, peak)
for review and writes nothing. The tool **refuses to write** unless each
segmented file splits into exactly its expected count - so a file without enough
silence is caught, not mis-split. Retune with `--min-silence` / `--silence-dbfs`,
or per file with `--override near_phrases.wav:min_silence=1.5`. The seven
originals are **preserved byte-for-byte** (verified by SHA-256 before and after)
in `_continuous/`, and the 55 derived takes land under `E001/originals/` exactly
where the validator and importer expect them, with a `SHA256SUMS` manifest.

After splitting, validate the package (GREEN via `validate_compact_submission`)
and run the ingestion dry-run. Note that `import_speaker.py` is the **full
E003-E007 round** importer: on a compact E001/E002 package its format, checksum,
role and classification checks pass, but it reports two expected count
"shortfalls" ("enough usable takes" / "matches SPEAKER_RECORDING_PACKAGE.md")
because the compact package carries fewer takes per section by design. The
authoritative GREEN for this round is the compact validator.

## Handoff

When the sitting is done, the coordinator fills the metadata form
(`RECORDING_METADATA.json`), adds the signed `CONSENT.pdf`, and writes the
`SHA256SUMS` manifest, leaving the speaker folder with exactly the four entries
the compact validator expects. A GREEN result means the auto-named folder is
complete and ready to hand over. Nothing is uploaded.
```
