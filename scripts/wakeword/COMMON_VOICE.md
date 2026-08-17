# Common Voice as measured negatives — the bounded plan

Nothing here has been downloaded. This document and
`acquire_common_voice.py` exist so that the moment the one credential arrives,
acquisition is a single bounded command rather than a design exercise — and so
that the bound, the licence, the disk cost, the selection rule and the usage
marking are agreed before anything is fetched rather than after.

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

It is used as negatives only. No Common Voice clip whose published transcript
contains the wake phrase enters the subset, so every clip in it is a true
negative as far as the corpus's own text says — see [What the exclusion does
*not* claim](#residual-risk-what-the-exclusion-does-not-claim). It contributes
**nothing** to the positive class: the positives problem is a different problem,
and it is what the consented speaker recordings
(`SPEAKER_RECORDING_PACKAGE.md`) are for.

## How much, and why that much

**The floor is 14.98 hours. The target is 20 hours.**

Both are derived, not chosen.

A clean run is evidence only in proportion to how long it ran. With zero
observed activations across *n* hours, the 95% upper bound on the rate is
`2.9957 / n` — the exact Poisson form of the "rule of three", and the constant
`round8_config.json` predeclares for target 2. So:

```
hours needed  =  2.9957 / 0.2 per hour  =  14.98 h
```

Below 14.98 h a perfect run demonstrates nothing about the 0.2/h criterion, no
matter how well the model behaves. `acquire_common_voice.py` refuses a
`--target-hours` below that figure, and refuses to freeze a manifest over a
subset that landed short of it.

**Why 20 h and not 14.98 h.** Clips are excluded *after* download by rules that
run over the corpus's own metadata, so the acquired hours are only known at the
end; a run aiming at exactly 14.98 h that lands at 14.9 h would have to be
extended anyway. 20 h is the figure predeclared in `round8_config.json`, it
leaves a 5.02 h margin, and it puts the demonstrable bound at
`2.9957 / 20 = 0.1498/h` — inside the 0.2/h target rather than exactly on it.
More hours than that would buy a tighter bound than acceptance asks for, at
proportional bandwidth and disk.

For contrast, from the same arithmetic — this is why nothing else will do:

| source of recorded negatives | hours | clean-run bound |
|---|---|---|
| consented free conversation, 1 min × 3 speakers | 0.05 | 59.9/h |
| Speech Commands validation partition | 7.93 | 0.378/h |
| Speech Commands eval partition | 11.66 | 0.257/h |
| **this subset** | **20.0** | **0.1498/h** |

At the budget rate, 20 hours should produce about 4 activations. Zero is a
pass with margin; a handful is the number to look at closely.

## Measurement only: the subset can never be trained on

The manifest is frozen with `usage="sealed-evaluation"` and `split="evaluate"`.

That is the opposite of a formality. A negative set the model was *fitted* on
cannot bound its false-activation rate — the number it produces is a
training-set number wearing an acceptance criterion's name. Since making target
2 demonstrable is the entire reason this corpus exists, the marking that
protects that use is enforced in three places:

* `freeze_manifest.assert_usable_for` refuses `sealed-evaluation` for
  `training` and for `validation`. Hard-negative mining is a training use and
  lands on the same refusal; there is no fourth purpose for it to hide in.
* `build_human_dataset.py` calls that function before it reads a clip, so
  `common_voice=…` on `--split train` or `--split validation` is refused by the
  stage that would otherwise consume it. `--split qualification` maps to the
  `evaluation` purpose and is the one path that accepts it.
* `acquire_common_voice.assert_never_trainable` re-checks both refusals against
  the manifest the acquisition just wrote — a post-condition of acquiring, not
  a sentence in a document. If the marking is ever changed to something
  trainable, the acquisition itself fails.

## What is selected, and how the seed is frozen

The subset is a **seeded** selection, and the seed
(`acquire_common_voice.SELECTION_SEED`) is a constant in the module — fixed in
a reviewable diff, and written into `common_voice_en.selection.json` *before a
single candidate is scored*. A seed chosen after looking at results is not a
seed, it is a filter.

Order of operations, which is the whole point:

1. The selection record is written: seed, revision, target hours, the
   eligibility rules and the digest of the file listing.
2. The corpus's transcript TSV is fetched and parsed.
3. Shards are taken in seeded order; inside each shard, eligible clips are
   taken in seeded order.
4. The pass stops at the target hours — so *which shards are downloaded* is
   determined by the seed and the bound, not by the order somebody pasted a
   listing.

Afterwards the record is immutable in all but one direction: the listing may be
**extended** (appending shards is the documented remedy for a subset that
landed short), and every extension is recorded with its own timestamp and
digest. A reordered or shortened listing is refused, because it would silently
describe a different selection than the one that was frozen.

## Which metadata the exclusion reads

Only fields the corpus itself publishes in its own TSVs. Nothing is inferred,
no audio is compared across corpora, no voice embedding or diarization is
computed — the terms of access forbid attempting to identify speakers, and the
script has no code path that could.

| field | required | what it is used for |
|---|---|---|
| `client_id` | yes | exact-string exclusion of contributor pseudonyms passed to `--exclude-client-ids`, so a held-out slice of the corpus can be kept out of this one |
| `path` | yes | the clip filename, which is what joins a tar member to its metadata row |
| `sentence` | yes | excluding any clip whose published transcript contains a spelling of the wake phrase, taken from `phrases.POSITIVE_SPELLINGS` |
| `sentence_id` | no | recorded per clip in the resume record, so a selection can be traced back to a published sentence |
| `locale` | no | must be `en` where the column exists |
| `up_votes`, `down_votes` | no | where both are published, a clip whose down-votes match or exceed its up-votes is dropped. This is weaker than the corpus's own validation rule and deliberately does not re-derive it |

**Near-miss text is deliberately kept.** "hey youtube", "hey your tab" and the
rest of `phrases.HARD_NEGATIVES` are the hardest and most valuable negatives in
the corpus. Filtering them out would measure a false-activation rate against
speech selected for being easy, which is not the number acceptance is asking
for. Only the wake phrase itself is excluded.

### Residual risk: what the exclusion does *not* claim

Stated here in full, because an exclusion rule that travels without its
residual risk reads as a guarantee. The same list is in the script's
`IDENTITY_NOT_CLAIMED` and in every report it prints.

* `client_id` is the corpus's own **per-release pseudonym, not an identity**.
  The same person re-registering receives a different one, so even within
  Common Voice the exclusion is best-effort rather than complete.
* **No disjointness from any other corpus is claimed.** Not from Speech
  Commands, not from the consented speaker recordings, not from another Common
  Voice release. Nothing published by either side could establish it, and
  establishing it by comparing voices is precisely what the terms of access
  forbid. Any statement in a model card must say "no speaker-level overlap
  check was possible", not "the corpora are disjoint".
* The wake-phrase exclusion reads the **published transcript**. A contributor
  who misread the prompt or ad-libbed is not visible in the text, so "no clip
  contains the wake phrase" is a claim about the `sentence` field and not about
  the audio. The consequence is bounded and in the safe direction for a
  negative set: an unlabelled positive would count as a false activation, so it
  can only make the measured rate look worse.
* `age`, `gender` and `accents` are self-reported, optional, and **not read**.
  No coverage or representativeness claim is made about the subset.

## The decode: once, and frozen

Common Voice ships MP3. The pipeline reads 16 kHz mono 16-bit PCM only —
`build_dataset.read_wav16` rejects anything else — so exactly one decode stands
between them, and it happens once:

* `ffmpeg` is required, checked by `--preflight`, and recorded **by version** in
  the resume record. A tree half-decoded by two different decoders is a dataset
  whose digests describe two computations, so a resume with a different
  `ffmpeg` is refused.
* The flags are the contract: `-map 0:a:0 -map_metadata -1 -fflags +bitexact
  -ac 1 -ar 16000 -c:a pcm_s16le -f wav`. No tags, no encoder string, one audio
  stream, one format.
* Every decoded file is verified twice — its header must state exactly 16 kHz
  mono 16-bit, and the file must actually hold the audio its header declares. A
  truncated decode reports its full length and reads as *silence*, and hours of
  silence counted as speech would inflate the measured exposure.
* The decoded tree is frozen by `freeze_manifest.py`. A clip already decoded is
  never decoded again: a rerun re-hashes it instead.

## Resumable, and never half-complete

An interrupted acquisition is a normal event over 20 hours of audio, so it is
designed for rather than handled.

* `common_voice_en.state.json` is written **before the first byte** with
  `complete: false`, and only ever set to `true` after the manifest is frozen.
  Both "no state file" and "a state file claiming completion" are ways a
  partial acquisition reads as a whole one.
* Every JSON artifact is written to a temporary file and renamed, so no reader
  ever sees a half-written record.
* Downloads are staged through `.partial` and renamed, so an interrupted fetch
  cannot leave a truncated file that hashes into the lock.
* A resume re-hashes what is already on disk rather than trusting the
  filenames, and skips anything that verifies: no verified byte is downloaded
  twice and no verified clip is decoded twice.
* A manifest found beside an unfinished state file is a refusal, not a
  judgement call about which of the two to believe.
* Anything in the decoded tree that this selection would not have chosen is a
  refusal naming the file. The manifest is frozen over the tree, so the tree
  has to *be* the selection.

## Bounded, structurally

Three limits, and none of them is a default a caller can raise away:

| limit | value at 20 h | what it stops |
|---|---|---|
| `--max-bytes` ceiling | 576 MB | a budget raised until the acquisition is the full archive |
| `--target-hours` ceiling (`MAX_TARGET_HOURS`) | 40 h | inflating the byte ceiling by asking for more hours |
| per-stream remaining budget | whatever is left | one listed file that is itself the whole archive |

The ceiling is `target_hours × 14.4 MB/h × 2` — the hours asked for, at the
corpus's stated MP3 bitrate, doubled because a shard carries clips this subset
excludes. It is arithmetic, not a preference. The per-stream limit is checked
against `Content-Length` before a body is read and against the running total
while it is read, so a stream that crosses the budget is abandoned
mid-download and its partial file deleted. The full English corpus is very much
larger — tens of gigabytes — and there is no path through this script that
fetches it.

## Expected size on disk

Estimates, not measurements — nothing has been fetched. Each line states the
assumption it rests on, and `acquire_common_voice.py` records the *actual*
figures in its manifest and its report.

| stage | arithmetic | size |
|---|---|---|
| downloaded clips | MP3, mono, ~32 kbit/s → 14.4 MB per hour | **~288 MB** |
| download ceiling | 20 h × 14.4 MB/h × 2 | **576 MB** |
| decoded to the pipeline's format | 16 kHz mono 16-bit → 115.2 MB per hour | **~2.3 GB** |
| staging and extraction | one shard, extracted | **~1 GB** |
| 2.00 s windows | 20 h ÷ 2 s = 36,000 windows | — |
| openWakeWord features | 36,000 × 16 × 96 × 4 B | **~221 MB** |
| peak, before cleanup | what `--preflight` requires free | **~3.95 GB** |

No figure for the full corpus is asserted here, because asserting one would
mean quoting a number nobody in this repository has checked.

## Licence: CC0-1.0

Common Voice's audio and transcripts are released under
[CC0-1.0](https://creativecommons.org/publicdomain/zero/1.0/), a public-domain
dedication.

**What it permits.** Use, modification, redistribution and commercial use, with
no attribution requirement and no share-alike obligation. Training a model on
it and shipping that model is unambiguously allowed — though this subset is not
used for training, for the statistical reason above. Attribution is given
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
  rest of the pipeline's intermediates, the script refuses an `--out` inside
  the checkout, and it refuses to run at all when a CI marker is set — a CI
  workspace is archived as an artifact and its stdout is a public log.
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
The resolved revision, the resolved URL of every file, its size, its SHA-256
and its licence all go into the lock, and the revision goes into the manifest's
dataset name.

The corpus's internal file layout is deliberately not asserted here. Nothing
has fetched it, so any claim about which tar shards exist would be a guess
dressed as a specification. The script takes the file paths as input — one
click from the dataset's file browser, or one `huggingface-cli` listing —
requires the listing to name the transcript TSV as well as the audio, and pins
each of them by SHA-256.

Hashes cannot be pinned in advance — no bytes have been fetched, so there is
nothing to pin. The first fetch therefore runs with `--establish-lock`, which
records what it received; every run after that verifies against the lock and
refuses on any mismatch. This is trust-on-first-use, it is weaker than the pins
in `assets.py`, and it is explicit rather than implied precisely because it is
weaker.

## What the script produces

```
<work>/common_voice/
    clips/…                              the fetched files, exactly as published
    decoded/…                            16 kHz mono 16-bit PCM, decoded once
    common_voice_en.lock.json            path -> url, bytes, sha256, licence
    common_voice_en.selection.json       the frozen seed and the eligibility rules
    common_voice_en.state.json           the resume record; complete=false until it is
    common_voice_en.manifest.json        frozen; usage=sealed-evaluation, split=evaluate
    common_voice_en.manifest.SHA256SUMS  coreutils format, for `sha256sum -c`
```

None of it is inside the repository, and none of it contains the credential.

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
log line, an error message or a subprocess's argument list — `argv` is
world-readable on Linux, which is why there is no `--token` flag to offer. The
decoder is even run with the token stripped out of its environment.

Then, on the machine with the work directory:

```bash
# What it would do, with no credential and no network:
python scripts/wakeword/acquire_common_voice.py --plan

# What is missing, with no credential and no network. Checks the token, ffmpeg,
# the listing, the destination and the free space, and starts nothing:
python scripts/wakeword/acquire_common_voice.py --preflight \
    --out ../wakeword-build/common_voice \
    --revision <full commit sha> \
    --file-list cv17-en.txt

# The first fetch, establishing the lock (the listing must name the
# transcript TSV as well as the audio shards):
python scripts/wakeword/acquire_common_voice.py \
    --out ../wakeword-build/common_voice \
    --revision <full commit sha> \
    --file-list cv17-en.txt \
    --establish-lock

# Every later run, enforcing it. Interrupted runs resume from here:
python scripts/wakeword/acquire_common_voice.py \
    --out ../wakeword-build/common_voice \
    --revision <same sha> --file-list cv17-en.txt
```

`--max-bytes` defaults to the derived ceiling and cannot be raised past it.
`--exclude-client-ids FILE` takes corpus-published `client_id` values to hold
out. `--target-hours` defaults to 20 and is refused below 14.98.
