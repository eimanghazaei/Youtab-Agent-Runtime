# Common Voice as recorded negatives — the bounded plan

Nothing here has been downloaded. This document and
`acquire_common_voice.py` exist so that the moment the one credential arrives,
acquisition is a single bounded command rather than a design exercise — and so
that the bound, the licence and the disk cost are agreed before anything is
fetched rather than after.

## Why this corpus, and why negatives only

The shipped model's measured false-activation rate on recorded human speech is
0.07% of windows — but every one of those windows is a **single word** from
Speech Commands. The model card says so in its own limitations: *"the negatives
are read commands and short sentences … sustained conversation, music and
television are not represented, so the per-hour figure is a floor rather than a
promise about a noisy living room."*

Acceptance asks for ≤ 0.2 false activations per hour of recorded speech. That
number cannot be honestly measured against one-second clips of the word
"eight". Common Voice is read *sentences*, from tens of thousands of speakers,
across accents, recorded on whatever microphone the contributor had — which is
exactly the distribution an always-on microphone actually hears.

It is used as negatives only. No Common Voice clip contains "hey youtab", so
every clip is a true negative by construction, the same argument that makes
Speech Commands safe to use unlabelled. It contributes **nothing** to the
positive class: the positives problem is a different problem, and it is what
the consented speaker recordings (`SPEAKER_RECORDING_PACKAGE.md`) are for.

## How much, and why that much

**20 hours of validated English clips.**

That is derived, not chosen. With zero observed activations in *n* hours, the
95% upper bound on the rate is about 3/*n* — the rule of three. To demonstrate
a ≤ 0.2/h budget you therefore need at least 15 hours before a clean run says
anything, and 20 hours puts the demonstrable bound at 0.15/h, comfortably
inside the target. Anything less cannot prove the criterion no matter how well
the model behaves; much more spends bandwidth and disk on a tighter bound than
acceptance asks for.

At the budget rate, 20 hours should produce about 4 activations. Zero is a
pass with margin; a handful is the number to look at closely.

## Expected size on disk

Estimates, not measurements — nothing has been fetched. Each line states the
assumption it rests on, and `acquire_common_voice.py` records the *actual*
figures in its manifest.

| stage | arithmetic | size |
|---|---|---|
| downloaded clips | MP3, mono, ~32 kbit/s → ~14.4 MB per hour | **~290 MB** |
| decoded to the pipeline's format | 16 kHz mono 16-bit → 115.2 MB per hour | **~2.3 GB** |
| 2.00 s windows | 20 h ÷ 2 s = 36,000 windows | — |
| openWakeWord features | 36,000 × 16 × 96 × 4 B | **~221 MB** |
| peak, with staging | clips + decoded + features, before cleanup | **~3 GB** |

The full English corpus is very much larger — tens of gigabytes — and is not
downloaded. No figure for it is asserted here, because asserting one would mean
quoting a number nobody in this repository has checked.

## Licence: CC0-1.0

Common Voice's audio and transcripts are released under
[CC0-1.0](https://creativecommons.org/publicdomain/zero/1.0/), a public-domain
dedication.

**What it permits.** Use, modification, redistribution and commercial use, with
no attribution requirement and no share-alike obligation. Training a model on
it and shipping that model is unambiguously allowed. Attribution is given
anyway, in `SOURCES` in `assets.py` and in the model card, because a provenance
record that omits an input is worse than useless even when the licence does not
demand one.

**What it does not do.**

* It does not waive the **terms of access** the downloader accepts to reach the
  gated distribution. Those terms are a separate agreement and include not
  attempting to identify speakers. CC0 covers the data; it does not license
  behaviour.
* It is not permission to **redistribute the corpus** from this project. There
  is no reason to: the acquisition script fetches from the source and the
  clips never enter this repository.
* It does not change the rule that **no audio is committed**. Public-domain
  audio is still audio, and
  `tests/tools/test_wakeword_no_human_data_committed.py` rejects it on the same
  patterns as everything else. The clips live in the work directory beside the
  rest of the pipeline's intermediates.
* CC0 waives copyright. It does not, and cannot, resolve every jurisdiction's
  personality and voice-likeness rights. For a negative set that is never
  reproduced and never synthesized from, this is not a live concern — it would
  be a different question for voice cloning.

## The version

`mozilla-foundation/common_voice_17_0`, English (`en`), on the Hugging Face
Hub — the version this plan is written against.

The script does **not** accept a branch name. A revision must be a full commit
SHA, because "main" is not a version and a dataset that is re-uploaded under
the same name would silently change what "measured on Common Voice" means.
The resolved revision goes into the manifest.

The corpus's internal file layout is deliberately not asserted here. Nothing
has fetched it, so any claim about which tar shards exist would be a guess
dressed as a specification. The script takes the file paths as input — one
click from the dataset's file browser, or one `huggingface-cli` listing — and
pins each of them by SHA-256.

## What the script produces

```
<work>/common_voice/
    clips/…                            the fetched files, exactly as published
    common_voice_en.lock.json          path -> sha256, established once, enforced after
    common_voice_en.manifest.json      frozen by freeze_manifest.py
    common_voice_en.manifest.SHA256SUMS  coreutils format, for `sha256sum -c`
```

The manifest carries `usage="training"` and `split="train"`, so
`freeze_manifest.assert_usable_for` will refuse if anyone later tries to
measure on it. A negative set the model was fitted against is not a
measurement of anything.

Hashes cannot be pinned in advance — no bytes have been fetched, so there is
nothing to pin. The first fetch therefore runs with `--establish-lock`, which
records what it received and prints every digest for review; every run after
that verifies against the lock and refuses on any mismatch. This is
trust-on-first-use, it is weaker than the pins in `assets.py`, and it is
explicit rather than implied precisely because it is weaker.

## The Owner action

Exactly two things, both of which require being a person with an account:

1. Open <https://huggingface.co/datasets/mozilla-foundation/common_voice_17_0>
   while signed in and accept the dataset's terms. Access is gated; a token
   alone does not open it.
2. Create a Hugging Face access token with **read** scope only, and put it in
   the environment of the machine that will run the fetch:
   `HF_TOKEN=…` (or `HUGGING_FACE_HUB_TOKEN=…`).

Nothing else is blocked. No token is requested by this repository, nothing here
stores one, and the script reads it from the environment, sends it only as an
`Authorization` header to `huggingface.co`, and never writes it to a file, a
log line or an error message.

Then, on the machine with the work directory:

```bash
# What it would do, with no credential and no network:
python scripts/wakeword/acquire_common_voice.py --plan

# The first fetch, establishing the lock:
python scripts/wakeword/acquire_common_voice.py \
    --out ../wakeword-build/common_voice \
    --revision <full commit sha> \
    --file-list cv17-en-shards.txt \
    --max-bytes 400000000 \
    --establish-lock

# Every later run, enforcing it:
python scripts/wakeword/acquire_common_voice.py \
    --out ../wakeword-build/common_voice \
    --revision <same sha> --file-list cv17-en-shards.txt --max-bytes 400000000
```
