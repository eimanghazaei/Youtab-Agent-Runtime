# Round 8 design — human-only, predeclared before the recordings exist

Not started. Round 8 cannot begin: E003, E004, E005, E006 and E007 have not been
recorded. Nothing in this document has been executed — no feature extraction, no
training, no synthesis, no evaluation, no sealed set opened.

It is written **now**, before the audio exists, for the reason Round 6's and
Round 7's were. Every decision that could be influenced by seeing a result is
fixed here, where it can be checked against. And a sealed set that is opened
after a decision was tuned against it is spent: predeclaration is what keeps
E002, E006 and E007 usable at all.

The machine-readable form of everything below is `round8_config.json`, checked
for completeness and internal consistency by
`tests/tools/test_wakeword_round8_predeclaration.py`. Where the two disagree,
the JSON is the artifact the pipeline reads and this document is the argument;
the test exists so they cannot drift apart.

## The binding decision: human-only

Synthetic training is retired. Rounds 1–7 are historical rejected experiments;
none will ship. Round 8 and every later candidate in this phase is trained,
validated and qualified **exclusively** with real human recordings and real
recorded environmental audio.

Prohibited, without exception: TTS speech; Piper, LibriTTS, LibriTTS-R or VCTK
voices; voice conversion or artificial speaker mixing; synthetic positives, near
phrases or hard negatives; generated background noise; generated or artificial
impulse responses and reverberation; pitch shift; time stretch; speed
augmentation; gain augmentation; artificially mixed speech-noise composites; any
previous synthetic tensor; any synthetic candidate as an initialization.

Allowed processing, and nothing else: decode; fixed resample to 16 kHz; fixed
mono conversion; deterministic trim, pad and window extraction;
production-identical feature extraction; the fixed numeric scaling the runtime
requires. None of these invents a speech condition or acoustically alters the
speaker.

Real *recorded* corpora are real human audio and may serve as **real negatives**,
with licence, hash and source recorded. Neither may ever be a positive speaker,
and neither is allowed to delay recording. They are not interchangeable, though,
and the split between them is load-bearing:

* **Speech Commands v0.02** (105,829 clips of 35 words from 2,618 speakers,
  CC BY 4.0, pinned by sha256 in `assets.py`) is the **training** negative
  source.
* **The governed Common Voice subset** (`COMMON_VOICE.md`) is frozen
  `usage="sealed-evaluation"` and is **qualification only** — `build_human_dataset.py`
  refuses it on `--split train` and `--split validation`. Target 2 bounds
  recorded-speech false activations at 0.2/h, and a negative set the model was
  fitted on cannot bound its own false-activation rate. Using it for training,
  or for hard-negative mining, would destroy the one measurement it exists to
  provide.

## What Round 8 varies — exactly one axis

**`--channels`, downward, over the predeclared set
{(128, 128, 64), (32, 32, 16), (8, 8, 4)}.** Exactly three arms, three training
runs. Nothing else varies between them.

Exactly three is a bound, not a budget. `round8_config.check` refuses any other
arm count and quotes the stop condition below when it does, so a fourth width
cannot be introduced by editing `round8_config.json` before a run — and a
two-arm config is refused just as squarely, because dropping an arm turns a
capacity–accuracy relation into two points and a line drawn through them.

| arm | channels | parameters | ratio to r6c1 | parameters per positive utterance |
|---|---|---|---|---|
| r8a | 128, 128, 64 | 192,961 | 1.000× | 1,313 |
| r8b | 32, 32, 16 | 28,369 | 0.147× | 193 |
| r8c | 8, 8, 4 | 5,941 | 0.031× | 40 |

Parameter counts are computed from the architecture, not guessed:
`conv1 96→c1 k5`, `conv2 c1→c2 k5`, `conv3 c2→c3 k3`, six frames survive the
kernels, `head (c3×6)→64`, `out 64→1`. The formula reproduces r6c1's recorded
192,961 exactly, which is the check that it is right. The same formula is
asserted in the predeclaration test.

### Why capacity, and why downward

Not because capacity is the lever. R5 measured that it is not, and the
correction below makes that finding *stronger*, not weaker. Capacity is varied
because it is **the one hyperparameter the data change leaves undefined**, and
because the direction the data change implies has never been measured.

The training set changes size by two orders of magnitude in the class that
matters. Measured, from the stored tensors:

| | r6c1 (features_r6c1) | Round 8 (projected) | change |
|---|---|---|---|
| train windows | 346,441 | 73,161 | ÷4.7 |
| positive windows | 116,300 | 1,029 | ÷113 |
| distinct positive-class speakers | ~600 synthetic + 1 real | **3 real** | — |
| hard-negative windows | 164,090 | 1,414 | ÷116 |
| optimizer steps at batch 512 × 60 epochs | 40,620 | 8,580 | ÷4.7 |

192,961 parameters over 1,029 positive windows derived from **147 utterances by
three people** is 1,313 parameters per utterance. R5's measured slope is that
6.31× parameters made false rejects worse — at each candidate's own selected
threshold, r4 7.56% against r5c3 48.42%, a factor of 6.4; on a matched
false-accept budget, 11.66% against 15.52%. Every candidate ever trained sits at
192,961 parameters or above. Down is the untested direction, and it is the
direction a 113× smaller positive class points in.

Reducing capacity is also the only lever against the failure mode
`ROUND6_DESIGN.md` flagged and could not then test: with a single-speaker
validation set, a model that learns *these three voices* rather than *this
phrase* looks fine. Fewer parameters is the structural version of that
protection; it does not depend on noticing.

### Stop condition

If no arm meets all five targets on E005 validation, **stop**. Report the
measured capacity–accuracy relation on human-only data and the per-category
diagnosis below. Do not add a fourth width, do not widen the set, do not begin a
context-length, optimizer, threshold or architecture-family experiment without
an explicit Owner decision. This is R5's stop condition, reused for the third
time.

No blind sweeps. Three arms is the whole matrix, and the validator holds the
matrix closed at both ends rather than leaving it to be remembered.

## What the measured evidence actually says, including where it has been overstated

Everything here is recomputed from the stored artifacts rather than quoted.

### R5: capacity up is worse

At each candidate's own validation-selected threshold, on the untouched eval
split, identically on both backends:

| candidate | channels | parameters | false rejects | near-phrase FA | recorded FA/h |
|---|---|---|---|---|---|
| r4 | 128, 128, 64 | 192,961 | 7.56% | 2.90% | 0.515 |
| r5c1 | 192, 192, 96 | 369,249 | 14.76% | 1.68% | 0.086 |
| r5c2 | 256, 256, 128 | 598,785 | 18.80% | 1.32% | 0.086 |
| r5c3 | 384, 384, 192 | 1,217,601 | 48.42% | 0.20% | 0.086 |

6.31× the parameters, 6.4× the false rejects. Holds.

### R6: real human positives help — by 1.19×, not 2.4×

`ROUND7_DESIGN.md` records the matched-false-accept-budget bound as
23.14% → 9.78%, a 2.4× improvement, and treats that as the measured size of the
real-positive effect. **That figure is an artifact of the nine-point threshold
grid the measurements were sampled on.** r4's grid steps from 0.60 (near-phrase
FA 2.02%, which fails the ≤2% budget) straight to 0.70 (false rejects 23.14%),
so the coarse grid forces r4 to 0.70, while r6c1 happened to land at 1.98% at
exactly 0.60 and was allowed to stay there.

Recomputed on a dense grid over every distinct negative peak score in the eval
split (24,773 candidate thresholds for r4, 24,618 for r6c1), with the same
3-frame confirmation rule and the same three false-accept constraints:

| candidate | coarse nine-point bound | dense bound | dense threshold |
|---|---|---|---|
| r4 (no human positives) | 23.14% | **11.66%** | 0.609139 |
| r6c1 (E001 × 4) | 9.78% | **9.82%** | 0.600875 |
| r6c2 (E001 × 16) | 22.58% | 19.24% | 0.605003 |
| r6c3 (E001 × 64) | 16.42% | 11.70% | 0.654000 |
| r7 (second TTS family) | 24.62% | 18.40% | 0.631941 |

The effect is real and it is significant. Paired on the same 5,000 positive
windows at each candidate's own dense oracle threshold, bootstrapped over the
2,500 source-utterance group ids rather than over windows:

```
r4  - r6c1: +1.84 pts [95% cluster-bootstrap CI +1.10, +2.58]
            window-level discordant: r4-only miss 216, r6c1-only miss 124
r6c1 - r7 : -8.58 pts [95% cluster-bootstrap CI -9.54, -7.66]
            window-level discordant: r6c1-only miss 59, r7-only miss 488
```

So: 300 real-human positive windows — 0.26% of a 116,000-window positive pool —
bought **1.84 points**, 11.66% → 9.82%, a factor of 1.19. That is the entire
measured evidence for real positives, and it is a twentieth of the way from
9.82% to the 5% target. Round 8's design must not be built on an extrapolation
from 2.4×; the number is 1.19×.

### R7: a second synthetic voice family made it worse

The brief this design answers records R7 as neutral, bound 9.70% → 10.16%.
Neither figure is reproducible from the stored artifacts. On the nine-point grid
r7's bound is 24.62% against r6c1's 9.78%; on the dense grid 18.40% against
9.82%; paired, r7 misses 8.58 points more positives than r6c1 with a CI that
excludes zero by a wide margin, and 488 positive windows that r6c1 detects are
lost against 59 gained. 26,000 clips from a disjoint TTS corpus did not do
nothing — they did harm. The conclusion the brief draws (a second synthetic
family is not the answer) survives with a larger effect than stated.

### The gap is the speech, not the channel

`tmp/channel_gap_diagnostic.json`, r6c1 at its own selected threshold 0.5561035871505737,
400 paired synthetic clips per transform, 75 real ones, produced elementwise in
one loop so index *i* is the same clip in every arm:

| arm | fire rate | closes of the A→D gap |
|---|---|---|
| A clean synthetic | 92.75% | — |
| resample-only null | 92.75% | 0.0% |
| band-limited to 7 kHz | 92.75% | 0.0% |
| AAC 64 kbit/s round trip | 90.25% | **6.3%** |
| AAC 24 kbit/s round trip | 89.50% | 8.2% |
| real human, same phrase | 53.33% | — |

The synthetic-to-real gap is 39.42 points. Every plausible transmission-channel
explanation together accounts for under a tenth of it. There is nothing to fix
in the codec path; the difference is the speech.

### The confirmation rule is where real speech is lost

Same diagnostic, comparing "any frame's peak crossed the threshold" against
"three consecutive frames crossed it", which is what the product runtime
requires:

| arm | peak ≥ threshold | 3-frame confirmed | cost of the rule |
|---|---|---|---|
| clean synthetic | 98.75% | 92.75% | 6.00 pts |
| resample-only null | 98.75% | 92.75% | 6.00 pts |
| AAC 64k | 98.00% | 90.25% | 7.75 pts |
| raw TTS wavs | 98.75% | 97.25% | 1.50 pts |
| **real human** | **76.00%** | **53.33%** | **22.67 pts** |

Real utterances cross the threshold and fall back off it. Three quarters of them
reach the threshold at some frame; barely half hold it for 240 ms. The plateau,
not the peak, is the binding defect — and the plateau is exactly what the
trailing-context band in `build_dataset.PHRASE_END_JITTER` exists to teach.

### Where the deficit lives, by recording condition

Same 75 real positives, same threshold, by the category the speaker recorded
them in. Confirmed fire (what the runtime does) and peak-only (what the model
alone does):

| category | n | confirmed | peak ≥ threshold | median peak | median RMS |
|---|---|---|---|---|---|
| close mic, normal | 15 | **15/15 (100.0%)** | 15/15 (100.0%) | 0.8597 | −25.8 dBFS |
| varied rate and loudness | 30 | 11/30 (36.7%) | 16/30 (53.3%) | 0.6198 | −33.4 dBFS |
| far field | 15 | 8/15 (53.3%) | 12/15 (80.0%) | 0.6722 | −25.7 dBFS |
| over real background noise | 15 | 6/15 (40.0%) | 14/15 (93.3%) | 0.7657 | −14.8 dBFS |

Close-mic real speech is solved. Everything else is not, and the noise category
is the clearest case of the plateau problem: 14 of 15 reach the threshold and
only 6 hold it.

## The dataset, and the honest consequence of its size

### What Round 8 trains on

Phrase-anchored clips (positives, near phrases) are windowed on a **fixed
deterministic grid of seven offsets**: the phrase end sits 2, 3, 4, 5, 6, 7 or 8
frames before the window end. That is the integer-frame enumeration of the
0.16–0.64 s trailing-context band `build_dataset.py` already documents, at the
runtime's own 80 ms frame rate. It is not a tuning parameter — the band is
existing documented design and the frame rate is the runtime's, so the count is
derived. It is also the *only* legitimate multiplier left: with augmentation
prohibited, seven framings of one utterance is all the pipeline may produce from
it.

Non-phrase-anchored audio (free speech, background recordings, Speech Commands)
is tiled deterministically into non-overlapping 2.00 s windows.

| pool | source | utterances | windows | label | window category |
|---|---|---|---|---|---|
| positives | E001 75, E003 36, E004 36 | 147 | 1,029 | 1 | `positive_human` |
| hard negatives | E001 90, E003 56, E004 56 near phrases | 202 | 1,414 | 0 | `near_phrase_human` |
| free speech | E001 51 clips, E003 and E004 one minute each | — | 111 | 0 | `free_speech_human` |
| recorded negatives | Speech Commands train partition | 70,577 | 70,577 | 0 | `recorded_speech` |
| background only | `exercise_bike.wav`, 61.3 s | — | 30 | 0 | `background_only` |
| **total** | | | **73,161** | | |

Speech Commands' train partition is 70,577 clips = 39.209 h of window time,
measured by running the pipeline's own `speech_commands_split` and
`_speaker_bucket` over the corpus. Nothing is drawn from the validation or eval
partitions.

`synthesized_speech` and `common_speech` — 12,000 windows in every previous
round — are **gone**. Both were TTS.

### Four real background recordings, not six

`_background_noise_` ships six files. Two of them, `pink_noise.wav` and
`white_noise.wav`, are **generated** noise and are prohibited. Measured
durations of what remains: `doing_the_dishes.wav` 95.2 s, `dude_miaowing.wav`
61.8 s, `exercise_bike.wav` 61.3 s, `running_tap.wav` 61.2 s — 279.4 s total
(279.5 s if the rounded figures are added), 0.0776 h, 139 non-overlapping
windows for all three splits combined.

**Real recorded noise only, in every active path.** The rule holds for training,
for validation and for qualification alike; there is no split, no diagnostic and
no "just for the baseline" exception. It is enforced in three places rather than
stated in one: `build_dataset.GENERATED_BACKGROUND_NAMES` drops both files before
any split is cut and counts the drop in `stats.generated_background_excluded`,
`build_human_dataset.GENERATED_BACKGROUND_NAMES` does the same for a
`recorded_background` source, and `round8_config.check` reads `build_dataset.py`'s
noise constants out of its source so a config that documents the rule while the
builder breaks it fails the build. The predeclaration test holds the two builders'
lists in agreement, so there is one definition of "generated" rather than two.

This had a consequence nobody had stated before, and it has now been corrected in
the code. `VALIDATION_NOISE` was `("pink_noise.wav", "doing_the_dishes.wav")`, so
**one of validation's two background recordings was generated**. It is now
`("doing_the_dishes.wav",)`: validation holds one real recording where it held
one real and one synthesised, and train holds `exercise_bike.wav` where it held
that plus `white_noise.wav`. No real seconds were lost — the four recordings and
their 279.4 s were always the whole real pool — and the assignment below is the
one the projection and the validation gate already assumed. Round 8's assignment,
disjoint by recording:

| split | recording | seconds | windows |
|---|---|---|---|
| train | `exercise_bike.wav` | 61.3 | 30 |
| validation | `doing_the_dishes.wav` | 95.2 | 47 |
| sealed | `dude_miaowing.wav`, `running_tap.wav` | 123.0 | 61 |

"Background-only false accepts = 0" is therefore a claim about **one or two
rooms**, and 61 clean windows bound the per-window rate at 4.79%, not at zero.
Said here rather than discovered in the report.

### The class-balance problem, and the one derived rule that answers it

Under the mix above, positives are 1.41% of training windows. r6c1's positives
were 33.57%. If `--negative-weight 3.0` and `--hard-negative-weight 6.0` are
carried over unchanged, the positive class carries

```
1,029 / (1,029 + 6×1,414 + 3×70,718) = 0.46%
```

of the total loss mass, against r6c1's measured 8.953%
(`116,300 / (116,300 + 6×164,090 + 3×66,051)`). A 19× reduction in the gradient
pressure on the only class that has ever been the problem is not "holding a
hyperparameter fixed" — it is changing the experiment by leaving a number alone
whose meaning depends on a mix that no longer exists.

So Round 8 holds the **derived quantity**, not the nominal flag. One rule, fixed
here, computed arithmetically from the frozen manifest counts before training
starts and recorded in `round8_config.json`:

> Choose `w` such that the positive class's share of total loss mass equals
> r6c1's 8.953%, with `--negative-weight = w` and
> `--hard-negative-weight = 2w`, preserving r6c1's exact 6:3 hard-to-ordinary
> ratio.

With the projected counts that is `w = 0.1423`, `2w = 0.2846`. The values are
recomputed from the actual frozen counts at freeze time and pinned before the
first epoch; the *rule* is what is predeclared, and the rule contains no free
choice.

The alternative — cap the recorded negatives instead, restoring the ratio by
discarding audio — was considered and refused. Matching r6c1's window ratio
would mean using about 530 of the 70,577 permitted real recorded negatives.
Throwing away 99% of the only real negative corpus available, in order to
protect a ratio inherited from a synthetic dataset, is the worse error, and it
would also destroy the hours the ≤0.2/h target needs.

### What this means for overfitting, and what is done about it

147 utterances from three people is not a training set that supports a claim
about voices in general, and nothing in this design pretends otherwise.

* **Capacity** is the swept axis for exactly this reason. r8c at 5,941
  parameters is 40 parameters per positive utterance.
* **Epochs stay at 60.** Restoring the previous optimizer-step count would mean
  284 passes over 1,029 positive windows instead of 60. Sixty passes over three
  voices is already the memorisation risk; multiplying it by 4.7 to keep a step
  count constant is the wrong invariant. The step count drops from 40,620 to
  8,580 and that is reported, not corrected.
* **Dropout stays at 0.15 and weight decay at 1e-4.** Capacity reduction *is*
  the regularisation response. Moving dropout as well would make two changes and
  make neither attributable.
* **The one training speaker with a measured per-condition profile is E001**, and
  its profile above says close-mic is solved and the other three conditions are
  not. E003 and E004 must therefore spread device, distance and noise cells
  across the group as `SPEAKER_RECORDING_PACKAGE.md` instructs. A training set of
  three close-mic speakers would train the one condition that already works.

## The exact manifests Round 8 consumes, and their hashes

Every one is frozen by `freeze_manifest.py` before it is read, which computes
`manifest_sha256` over the canonical JSON body and writes both a `SHA256SUMS`
for the data and a `.sha256` for the manifest itself. `usage` is a field, not a
comment: `assert_usable_for` raises when a purpose and a usage disagree.

**Every hash field below is EMPTY, deliberately.** They are filled and frozen
before execution. Inventing one now — even a plausible-looking one — is exactly
the failure this document exists to prevent, and
`round8_config.py --check` refuses a config in which some hashes are filled and
some are not, because a half-frozen config looks frozen and is not.

| dataset | role | usage | positives | near phrases | free speech | `manifest_sha256` |
|---|---|---|---|---|---|---|
| E001 | train | `training` | 75 usable (4 excluded) | 90 | 51 clips | *(empty — pending freeze)* |
| E003 | train | `training` | 36 | 56 | 60 s | *(empty — not recorded)* |
| E004 | train | `training` | 36 | 56 | 60 s | *(empty — not recorded)* |
| E005 | validation | `validation` | 36 | 56 | 60 s | *(empty — not recorded)* |
| E002 | sealed | `sealed-evaluation` | — | — | — | *(empty — pending freeze)* |
| E006 | sealed | `sealed-evaluation` | 36 | 56 | 60 s | *(empty — not recorded)* |
| E007 | sealed | `sealed-evaluation` | 36 | 56 | 60 s | *(empty — not recorded)* |

E002's counts are left blank on purpose. It already exists and its ingestion
report is on disk; nothing about it was read, listed or counted while writing
this design, because a predeclaration that consulted a sealed set is not one.

Non-speaker inputs, same treatment:

| input | pin | state |
|---|---|---|
| Speech Commands v0.02 archive | `af14739ee7dc311471de98f5f9d2c9191b18aedfe957f4a6ff791c709868ff58` | already pinned in `assets.py`, CC BY 4.0 |
| the four real background recordings | per-file sha256 in the corpus `SHA256SUMS` | derived from the archive pin |
| Common Voice 17.0 (en) subset | revision, lock and manifest digests | *(empty — not acquired; blocked on one Owner action)* |
| the r8 feature tensors | per-file sha256 | *(empty — not built)* |
| the selected checkpoint, ONNX and TFLite | sha256 | *(empty — not trained)* |

## Held fixed — everything else

| | |
|---|---|
| Input contract | 16 frames × 96 embedding dims — unchanged |
| Output contract | single sigmoid score — unchanged |
| Architecture family | three `Conv1d` layers, kernels (5, 5, 3), `head→64`, `out→1` — unchanged; only the channel widths move |
| Product runtime | `tools.wake_word._OpenWakeWordEngine`, 1280-sample (80 ms) frames, sensitivity **is** the raw threshold, 3-frame confirmation — unchanged |
| Loss | `BCELoss(reduction="none")` with focal weighting, `--focal-gamma 2.0` — unchanged |
| Optimizer | `AdamW`, lr 1e-3, weight decay 1e-4 — unchanged |
| Schedule | `CosineAnnealingLR(T_max=60)`, 60 epochs, batch 512 — unchanged |
| Hard-negative categories | `near_phrase`, `near_phrase_human` — unchanged |
| Validation margin | 0.5 — unchanged |
| Splits | immutable, below |
| Targets | all five, unchanged |
| Initialization | **fresh**, seed 20260818 |

### Splits — immutable

| role | members | what it may be used for |
|---|---|---|
| train | E001, E003, E004 | fitting only |
| validation | E005 | threshold selection, candidate selection, epoch selection — nothing else |
| sealed | E002, E006, E007 | one measurement, after freeze |

E005 is never fitted on. E002, E006 and E007 are never fitted on, never used for
threshold selection, never used for hard-negative mining, never used for
candidate or epoch selection. `freeze_manifest.assert_usable_for` raises on any
attempt, and `build_dataset.load_human_clips` refuses any manifest that does not
declare itself `train` and any split but `train`.

### Initialization is fresh

`torch.manual_seed(20260818)`; a new `WakeWordNet`; `numpy.random.default_rng(20260818)`
for the batch order. **No checkpoint from r1–r7 is loaded, as an initialization
or otherwise, and no synthetic tensor is read.** `train_model.py` resumes from
`<out>/checkpoint.pt` if one exists, so each arm's output directory must be
empty at launch; the arm's epoch-0 checkpoint sha256 is recorded so "it started
from nothing" is checkable afterwards rather than asserted.

The seed is new, not 20260807. Reusing the synthetic era's seed on a dataset
whose draw order is entirely different buys nothing and invites the false
impression that the runs are comparable draw-for-draw. They are not.

## What has to be built before Round 8 can run

Predeclared as prerequisites, not as hyperparameters. Each is a code change, and
none of them may be decided during execution.

1. **A no-augmentation build path.** `build_dataset.Augmenter.apply` is
   unconditional and applies gain scaling, a synthesized room impulse response
   at `REVERB_FRACTION 0.5`, and additive background at a drawn SNR in
   `SNR_RANGE (0, 25) dB`. All three are prohibited. `build_rir_pool`
   synthesizes impulse responses with pyroomacoustics — prohibited. Round 8
   needs the augmenter bypassed entirely, not configured quietly to identity.
2. **`POSITIVE_SPEECH_PREFIX_FRACTION` set to 0.** Splicing an unrelated Speech
   Commands word in front of the wake phrase manufactures an utterance that
   never happened, from two rooms and two speakers. Prohibited. The cost is
   real and is stated in advance: the model will only ever see the phrase
   preceded by the speaker's own room tone, so "wake word spoken mid-sentence"
   is a predicted weakness of every Round 8 arm.
3. **The deterministic seven-offset window grid**, replacing the random draw
   from `PHRASE_END_JITTER`, and deterministic non-overlapping tiling for
   non-anchored audio.
4. **Generated background files excluded** from every split by name, and
   `VALIDATION_NOISE` corrected. **Done.** `build_dataset.GENERATED_BACKGROUND_NAMES`
   removes both before a split is cut and reports them in
   `stats.generated_background_excluded`; `VALIDATION_NOISE` is
   `("doing_the_dishes.wav",)`; `round8_config.check` reads all three constants
   out of the builder, so reverting it fails the predeclaration.
5. **Utterance-level selection and reporting.** With seven windows per
   utterance, a window-level rate over 392 near-phrase windows is not 392
   independent trials — it is 56 utterances measured seven ways. Every
   false-reject and near-phrase figure used for selection or acceptance is
   aggregated on the source-utterance group id that `build_dataset` already
   assigns: a positive utterance is a miss if **none** of its seven windows
   fires; a near-phrase utterance is a false accept if **any** of its seven
   windows fires. Window-level numbers may be reported beside them, never
   instead of them.
6. **A synthetic-input refusal guard** in the Round 8 path: any TTS directory,
   any previous feature tensor, any r1–r7 checkpoint is a fatal error rather
   than a warning.

## Acceptance — unchanged

5/5 on **both** backends independently, with zero detection-parity
disagreements:

| # | target |
|---|---|
| 1 | false rejects ≤ 5% |
| 2 | recorded-human-speech false activations ≤ 0.2/hour |
| 3 | deliberate near-miss false accepts ≤ 2% |
| 4 | background-only false accepts = 0 |
| 5 | ONNX/TFLite numerical and detection parity, with latency and memory measured |

### Both artifacts from one frozen candidate

`hey_youtab.onnx` and `hey_youtab.tflite` are exported from the **same** frozen
checkpoint by `train_model.export_onnx` and `export_tflite`, with normalization
folded into the first convolution identically for both. Each is then evaluated
**independently** through the real product runtime —
`tools.wake_word._OpenWakeWordEngine.process`, 1280-sample frames, 3-frame
confirmation, the same validation-selected threshold at full float64 precision —
over the same windows. **Zero detection-parity disagreements is a hard
requirement, not a tolerance.** Frame-score deltas are reported (r7 measured max
8.225e-06, mean 6.46e-08 over 31,986 windows); a single detection disagreement
fails the candidate outright, whatever the score delta.

## Selection rules

### Threshold selection — validation only

Unchanged from every previous round: `train_model.threshold_meeting_targets`
takes the **lowest** threshold at which every false-accept target, tightened by
`--validation-margin 0.5`, is met on E005 validation. Lowest because raising the
threshold only ever costs missed wake words. Candidates are the observed negative
scores themselves, so the search is exact rather than a grid. The selected
threshold is recorded at full float64 precision and never re-tuned afterwards.

### Epoch selection — validation only

Per epoch: the lowest threshold meeting the tightened targets, and the
utterance-level false-reject rate it buys. An epoch where no threshold satisfies
the targets cannot be selected. Lowest false-reject rate wins.

### Candidate selection — validation only, then a tie-break that prefers smaller

The arm with the lowest utterance-level false-reject rate on E005 at its own
validation-selected threshold. If two arms are within **1.0 percentage point**
— less than the 1.84-point effect that 300 real windows bought, so inside the
noise this pipeline has ever resolved — the arm with **fewer parameters** wins.

The tie-break direction is fixed here, before any number exists, because it is
the one place a preference could otherwise be invented after the fact. It
prefers smaller because a smaller model over 147 utterances from three speakers
is less likely to have learned the speakers, and because that failure mode is
invisible on a single-speaker validation set.

### 5/5 on E005 before any sealed dataset is opened

**Hard gate.** No sealed manifest is read, hashed against, or listed until one
arm has met all five targets on E005 validation, on both backends independently,
with zero detection-parity disagreements. If no arm passes, the sealed sets stay
sealed and Round 8 reports a failure. This is not a recommendation; it is the
only thing that keeps E002, E006 and E007 worth having.

Each target's E005 form is exact, and each is reported next to the bound the
sample size can actually support:

| # | E005 form | passes if | demonstrability bound from E005 alone |
|---|---|---|---|
| 1 | utterance-level false rejects over 36 positive utterances | ≤ 5%, i.e. ≤ 1 miss | 0 misses bounds the rate at 7.98%, not 5% |
| 2 | false activations per hour over the Speech Commands validation partition, 7.926 h | ≤ 0.2/h, i.e. 0 fires (1 fire = 0.126/h passes, and is reported as a fire) | 0 fires bounds the rate at 0.378/h — **cannot demonstrate 0.2/h** |
| 3 | utterance-level near-phrase false accepts over 56 near utterances | ≤ 2%, i.e. ≤ 1 | 0 accepts bounds the rate at 5.21%, not 2% |
| 4 | background-only false accepts over 47 windows of one recording | 0 | 0 bounds the per-window rate at 6.18%, not 0 |
| 5 | detection parity over every validation window | 0 disagreements | 0 over 14,987 windows bounds the rate at 0.0200% |

Targets 2, 3 and 4 are therefore **passed as "not contradicted"** on E005, never
reported as demonstrated. The margin (`--validation-margin 0.5`) is what makes
the pass mean something rather than nothing, and R6 measured that even 0.5 was
not enough: r6c1's selected epoch showed 1.000% near-phrase FA and 0.000/h
recorded on validation and delivered 2.725% and 0.429/h on evaluation. That
finding is recorded and **not acted on**, for the same reason `ROUND7_DESIGN.md`
gave: the only evidence for a better margin comes from comparing validation
against evaluation, and setting a hyperparameter from that comparison is tuning
on the evaluation set.

## Statistical power, and what cannot be measured

Derived from the rule of three (zero events in *n* trials bounds the rate at
about 3/*n* with 95% confidence) and from exact Clopper-Pearson bounds. The unit
of evidence is the **utterance**, never the window: seven deterministic framings
of one utterance are one trial, not seven.

### Target 1, false rejects ≤ 5%

| positive utterances | clean-run 95% upper bound |
|---|---|
| 36 (E005 alone) | 7.98% |
| 60 | 4.87% |
| 72 (E006 + E007 at 36 each) | 4.08% |
| 144 | 2.06% |

Sixty positive utterances is the minimum for a clean run to demonstrate ≤5% at
all. E006 and E007 together supply 72, so the sealed measurement **can** make the
claim — but only on a clean run. One miss in 72 is 1.39% observed with a
Clopper-Pearson upper bound of 6.42%, which fails to demonstrate the target while
looking like a pass.

For a *usable interval* around 5% rather than a one-sided bound:

| Wilson half-width wanted | positive utterances needed |
|---|---|
| ±5.0 pts | 73 |
| ±2.5 pts | 292 |
| ±2.0 pts | 456 |
| ±1.0 pts | 1,825 |

At the 144 positives the four-speaker programme produces in total, an observed
4.86% carries a 95% interval of [2.37%, 9.69%]. **The recording programme can
demonstrate "≤5% on a clean run" and cannot distinguish 5% from 9.7%.** That is
the honest ceiling on target 1, and it is a shortfall of roughly 2× against
±2.5 pts and 12× against ±1 pt.

There is a second, larger limitation that sample size does not fix: 4 to 6
speakers is 4 to 6 draws from the population of human voices. Clustered on
speaker, the effective *n* for a claim about people is 2 sealed speakers plus
E002. No arithmetic on utterance counts changes that, and no Round 8 result
should be phrased as a rate for users in general.

### Target 2, recorded-human-speech false activations ≤ 0.2/h — not demonstrable

Poisson: `P(0 events | rate r over t hours) ≤ 0.05` iff `r·t ≥ 2.9957`. At
r = 0.2/h that is **t ≥ 14.98 h**.

| audio | hours | clean-run bound |
|---|---|---|
| consented free conversation, 1 min × 3 sealed speakers | 0.050 | 59.9/h |
| E001 free-speech clips (train side) | 0.023 | 129.3/h |
| Speech Commands validation partition | 7.926 | 0.378/h |
| Speech Commands eval partition | 11.659 | 0.257/h |
| Speech Commands train partition | 39.209 | 0.076/h |
| governed Common Voice subset as planned | 20.000 | 0.150/h |

Three things follow, and the third is a correction to the record.

1. **The consented recordings cannot measure this target, by three orders of
   magnitude.** One minute per speaker bounds the rate at 59.9/h. Reporting
   "zero false activations on the sealed speakers" as evidence for ≤0.2/h would
   be meaningless, and `ROUND6_DESIGN.md` already said so about E002's two
   minutes.
2. **The only corpus that can demonstrate it is the 20 h governed Common Voice
   subset**, which is blocked on one Owner action: accepting the dataset terms
   while signed in and exporting a read-scope token. Until that exists, target 2
   is **undemonstrable at any model quality**, and the shortfall is
   14.98 − 0.050 = **14.93 h** of consented conversational speech, or the whole
   20 h Common Voice fetch.
3. **This target has never been demonstrated in any previous round either.** The
   eval split's recorded-speech partition is 11.659 h, so a *perfect* clean run
   there bounds the rate at 0.257/h — above the 0.2/h target. Every "0.000/h" in
   `evidence/frontier.txt` is one observed event away from 0.086/h and was never
   a demonstration of the criterion. The Speech Commands train partition is the
   only pool with the hours, and it is the pool the model is fitted on, so it
   cannot measure anything.

Round 8 therefore reports target 2 as **not demonstrable** unless the Common
Voice subset has been acquired, frozen and hashed first, and says which of the
two it is in the qualification record.

### Target 3, near-miss false accepts ≤ 2%

| near-phrase utterances | clean-run 95% upper bound |
|---|---|
| 56 (E005 alone, or one sealed speaker) | 5.21% |
| 112 (E006 + E007) | 2.64% |
| 150 | 1.98% |
| 168 (three sealed speakers) | 1.77% |

150 near-phrase utterances is the minimum. E006 and E007 together give 112, so
the sealed side is **38 utterances short** and a clean run bounds the rate only
at 2.64%. E002's historical near set would close the gap numerically, but it does
not contain `okay youtab`, `hey google` or bare `hey`, and its `hey you tab`
takes were spoken with a pause inside the name — so it cannot carry the claim for
the phrases that matter. The remedy is a third sealed speaker recording the take
list, or more takes of rows 6–13 from E006 and E007; either is an Owner decision
about the recording programme, not something Round 8 can fix.

`SPEAKER_RECORDING_PACKAGE.md` states "40 near × 4 speakers = 160". The take
list actually sums to **56 near-phrase utterances plus one conversation block per
speaker**, so the document understates its own package. The arithmetic above uses
the take list.

### Target 4, background-only = 0

61 sealed windows from two real recordings bound the per-window rate at 4.79%.
0.0776 h of real recorded background exists in total, split three ways. The
target is met or not met as a count; it is not a demonstration of a rate, and no
Round 8 report may present it as one.

### Target 5, parity

Zero disagreements over *N* compared windows bounds the disagreement rate at
3/*N*: 0.0094% at r7's 31,986 windows, 0.0998% at 3,000, 0.2991% at 1,000. Round 8
must therefore compare **every** window of every category on both backends,
including the full recorded-negative pool, because the real negative corpora are
the only thing keeping *N* large once the synthetic positives are gone.

### The honest summary of power

Two of the five targets — 2 and 4 — cannot be demonstrated by anything the
recording programme will produce. Target 3 is 38 utterances short. Target 1 can
be demonstrated on a clean run and cannot be estimated to better than about
±5 points. Round 8 is worth running because it is the first measurement of a
human-only model and it produces the capacity–accuracy relation on real data,
**not** because a 5/5 result would prove the product is ready.

## The sealed-set opening rule

Sealed sets are opened once, in one operation, after everything is frozen and
hashed. Order is not negotiable.

1. One arm passes 5/5 on E005 validation, on both backends, zero parity
   disagreements. If none does, stop; nothing below happens.
2. Freeze and hash, all of it, before a sealed manifest is read:
   * the selected checkpoint
   * `hey_youtab.onnx` and `hey_youtab.tflite`, exported from that one checkpoint
   * the validation-selected threshold, full float64 precision
   * every training manifest and its `manifest_sha256`
   * the E005 validation manifest
   * each sealed manifest
   * the Speech Commands asset sha256, and the Common Voice lock and manifest if
     acquired
   * the runtime version and the repository commit SHA
   * `round8_config.json` and its sha256
3. Measure **once**, through the real ONNX and TFLite product runtimes,
   independently, at that frozen threshold, on E002, E006 and E007.
4. Report. The threshold is not re-tuned on a sealed set. Difficult samples are
   not excluded. Nothing is retried. Each sealed speaker is reported separately
   as well as pooled, because a pooled rate hides the one voice the model hates.
5. A sealed set that has been measured is **spent**. It may not be measured again
   for a different candidate, and a later candidate needs a sealed speaker that
   has never been opened.

## If validation fails

The likely outcome, stated in advance so it cannot be dressed up afterwards:
r6c1 — trained on 116,000 synthetic positives *and* E001's own 300 real windows
— confirms only 53.33% of E001's positives, a speaker it was fitted on. An arm
trained on 147 utterances from three speakers and measured on an unseen fourth is
unlikely to reach 5% false rejects, and targets 2 and 4 are undemonstrable
regardless.

When an arm fails, the procedure is fixed:

1. **Diagnose by real recording category**, using E005's own manifest categories
   and the per-condition profile above: close mic, varied rate and loudness, far
   field, real background noise. Report confirmed-fire and peak-only rates for
   each, so a plateau failure is distinguishable from a discrimination failure.
   That distinction is the whole content of the 22.67-point confirmation cost,
   and it decides what a correction should even target.
2. **Then exactly one evidence-backed structural correction.** One. It must name
   the measurement that motivates it, and it must be predeclared in a Round 9
   design document before it is executed, exactly as this one was.
3. **Returning to synthetic data is forbidden.** Not discouraged — forbidden. If
   the diagnosis says the training distribution is too narrow, the answer is more
   consenting speakers, not TTS. R7 is the measured precedent: 26,000 clips from
   a second, genuinely disjoint TTS corpus made false rejects 8.58 points worse.
4. Not permitted as the "correction": re-tuning the threshold on any sealed set;
   excluding difficult recordings; widening the capacity set; running a fourth
   arm; re-sweeping a hyperparameter this document holds fixed; or reporting a
   window-level rate in place of an utterance-level one.

## Cost

Estimates, and stated as estimates. Feature extraction over 73,161 train windows
plus validation is a fraction of the 398,441-window synthetic build. Each arm is
8,580 optimizer steps against r7's 40,620, and r8b and r8c are 6.8× and 32.5×
smaller than r8a. Three arms plus two-backend evaluation should be well under a
day on the same host. r7 measured ONNX p50 2.04 ms and TFLite p50 1.57 ms per
80 ms frame at real-time factors 0.062 and 0.046; both smaller arms can only be
faster, so there is no latency risk in this matrix — which is itself a reason the
capacity axis is cheap to measure honestly.
