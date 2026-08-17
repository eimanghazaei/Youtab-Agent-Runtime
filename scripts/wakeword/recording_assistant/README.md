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

Python 3.12+, then the two operator libraries (`numpy` is already present in the
project environment):

```
pip install -r scripts/wakeword/recording_assistant/requirements-recording-assistant.txt
```

- `sounddevice` - microphone capture and playback.
- `pyttsx3` - offline Windows SAPI5 voice guidance (optional; if it is not
  installed the on-screen phrase is still shown).

## Run

```
py -m scripts.wakeword.recording_assistant.app --speaker E001
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

## Handoff

When the sitting is done, the coordinator fills the metadata form
(`RECORDING_METADATA.json`), adds the signed `CONSENT.pdf`, and writes the
`SHA256SUMS` manifest, leaving the speaker folder with exactly the four entries
the validator accepts. Then:

```
py scripts/wakeword/validate_speaker_submission.py <incoming-root>/E001
```

The recording assistant produces the naming and layout that command accepts.
```
