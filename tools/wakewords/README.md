# Bundled wake-word models

`hey_youtab.onnx` / `hey_youtab.tflite` — the on-device "Hey Youtab" hotword
model. This is the default detector for the wake-word feature (see
`website/docs/user-guide/features/wake-word.md`); no training or setup is
required to say "hey youtab".

- **Engine:** [openWakeWord](https://github.com/dscripka/openWakeWord) (Apache-2.0).
- **Trained here.** The pipeline that produced these files is
  `scripts/wakeword/`, and it runs end to end on CPU from pinned, licensed
  inputs. `MODEL_CARD.md` records the datasets, the training configuration, the
  measured false-accept and false-reject rates, and the licences.
- **Integrity:** `SHA256SUMS` names the exact bytes every number in the model
  card was measured against, and `tests/tools/test_wake_word_model_assets.py`
  fails if either file changes without the measurements being redone.
- **Label:** the model registers as `hey_youtab` (matches the filename).
- **Two files, one model.** ONNX is loaded everywhere except macOS ARM64, where
  openWakeWord's ONNX embedding model returns near-zero scores
  (dscripka/openWakeWord#336) and the runtime switches to tflite. Both are
  exported from the same weights and are checked against each other on real
  audio; they agree to better than 1e-4.
- **Runtime:** openWakeWord's shared feature-extraction models (melspectrogram +
  embedding) are NOT bundled here — they are fetched once on first use by
  `tools/wake_word.py` via `openwakeword.utils.download_models()`.

To use a different phrase, train your own model and point
`wake_word.openwakeword.model` at its path, or set a built-in openWakeWord name
(`hey_jarvis`, `alexa`, `hey_mycroft`, …). `scripts/wakeword/README.md` is a
working recipe for the former: change the phrase inventory in `phrases.py` and
rerun the pipeline.
