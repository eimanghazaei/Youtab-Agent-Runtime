# Two-speaker Round 8 development flow

Only two human speakers are available, so Round 8 development runs on them:

- **E001 — training only.** `SPEAKER_SPLITS["E001"] == "train"`.
- **E002 — validation only.** Reassigned from `eval_sealed` and recorded
  `CONSUMED_FOR_VALIDATION` — the sealed final holdout is now E006/E007 only,
  and E002 can never be re-sealed (`assert_consumed_speakers_not_sealed`).
- **E006 / E007 — sealed final qualification, pending.** Not recorded yet.
  Final *production* qualification stays blocked on new independent human voices
  and does **not** block the development below.

This is development and validation, not production sign-off. Nothing here is
executed until the recordings exist; this file is the sequence and the gate each
step already passes through.

## 0. Record and ingest
1. Each human records the compact package with the recording assistant
   (`scripts/wakeword/recording_assistant/`), which writes WAV mono ≥16 kHz to
   the `incoming/E001` and `incoming/E002` folders and produces `SHA256SUMS`.
2. The assistant's compact validator must report **GREEN** for each folder.
3. Ingest with `import_speaker.py` (dry-run first). It derives the split/usage
   from the registry — E001→train, E002→validation — refuses a sealed speaker,
   preserves the originals byte-for-byte, and freezes a manifest.

A dry-run against a self-test compact E002 folder confirms the binding:
`import_speaker` reports *"E002 is validation (split validation), from
round8_config.SPLITS['validation']"* and passes the metadata, `SHA256SUMS`,
exclusion-rule, not-already-imported and no-host-path checks. (The self-test
writes one placeholder tone to every take, so the byte-identical-reuse guard
fires on it; real recordings differ and will not.)

**One prerequisite before ingesting the real recordings:** E001 and E002 have a
split in the registry (`round8_config.SPLITS` / `build_human_dataset.SPEAKER_SPLITS`),
but they are **not** in `speaker_recording_spec.SPEAKER_ASSIGNMENTS`, which is the
current five-speaker recording round (E003–E007) and is what records, per speaker,
whether a folder may be trained on. `import_speaker` notes this and asks that the
speaker be assigned before ingesting. Add E001/E002 (or a compact-round assignment
table) at ingestion time so the training-eligibility record exists; the split
binding itself is already correct.

## 1. Train on E001 only
`build_human_dataset.py --split train` accepts E001 (and only train speakers);
`train_model.py` fits the capacity arms. E002 is **refused** on the train path
(`validation ≠ training`).

## 2. Validate and select the threshold on E002 only
`build_human_dataset.py --split validation` now accepts E002 (this is what the
reassignment unlocked). `train_model.threshold_meeting_targets` picks the epoch
and the lowest threshold that meets every false-accept target — including the
near-phrase target (V1 fix: the near-phrase mask is built from
`HARD_NEGATIVE_CATEGORIES`, so `near_phrase_human` is not silently skipped, and
an empty near-phrase set refuses certification rather than passing vacuously).
E001 (train) is never used to pick the threshold; E006/E007 (sealed) are never
touched here.

## 3. Freeze the candidate and the threshold
Before any sealed data could be opened, the candidate and its threshold are
frozen (`round8_controller` States 6/10; the freeze is what the sealed barrier
checks). No sealed set is opened in the two-speaker flow — there is no recorded
sealed set yet, and the controller refuses to open one that is not frozen-behind.

## 4. Evaluate both backends independently through the product runtime
`qualify.py` measures **ONNX and TFLite independently** through the real
`_OpenWakeWordEngine`, and records the framework that actually ran (V3 fix), so
a backend that silently falls back cannot be reported as the one requested.

## 5. Require 5/5 on both backends with zero disagreements
Qualification passes only when **both** artifacts independently meet all five
targets on E002 at the frozen threshold, with **zero** per-window detection
disagreements between ONNX and TFLite (`DetectionDisagreementRefused`,
`BackendMissingRefused`). One backend passing is not a pass.

## 6. If validation fails
Request a **small, evidence-based, targeted top-up** — a few more takes of the
specific failing condition (e.g. the confusable near phrases that drove the
false accepts, or the low-SNR far-field positives that drove the false
rejects) — **not** another full recording package. The top-up is ingested the
same way and folded into E001 (training) or E002 (validation) by its own label;
it never crosses the train/validation boundary.

## 7. Do not fabricate a sealed final holdout
The two-speaker flow **never** opens or invents a sealed qualification set.
E006/E007 stay unrecorded and sealed. A development candidate that passes step 5
is a *validated* candidate, not a *production-qualified* one.

## 8. Production qualification remains pending
Final production qualification requires new **independent** human voices
(E006/E007, recorded later) opened **once** through the sealed path. Until then,
the shipped `hey_youtab.onnx` / `hey_youtab.tflite` remain unchanged, and any
report says plainly: **validated on E002; production qualification pending new
independent speakers.**

> No synthetic or TTS-generated voice may enter training, validation or
> qualification at any step. The recording assistant's TTS is guidance to the
> human only and is never recorded.
