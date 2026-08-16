#!/usr/bin/env bash
#
# Build tools/wakewords/hey_youtab.onnx and .tflite from nothing but pinned,
# licensed inputs.
#
# Every stage is seeded and every external byte is hash-checked, so a rerun on
# a different machine produces the same artifacts. The stages are separate
# scripts rather than one program because they have very different costs --
# synthesis is minutes, feature extraction is an hour -- and a rerun should be
# able to resume at the stage that changed.
#
# No GPU is required or used. The whole run fits on four CPU cores.
#
# Usage:
#   scripts/wakeword/run_pipeline.sh [work-dir] [python]
#
# The work directory holds tens of gigabytes of intermediate audio and is not
# part of the repository. Only the two model files, their hashes and the
# measurement reports are committed.
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="${1:-$repo_root/../wakeword-build}"
python_bin="${2:-$work/venv/bin/python}"
here="$repo_root/scripts/wakeword"

downloads="$work/downloads"
tts="$work/data/tts"
features="$work/data/features"
models="$work/models"
evidence="$work/evidence"
generator="$work/piper-sample-generator"

mkdir -p "$downloads" "$tts" "$features" "$models" "$evidence" || exit 1

step() { printf '\n===== %s =====\n' "$1"; }
die() { printf 'FAILED: %s\n' "$1" >&2; exit 1; }

# Two prerequisites come from the Environment block in README.md rather than
# from this script: the build virtualenv, and a clone of
# piper-sample-generator. Both are checked here, before step 0 opens a socket.
#
# The ordering is the point. `install_generator_config` runs *after* step 0's
# fetch loop, so without this guard a missing clone is discovered only once
# several hundred megabytes of pinned assets have already been downloaded --
# and a missing interpreter surfaces as a bare "No such file or directory" from
# the heredoc, naming neither the cause nor the fix.
#
# The clone is deliberately not created here. generate_speech.py puts it on
# sys.path, which makes it executed code, and `git clone` of a moving branch is
# not pinned -- every other external byte this pipeline touches is verified by
# SHA-256. Keeping it an explicit setup step is what stops this script from
# silently running whatever upstream published today. The config file copied
# out of the clone *is* hash-checked, which is what makes copying from an
# unpinned clone safe.
[ -x "$python_bin" ] || die "no build interpreter at $python_bin -- create it \
with the Environment block in scripts/wakeword/README.md, or pass one as the \
second argument"
[ -d "$generator/models" ] || die "piper-sample-generator not found at \
$generator -- clone it with the Environment block in scripts/wakeword/README.md"

step "0. fetch, verify and install pinned inputs"
# `$here` is passed in rather than derived: this runs from stdin, where
# `__file__` does not exist.
#
# Fetching is not sufficient on its own, and for a long time this step only
# fetched. Two pinned inputs have to be put somewhere specific before anything
# downstream can run:
#
#   * openWakeWord ships no models inside its wheel. A fresh environment has an
#     empty `resources/models`, so the first `AudioFeatures(...)` in
#     build_dataset.py dies with NO_SUCHFILE — or, if the caller ever invokes
#     `openwakeword.utils.download_models()`, quietly trains against whatever
#     upstream publishes that day instead of the pinned bytes.
#   * generate_speech.py opens `<checkpoint>.json` beside the VITS checkpoint.
#     piper-sample-generator keeps that config in its git repository, not as a
#     release asset, so it cannot be fetched by URL and must be copied from the
#     pinned clone.
#
# Both installs verify SHA-256 at the source and again at the destination.
"$python_bin" - "$here" "$downloads" "$generator" <<'PY' || die "asset verification"
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import assets

into = Path(sys.argv[2])
generator_root = Path(sys.argv[3])

for asset in assets.ALL_ASSETS:
    assets.fetch(asset, into)
    print(f"  ok {asset.name} ({asset.license})")

for path in assets.install_feature_extractors(into):
    print(f"  installed {path.name} -> {path.parent}")

config = assets.install_generator_config(generator_root, into)
print(f"  installed {config.name} -> {config.parent} ({assets.GENERATOR_CONFIG_SOURCE})")
PY

step "1. synthesize speech (multi-speaker, disjoint train/eval pools)"
gen() { # group split count seed name [--accents]
  "$python_bin" "$here/generate_speech.py" \
    --out "$tts/$5" --model "$downloads/en_US-libritts_r-medium.pt" \
    --generator-root "$generator" \
    --group "$1" --split "$2" --count "$3" --seed "$4" --batch-size 32 ${6:-} \
    || die "generate $5"
}

# Training pool, speakers [0, 600).
gen positive      train      20000 101 positive_train
gen hard-negative train      12000 103 hardneg_train
gen soft-negative train       6000 105 softneg_train
# The measured-confusable subset, synthesized again at a higher rate.
gen confusable    train      10000 107 confusable_train
# Accents, and the everyday phrases an always-on microphone actually hears.
gen positive      train      12000 201 positive_train_accented   --accents
gen confusable    train       8000 202 confusable_train_accented --accents
gen common        train       6000 203 common_train              --accents

# Validation pool, speakers [600, 700). Its own voices, for choosing the epoch
# and the operating point without touching evaluation.
gen positive      validation  3000 301 positive_validation   --accents
gen hard-negative validation  2000 302 hardneg_validation    --accents
gen confusable    validation  2000 303 confusable_validation --accents
gen common        validation  1500 304 common_validation     --accents
gen soft-negative validation   800 305 softneg_validation    --accents

# Evaluation pool, speakers [700, 904). Built once and left alone.
gen positive      eval        2500 102 positive_eval
gen hard-negative eval        2000 104 hardneg_eval
gen soft-negative eval        1000 106 softneg_eval

step "2. build feature tensors"
"$python_bin" "$here/build_dataset.py" \
  --tts-root "$tts" --speech-commands "$work/data/speech_commands" \
  --out "$features" --split train --seed 202 \
  --recorded-negatives 50000 --noise-only 4000 --rir-count 200 || die "train features"

# Validation: its own speakers, its own share of the recorded-negative pool
# (partitioned on a hash of the speaker id) and its own two room-tone
# recordings. Both the epoch and the operating point are chosen on this.
"$python_bin" "$here/build_dataset.py" \
  --tts-root "$tts" --speech-commands "$work/data/speech_commands" \
  --out "$features" --split validation --seed 404 \
  --recorded-negatives 0 --noise-only 1500 --rir-count 80 || die "validation features"

# The evaluation split keeps its raw audio: the measurement runs the real
# engine over waveforms, not over precomputed features.
"$python_bin" "$here/build_dataset.py" \
  --tts-root "$tts" --speech-commands "$work/data/speech_commands" \
  --out "$features" --split eval --seed 303 \
  --recorded-negatives 0 --noise-only 1000 --rir-count 60 --keep-audio || die "eval features"

step "3. train, export both backends, check parity"
# The hyperparameters that produced the shipped artifact. Written out rather
# than left to defaults so a rerun reproduces this model and not a later
# default's.
"$python_bin" "$here/train_model.py" \
  --features "$features" --out "$models" \
  --channels 128 128 64 --epochs 60 --dropout 0.15 \
  --negative-weight 3.0 --hard-negative-weight 6.0 \
  --calibrate-operating-point || die "training"

step "4. measure through the product runtime"
"$python_bin" "$here/evaluate_model.py" \
  --features "$features" --models "$models" \
  --out "$evidence/measurements.json" --repo-root "$repo_root" || die "evaluation"

step "5. install artifacts"
cp "$models/hey_youtab.onnx" "$models/hey_youtab.tflite" "$repo_root/tools/wakewords/" \
  || die "install"

step "6. cut the committed regression fixture"
"$python_bin" "$here/make_test_fixture.py" \
  --features "$features" --tts-root "$tts" \
  --out "$repo_root/tests/fixtures/wakeword" || die "fixture"

# The card is generated, not written: it records the hashes of the artifacts as
# they are on disk right now, alongside the measurements taken from them. That
# is what stops it from describing a model that is no longer there.
step "7. write the model card and hashes"
"$python_bin" "$here/write_model_card.py" \
  --training "$models/training.json" \
  --measurements "$evidence/measurements.json" \
  --train-stats "$features/stats_train.json" \
  --eval-stats "$features/stats_eval.json" \
  --wakewords "$repo_root/tools/wakewords" || die "model card"
cat "$repo_root/tools/wakewords/SHA256SUMS"

printf '\nPASS: hey_youtab.onnx and hey_youtab.tflite built and measured\n'
