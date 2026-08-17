# Training the "hey youtab" wake-word model

This directory builds `tools/wakewords/hey_youtab.onnx` and
`hey_youtab.tflite` — the on-device hotword model that ships with Youtab and
makes `wake_word.enabled: true` work with no setup.

Run it with:

```bash
scripts/wakeword/run_pipeline.sh [work-dir] [python]
```

It needs no GPU. The reference run took about three hours on four CPU cores
and 15 GB of RAM, and about 20 GB of scratch disk.

## Why a model has to be trained at all

An openWakeWord model detects exactly the phrase it was trained on. There is
no configuration, alias or text substitution that makes a model trained for one
phrase answer to another — the phrase is in the weights. So a wake word cannot
be renamed, only retrained, and that is what this pipeline does.

## What the model is

openWakeWord splits detection in two. A shared front end — a melspectrogram
model followed by Google's speech-embedding model — turns audio into a stream
of 96-dimensional frames, one every 80 ms. A small per-phrase classifier reads
the trailing sixteen of those frames and emits one probability. Only the
classifier is phrase-specific, and only the classifier is trained here; the
front end is a pinned, unmodified upstream artifact shared with every other
openWakeWord model.

That makes the trained artifact small and auditable. This table describes what
this pipeline **builds**; what is currently **shipped** in `tools/wakewords/`
is described by its own `MODEL_CARD.md`, which is generated from the run that
produced those exact bytes. The two differ while a retrain is in flight, and
the card is the one that describes the file a user loads.

| | |
|---|---|
| Input | `(1, 16, 96)` float32 — 16 frames covering ~1.9 s of audio |
| Output | `(1, 1)` float32 — probability the phrase just finished |
| Layers | 3 temporal convolutions (128, 128, 64 channels; kernels 5, 5, 3), then 64-unit head, sigmoid |
| Parameters | ~193,000 |

The first version flattened all sixteen frames into one 1,536-wide vector and
learned a dense map from it — 951,297 parameters, and it overfitted: training
loss 0.037 against 8.7% held-out near-miss confusion. Convolving along the
frame axis instead gives the same evidence a fifth of the parameters and the
right inductive bias, because a five-frame kernel sees a 400 ms span wherever
it lands rather than learning each position separately. That is exactly what
the measured failures turned on: `hey you tap` against `hey youtab` is one
voicing feature inside one 400 ms span.

Input standardization is folded into the first layer at export, so both
exported files are plain matrix multiplies with no backend-specific ops.

## Stages

| Stage | Script | What it does |
|---|---|---|
| 0 | `assets.py` | Fetches every external input and verifies it against a pinned SHA-256. |
| 1 | `generate_speech.py` | Synthesizes the wake phrase and the near-miss phrases across hundreds of voices. |
| 2 | `build_dataset.py` | Builds 2.00-second windows, augments them, and runs the real openWakeWord front end over them. |
| 3 | `train_model.py` | Fits the classifier and exports ONNX and tflite from one set of weights. |
| 4 | `evaluate_model.py` | Streams held-out audio through `tools.wake_word._OpenWakeWordEngine` and measures false accepts and false rejects. |
| 5 | `make_test_fixture.py` | Cuts the committed regression fixture out of the evaluation set. |
| 6 | `write_model_card.py` | Generates `tools/wakewords/MODEL_CARD.md` and `SHA256SUMS` from the run's own outputs. |
| — | `verify_on_device.py` | Checks the capture path on a real microphone. See `DEVICE_VERIFICATION.md`; not part of the build. |
| — | `verify_backends.py` | Runs both shipped artifacts on the current OS: parity, latency and memory. No hardware, no network. |
| — | `freeze_manifest.py` | Freezes a recorded dataset — hashes every file, hashes the list, records split and usage. Not part of the build. |
| — | `acquire_common_voice.py` | Bounded Common Voice negative acquisition. Blocked on an Owner credential; see `COMMON_VOICE.md`. |
| 2r8 | `build_human_dataset.py` | Round 8's stage 2. Builds the same windows and the same features from **real recordings only**, and refuses everything else. Replaces `build_dataset.py` for this phase; see below. |

`phrases.py` holds the phrase inventory shared by stages 1 and 4.

## Round 8: real recordings only

Rounds 1-7 were fitted on synthesized speech and none of them qualified.
Synthetic training is retired for this phase: Round 8 and every later candidate
is trained, validated and qualified exclusively on real human recordings and
real recorded environmental audio.

`build_human_dataset.py` is that stage, and it is a separate module rather than
a flag on `build_dataset.py` on purpose. `build_dataset.py` requires a TTS tree,
synthesizes impulse responses, mixes noise at a drawn SNR and re-levels every
window; a gated mode inside it would leave every prohibited operation one flag
away. The new module has no code path that can do any of those, so the
prohibition is structural rather than conditional. What it *does* reuse is
`read_wav16`, `sha256_file`, the window geometry and `FeatureSink`, so the
feature space is byte-for-byte the one the runtime uses, and the output file
names are unchanged so `train_model.py` consumes it as-is.

What it accepts, and only this:

| source | `--source` type | role |
|---|---|---|
| an approved human speaker's frozen manifest | `human=` | positives, human near phrases, human free speech |
| a frozen Speech Commands manifest | `speech_commands=` | recorded negatives |
| a frozen governed Common Voice subset | `common_voice=` | recorded negatives — **qualification only**, see below |
| a frozen manifest of real recorded room tone | `recorded_background=` | background-only windows |

**The Common Voice subset is sealed-evaluation, not training data.** It is frozen
with `usage="sealed-evaluation"`, so `build_human_dataset.py` refuses it on
`--split train` and `--split validation` and accepts it only for qualification.
That is not caution, it is the only thing that makes it useful: acceptance target
2 bounds recorded-speech false activations at 0.2/h, a negative set the model was
fitted on cannot bound its own false-activation rate, and bounding that rate is
the entire reason this corpus is acquired. Training negatives come from Speech
Commands, which is 105,835 real human recordings and is permitted for training.

Processing is limited to decode, the derivation's fixed resample and mono fold,
deterministic trimming/padding/window extraction, the production feature
extractor, and the fixed int16 scaling. There is no augmentation: no reverb, no
mixed noise, no gain, no pitch or speed change, and no random draw anywhere —
two builds of one input are bit-identical. Positives are windowed at a fixed
ladder of trailing offsets (160-640 ms), which is the same trailing-context
coverage `build_dataset.py` gets from jitter without inventing a condition.

The refusals are the point, and each is a test in
`tests/tools/test_wakeword_round8_human_only.py`:

* speaker split assignment comes from an explicit registry, never from a
  filename, and a manifest whose declared split disagrees with it is refused in
  either direction;
* a sealed speaker is refused in training and in validation on any combination
  of flags, and the sealed *evaluation* speaker is not buildable into a tensor
  at all — it is measured by streaming its audio through the runtime engine;
* every original is hash-verified against the manifest, at the recording itself
  where the capture drive is mounted and at the full-length decode where it is
  not, and the report says which;
* every clip carries provenance — a `source_file` corroborated by the
  manifest's own `files` list — so a synthetic clip renamed `positive_human`
  is still refused, because the name was never what was checked;
* the synthetic-era category names with no real-derivation meaning
  (`positive`, `hardneg`, `confusable`, `softneg`, `common`,
  `synthesized_speech`) are hard errors wherever they appear. `near_phrase` and
  `free_speech` are *not* in that list: those are what the deriver calls a real
  speaker's negatives, so refusing them by name made the frozen sealed manifest
  un-ingestible — and that manifest cannot be regenerated, because its digest is
  what a qualification result is traced to. They are normalised to the `_human`
  names instead, and what refuses a synthetic clip wearing one is the provenance
  and content check, never the string;
* nothing under `data/features*` or `data/tts` can be read, extended or written
  to, and a checkpoint may only be initialised from if the
  `DATASET_CONTRACT.json` this stage writes sits beside it asserting zero
  synthetic samples;
* every reference has to be *direct*: a clip, a full-length decode or a corpus
  file that is named absolutely, climbs out of the manifest's own directory with
  `..`, or arrives through a symlink is refused, because the bytes it reaches
  are not the bytes that were frozen;
* every clip, corroborating original, decode, corpus file and initialization
  checkpoint is looked up by SHA-256 in `retired_synthetic_artifacts.json`, so a
  byte-identical copy of retired synthetic material is refused wherever it sits
  and whatever it is called — a missing or unreadable registry is a refusal, not
  a pass. Those tests are
  `tests/tools/test_wakeword_synthetic_relocation_guard.py`. What the registry
  covers and what it does not is stated inside the registry: the retired TTS
  corpora are 207,300 clips and are represented by a documented sample, so for
  clips the provenance chain above is still the primary guard;
* every retired or synthetic-data flag aborts with a reason rather than being
  ignored;
* `pink_noise.wav` and `white_noise.wav` are *generated*, not recorded, so they
  are excluded from the background pool by name and the exclusion is counted in
  the stats;
* `synthetic_samples: 0` is written into the stats and the contract, computed
  from what was actually emitted — the build refuses rather than reporting a
  zero it did not measure.

A Round 8 human manifest carries a `MANIFEST.json.sha256` sidecar and declares
either the `_human` dataset names (`positive_human`, `near_phrase_human`,
`free_speech_human`) or the deriver's own names (`near_phrase`, `free_speech`,
`positive_<condition>`), which are normalised to them. Accepting both is what
lets an already-frozen speaker be read without rewriting it; the guard is
provenance plus content addressing, not the category string.

Every reference must also be *direct*: relative to the manifest, free of `..`,
and free of symlinks. A relocated synthetic clip with a perfectly self-consistent
manifest — correct digests, an innocent `source_file` corroborated in `files` —
used to build cleanly, and `retired_synthetic_artifacts.json` is what refuses it
now: the bytes are recognised wherever they sit and whatever they are called.

## Datasets that are recorded rather than downloaded

Everything the pipeline downloads is pinned by SHA-256 and fails loudly on a
mismatch. Anything *recorded* — a consented speaker session, a corpus subset
assembled locally — gets the same treatment from `freeze_manifest.py`, which
hashes every file, hashes the list, and refuses to overwrite a manifest that no
longer matches. It also records the split and the usage, and
`assert_usable_for()` raises rather than letting a sealed evaluation set be
consumed for training: that set is worth exactly its unseenness, and
re-recording the same speaker does not restore it.

Manifests are written beside the dataset, never inside this repository — the
tool refuses a destination under the checkout, because a list of filenames from
a recording session is part of what stays on local disk.

## Data provenance

Everything below is redistributable and pinned by hash in `assets.py`.

| Input | Source | Licence | Role |
|---|---|---|---|
| `melspectrogram.onnx` / `.tflite`, `embedding_model.onnx` / `.tflite` | openWakeWord v0.5.1 release assets | Apache-2.0 | Shared front end; used at training and at inference |
| `silero_vad.onnx` | Silero VAD, redistributed in the same release | MIT | Nothing here uses VAD; openWakeWord fetches it while loading a model, so it is pinned rather than left to the network |
| `en_US-libritts_r-medium.pt` | piper-sample-generator v2.0.0 release asset | MIT (code) over LibriTTS-R (CC BY 4.0) | Multi-speaker TTS; 904 speaker embeddings |
| `speech_commands_v0.02.tar.gz` | Google Speech Commands v0.02 | CC BY 4.0 | Recorded human negatives (2,618 speakers) and background noise |
| Room impulse responses | Generated by `pyroomacoustics` | MIT (tool); the responses are synthetic | Reverberation |
| Phoneme strings | `eSpeak NG`, via piper-sample-generator | GPL-3.0-or-later (tool only) | Accent realisations; no espeak-ng code or data is redistributed, and program output is not covered by the GPL |

No private recording, scraped audio or unlicensed corpus is used anywhere in
the pipeline.

`SOURCES` in `assets.py` is the machine-checked version of this table: every
project and corpus, its version or release, its licence, its attribution, and —
for the ones that were considered and refused — why. **MUSAN in particular is
not used**: the noise this pipeline mixes in is Speech Commands' six background
recordings plus synthetic impulse responses, and MUSAN is recorded as
`declined` so its absence reads as a decision rather than an omission.
`tests/tools/test_wakeword_asset_licences.py` fails if a pinned file names a
source that is not recorded, or if a pipeline script starts referencing a
corpus this table does not account for.

### Positives

Synthesized, not recorded. The generator mixes pairs of the 904 LibriTTS-R
speaker embeddings by spherical interpolation, so the voice pool is much larger
than the speaker count, and it varies speaking rate (0.7–1.3×) and two VITS
noise parameters per batch.

Three spellings are used, because espeak-ng renders them differently and both
renderings are things people say:

* `hey youtab` → `hˈeɪ jˈuːɾæb` (flapped /t/, the usual American realisation)
* `hey yoo tab` → `hˈeɪ jˈuː tˈæb` (hard /t/)
* `hey you tab` → `hˈeɪ juː tˈæb` (unstressed carrier)

**Accents.** For a phoneme-driven synthesizer, accent lives in the phoneme
string rather than the acoustic model, so the same LibriTTS-R generator covers
twelve English varieties by phonemizing through their espeak-ng voices. All
twelve were checked against the generator's own `phoneme_id_map`, so none of
them silently degrades to dropped symbols:

| voice | "hey youtab" |
|---|---|
| `en-us` | `hˈeɪ jˈuːɾæb` — flapped /t/ |
| `en-gb-x-rp` | `hˈeɪ jˈuːtæb` — hard /t/ |
| `en-gb-scotland` | `hˈeː jˈʉːtab` — fronted /u/, monophthong /e/ |
| `en-029` | `hˈeɪ jˈuːtab` — Caribbean |
| `en-gb-x-gbcwmd` | `ˈeː jˈəutab` — West Midlands, h-dropping |
| `en-au` / `en-nz` / `en-za` / `en-in` / `en-us-nyc` | `hˈeɪ jˈuːɾɛəb` — æ-tensing |

### Negatives

Three kinds, because they fail differently:

* **Near misses** — `hey youtube`, `hey you tap`, `hey your tab`, `youtab` on
  its own, `hey` on its own, and thirty others, synthesized from the same
  voices as the positives and placed at the same position in the window. A
  detector that separates the wake word from unrelated speech but not from
  these is the one that wakes up while its owner is talking about something
  else. Nothing in the near-miss list *contains* the wake phrase.

  Thirteen of them are synthesized again, at a higher rate, as
  `CONFUSABLE_NEGATIVES`. That list is measured rather than guessed: the first
  trained model's held-out false accepts were broken down by phrase, and the
  ranking was not what the inventory was designed around. `hey youtube` — the
  obvious confusion, and the reason the list exists — fired on 1 clip in 106.
  What actually fired was minimal pairs on the final syllable (`hey you tap`,
  one voicing feature away, at 88%) and the frame `hey <something> tab`. The
  extra clips go where the errors are.
* **Common phrases** — the short conversational fragments an always-on
  microphone hears all day, including the ones that begin with "hey" and the
  ones that mention tabs, since those are the two things the wake phrase is
  made of: `hey, how are you`, `hey guys`, `open a new tab`, `check the
  network tab`, `just a second`.
* **Recorded human speech** — every Speech Commands utterance, sometimes two
  concatenated. Real microphones, real rooms, 2,618 speakers.
* **Background only** — room tone with no speech at all.

### Splits

Disjoint by source, not by shuffling:

| | Train | Validate | Evaluate |
|---|---|---|---|
| Synthesized voices | speakers `[0, 600)` | `[600, 700)` | `[700, 904)` |
| Recorded speech | 1,779 speakers | 333 speakers | 506 speakers (the published `validation_list` + `testing_list`) |
| Background noise | `exercise_bike`, `white_noise` | `doing_the_dishes`, `pink_noise` | `running_tap`, `dude_miaowing` |
| Room impulse responses | seeded pool A | seeded pool B | seeded pool C |

Three pools, not two. The middle one exists because **both** the epoch and the
operating threshold are chosen, and choosing them on a random split of the
training windows is choosing them on voices the model has already heard. The
recorded-speech pools are partitioned on a hash of the speaker id, the same
convention Speech Commands uses for its own lists; the three sets were checked
to share zero speakers pairwise.

Evaluation is measured once, at the end, and is never used to choose anything.
No voice, room or noise recording is shared between fitting, selecting and
measuring.

### Augmentation

Applied identically to both classes — if only positives were reverberated the
model could score well by learning "reverberant" instead of "hey youtab".

* Half of all windows are convolved with a synthetic room impulse response
  (rooms from 2.5 × 2.0 × 2.2 m to 9 × 7 × 3.5 m, absorption 0.15–0.75).
* Background noise is mixed in at 0–25 dB SNR. 0 dB means the phrase is as loud
  as the room.
* Level is randomized between −24 and −3 dBFS peak.
* 30% of positive windows are preceded by an unrelated spoken word, so the
  model sees the phrase after speech and not only after room tone.

### Window placement

openWakeWord rescores every 80 ms on the trailing sixteen frames, so the score
is supposed to peak at the moment the phrase finishes. Positive windows put the
end of the phrase 160–640 ms before the window edge. Near-miss windows are
placed the same way — a negative that only ever appeared mid-window could be
rejected on position rather than on what was said.

The lower bound is not zero, and that is the whole point of the range. The
engine fires only after **three consecutive** frames over threshold. A phrase
that finishes exactly at the window edge scores high on one frame and one
frame only, because the next 80 ms pushes it out of the trailing sixteen — so a
model trained that way peaks beautifully and never wakes. Two to eight frames
of trailing context teaches it to hold the score up across the span the
confirmation rule needs, and `evaluate_model.py` reports the plateau it
actually achieves rather than assuming one.

### Choosing the epoch, and choosing the threshold

Both are chosen on the **validation** split, against the shipping targets
themselves rather than a weighted sum of them:

| target | |
|---|---|
| missed wake words | ≤ 5% |
| activation on recorded human speech | ≤ 0.2 per hour |
| activation on deliberate near misses | ≤ 2% |
| activation on background alone | 0 |

For each epoch the search finds the **lowest** threshold at which every
false-accept target holds — lowest because raising the threshold only ever
costs missed wake words, so among the thresholds that qualify the smallest is
the cheapest. The epoch's score is the false-reject rate that threshold buys,
and an epoch where no threshold qualifies cannot be selected at all. The chosen
threshold is then folded into the exported bias, so the runtime's fixed
`sensitivity` of 0.6 *is* that operating point.

Two earlier versions of this are worth recording, because both failed quietly
rather than loudly:

* Ranking epochs at a fixed 0.6 compares each at whatever point on its own
  curve the loss happened to land it, which is not a comparison. The version
  that did this had a false-reject constraint no epoch met, so it silently
  degenerated into the weighted sum it was meant to replace and selected an
  epoch at 9.5% false rejects.
* Calibrating to a false-reject *target* — rather than to the false-accept
  budget — moved the operating point onto a part of the curve where false
  accepts on ordinary recorded speech tripled, 0.105% to 0.272%, to buy two
  points of false-reject rate. The budget has to be on the errors that happen
  while nobody is talking to the agent.

## How the model is measured

`evaluate_model.py` does not compute a validation accuracy. It constructs
`tools.wake_word._OpenWakeWordEngine` — the class the CLI, TUI and desktop app
construct — and feeds it 1280-sample frames through the same streaming feature
buffer, the same raw threshold, and the same three-consecutive-frames
confirmation rule the product uses. The headline row of the report is the
engine's own fire decision. The rest of the threshold sweep is derived from
per-frame scores and cross-checked against the engine at the default
threshold; a single disagreement fails the run.

Both backends are measured. openWakeWord's ONNX and tflite front ends are not
bit-identical, and macOS ARM64 users run the tflite pair
(dscripka/openWakeWord#336), so both get a measured number.

Results live in `tools/wakewords/MODEL_CARD.md`.

## Reproducing

Seeds are fixed in `run_pipeline.sh`, the phrase inventory is in `phrases.py`,
and every external byte is pinned in `assets.py`. A rerun that fetches
different bytes fails at stage 0 rather than quietly training a different
model.

Two things are not bit-reproducible across machines: BLAS reduction order in
the TTS and in feature extraction, and cuDNN/oneDNN kernel selection. Expect
the artifacts to be numerically very close but not hash-identical to the
committed ones. The committed hashes identify *these* files, which is what
`tests/tools/test_wake_word_model_assets.py` verifies; they are not a claim
that training is bitwise deterministic.

## Real-speaker recordings

Round 8 retires synthetic positives and near misses in favour of real human
recordings. [`SPEAKER_RECORDING_PACKAGE.md`](SPEAKER_RECORDING_PACKAGE.md) is
the instructions a speaker follows; [`CONSENT_RECORD_TEMPLATE.md`](CONSENT_RECORD_TEMPLATE.md)
is the consent form each one signs. `speaker_recording_spec.py` is the
machine-readable version of that package — the same phrase list, folder
names, take counts and metadata fields — and `validate_speaker_submission.py`
checks a submitted folder against it before it is handed over. Metadata is
filled in from `RECORDING_METADATA.template.json`.

`build_human_dataset.py` is what consumes a validated submission. It is a
separate stage from `build_dataset.py` on purpose: the synthetic builder
*requires* a TTS root, synthesises impulse responses, mixes background at a
drawn SNR and re-levels every window, so a flag that switched it to
"human-only" would leave every prohibited operation one default away. In the
human-only stage the capability is absent rather than gated — it never imports
the augmenter and has no code path that can reach one.

`generate_speech.py` and `build_dataset.py` remain in the tree as the record of
rounds 1–7, which are retired and did not qualify. Nothing in the active path
loads them.

## Environment

The pipeline runs in its own virtual environment, outside the repository and
outside the agent's own interpreter, so nothing it installs can affect the
product's dependency set:

```bash
python -m venv wakeword-build/venv
wakeword-build/venv/bin/python -m pip install \
    numpy scipy soundfile tqdm onnx onnxruntime pyroomacoustics \
    torch tensorflow-cpu openwakeword piper-tts==1.3.0 torchaudio webrtcvad
git clone --depth 1 https://github.com/rhasspy/piper-sample-generator.git \
    wakeword-build/piper-sample-generator
```

`openwakeword` pulls a `tflite-runtime` wheel built against NumPy 1.x, which
cannot construct an interpreter under NumPy 2. Uninstall it and install
`ai-edge-litert` instead; `tools/wake_word.ensure_tflite_runtime()` bridges
`ai_edge_litert.interpreter` to the module name openWakeWord imports, which is
the same path macOS ARM64 users take.
