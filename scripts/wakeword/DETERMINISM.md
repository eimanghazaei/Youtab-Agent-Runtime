# Deterministic scoring: what the random prime cost

openWakeWord primes its feature buffer with unseeded random noise, in
`AudioFeatures.__init__` and again in `AudioFeatures.reset()`
(`openwakeword/utils.py:178`, 0.6.0):

```python
self.feature_buffer = self._get_embeddings(
    np.random.randint(-1000, 1000, 16000*4).astype(np.int16))
```

The classifier reads the last sixteen frames of that buffer, and
`evaluate_model.py` calls `reset()` before every clip — so this randomness was
present in every measurement this repository had taken.

`_OpenWakeWordEngine._prime_features` now overwrites that buffer with a prime
drawn once from a **local** `np.random.default_rng`, cached and copied on each
reset. Same distribution as upstream, minus the randomness. Not
`np.random.seed()`: seeding the global RNG would reach every other numpy user in
the process.

## How it was detected

Two passes over 220 identical held-out windows, same engine, same process,
disagreed on 92 of them. Restricted to 60 negative windows: 28 disagreed, worst
case by 0.031. Seeding numpy's global RNG made three passes bit-identical while
leaving it unseeded did not, which is what identified the prime as the cause
rather than backend nondeterminism.

After the fix, the same 220 windows: **0 differing**, for repeated resets and for
a freshly constructed engine alike.

## Re-measured: all seven candidates, deterministic

Every candidate was re-evaluated at its own frozen validation-selected threshold,
on the same evaluation corpus, with the same artifacts and no retraining. The
only difference is the prime.

### Accuracy: unchanged

Across 7 candidates × 2 backends × 4 accuracy metrics, **two** values moved, both
TFLite near-phrase rates, both by exactly one window in 4,000:

| candidate | metric | before | after |
|---|---|---|---|
| r4 | tflite near-phrase rate | 0.02875 | 0.02900 |
| r5c2 | tflite near-phrase rate | 0.01300 | 0.01325 |

In both cases the TFLite value moved *to match* its ONNX counterpart, because the
single detection disagreement between the backends was itself caused by the
random prime.

**No acceptance decision changed for any candidate on either backend.** False
rejects, recorded-speech false activations per hour, and background-only false
accepts are identical throughout.

### Parity: four orders of magnitude better

This is what the randomness was really corrupting. Parity compares per-frame
scores between two engines that each reset with their own independent random
draw, so the noise entered twice and was attributed to the backends.

| candidate | max frame delta before | max frame delta after | mean before | mean after | detection disagreements |
|---|---|---|---|---|---|
| r1 | 1.086e-01 | 4.664e-05 | 6.793e-06 | 3.986e-08 | 0 → 0 |
| r2 | 2.976e-01 | 2.044e-05 | 1.816e-05 | 4.056e-08 | **1 → 0** |
| r3 | 1.156e-01 | 1.132e-05 | 1.598e-05 | 3.984e-08 | 0 → 0 |
| r4 | 1.846e-01 | 5.007e-06 | 1.839e-03 | 7.467e-08 | **1 → 0** |
| r5c1 | 1.225e-01 | 5.543e-06 | 5.876e-04 | 5.258e-08 | 0 → 0 |
| r5c2 | 1.401e-01 | 8.047e-06 | 1.793e-03 | 6.979e-08 | **1 → 0** |
| r5c3 | 1.470e-01 | 1.693e-05 | 4.077e-05 | 4.543e-08 | 0 → 0 |

All seven candidates now show **zero** detection disagreements between ONNX and
TFLite.

### A previous conclusion this corrects

r1's parity was recorded as a maximum per-frame delta of 1.086e-01 and read as
evidence that the two front ends differ materially. That attribution was wrong.
The delta was the random prime, not the backends: the true divergence between the
converted graphs is on the order of 1e-5, roughly 2,000× smaller, and it is
consistent across every candidate.

## What this does not change

The model still fails acceptance. No candidate reaches all five production
targets on either backend, and the deterministic numbers confirm that at the same
values as before — the randomness was never what stood between these candidates
and 5/5. What it stood between was any two runs agreeing with each other.
