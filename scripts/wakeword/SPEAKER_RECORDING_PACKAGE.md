# Recording package — speakers E003 and onwards

Hand this to a person who has agreed to record. It is written to be read by
them, not by the pipeline: everything they need is here, and nothing here needs
them to understand openWakeWord.

**The recordings never enter this repository, are never uploaded anywhere, and
never leave the machine they are copied to.** That is a promise made to the
speaker, and it is enforced rather than remembered —
`tests/tools/test_wakeword_no_human_data_committed.py` fails the build if audio,
a feature tensor, a transcript or a consent record is ever committed.

## Why more speakers at all

Every positive this model has ever been trained on came from one text-to-speech
family. That is the standing explanation for why no candidate has passed
acceptance: the model has heard hundreds of synthetic voices and two real ones.
Two real speakers exist today — E001, which is training-only, and E002, which is
a sealed single-use evaluation set and is spent the moment it is measured
against.

So this package exists to produce (a) real positives that training can learn
from, and (b) a *new* sealed evaluation set, so that measuring one does not
consume the other.

### How many speakers, and how many takes

**Four to six speakers**, each recording the take list below once. Two of them
are marked sealed-evaluation before anything is recorded, and their audio is
never shown to training.

The numbers come from what has to be *demonstrated*, using the rule of three:
with zero failures in *n* trials the 95% upper bound on the rate is about 3/*n*.

| target | trials needed for a clean run to prove it | this package |
|---|---|---|
| false rejects ≤ 5% | 60 positives | 36 positives × 4 speakers = **144** |
| near-miss false accepts ≤ 2% | 150 near-phrase utterances | 40 near × 4 speakers = **160** |

Per speaker, 36 positives with no misses only bounds that speaker's false-reject
rate at 8%, so a single speaker's session is never enough on its own. The claim
is made across speakers; the per-speaker numbers are for spotting the one voice
the model hates.

A session is about 25 minutes including setup.

## Coverage, and why each dimension is there

Every dimension below changes the *signal*, not the words. A model that works
on one laptop, at one distance, in one quiet room, has been measured on one
cell of this table.

| dimension | levels to cover | why it matters |
|---|---|---|
| device | phone (handheld), laptop (built-in microphone), wired headset | Three different frequency responses and three different distances-to-mouth. A headset boom is 3 cm from the lips and has almost no room in it; a laptop array is 50 cm away, applies its own beamforming and noise suppression, and is what most desktop users actually have. |
| distance | close (~30 cm), room (2–3 m) | Distance is not just level. At 2–3 m the direct-to-reverberant ratio collapses and the room's own response is a large part of what the microphone hears — which is exactly what the synthetic impulse responses in training are approximating, and nobody has checked that approximation against a real room. |
| accent and first language | at least two different first languages; at least three English varieties among the speakers | The model's accent coverage comes from phonemizing through twelve espeak-ng voices, which changes the phoneme string and not the acoustics. A real second-language speaker differs in rhythm, vowel space and consonant release all at once, and none of that is in the training distribution. |
| pitch range | at least one low (≈85–120 Hz), one mid, one high (≈200–255 Hz) fundamental | The front end is a mel spectrogram, so fundamental frequency moves where the harmonics land under every filter. The synthetic voice pool is wide but it is a pool of *interpolated* speaker embeddings, which tends to fill the middle and thin out the extremes. |
| noise condition | quiet room, background speech or television, kitchen or street noise | An always-on microphone spends its whole life in the last two. Background *speech* matters most: it is the condition where a false activation is most likely and the one the current negatives — single words and read sentences — represent least. |

Cover the cells across the group rather than within each speaker: every speaker
does close and far, and the devices and noise conditions are spread across
people. Record which cell each block was in — the manifest note is the place.

## Before the session

1. **Consent first.** Fill in `CONSENT_RECORD_TEMPLATE.md` with the speaker,
   read it aloud or let them read it, and keep the filled copy next to their
   recordings. No recording begins before this exists.
2. **Turn off cloud sync on the recording device.** This is the one step people
   skip. iOS Voice Memos syncs to iCloud by default and Android recorders back
   up to Google Drive; a recording promised to stay on local disk is on
   somebody's server before the session ends. Turn it off for the app, record,
   copy the files off over a cable or the local network, then delete them from
   the device.
3. **Pick a label.** `E003`, `E004`, … No name, no initials, no email in any
   filename or directory name. The label is the only identifier that travels
   with the audio; the consent record is the only place the person's name
   appears, and it stays with them and with the operator.
4. **Set the room.** For the quiet blocks, no television, no fan, no dishwasher.
   For the noisy blocks, put the noise on deliberately and at a level where a
   conversation would still be comfortable — the point is realism, not a
   stress test nobody would attempt.

## How to say it

Read these before starting. They are the difference between a usable session
and one that measures the wrong thing.

* **Say the name as one unit: "youtab", not "you… tab".** A small pause after
  *hey* is fine — the model is trained on that spelling. A pause *inside the
  name* is not. It splits the wake phrase into two words the detector has never
  been trained on, and the trailing 1.28 s the model actually looks at may not
  even contain the whole phrase any more. **This is the single most important
  instruction in this document.** In the E002 session, `hey you tab` was spoken
  with a pause between "you" and "tab" in every take, which fragmented all of
  them and left the hardest confusion in the whole inventory — `hey you tab`
  against `hey you tap` — essentially untested. If you hear yourself pause,
  discard the take and do it again.
* **Do not perform it.** Say it the way you would say it to a device across the
  room while doing something else. Over-enunciated wake words are the ones that
  work in the lab and fail in the kitchen.
* **Vary naturally between takes.** Slightly faster, slightly quieter, mid-yawn,
  mid-sentence. Identical takes measure one utterance many times.
* **Leave a second of silence around each take.** The detector scores a 2-second
  window; a take clipped at the start is a take that was never recorded.
* **One phrase per file, or one long file with a pause between takes.** Either
  is fine. Do not edit the audio afterwards — no normalisation, no noise
  removal, no trimming beyond splitting. The processing chain is part of what is
  being measured.

## The take list

`takes` is per speaker. Prompts are written the way they should be *said*, not
the way they are spelled in the product.

The `in the synthesis inventory?` column says whether training has ever seen a
synthesized version of that phrase — it is checked against `phrases.py` by
`tests/tools/test_wakeword_recording_package.py`, so it cannot drift into being
decorative. A **no** means nobody has any evidence about that phrase at all:
never synthesized, and never recorded either. A **yes** is not a claim that it
was ever *recorded* — bare `hey` is the example, synthesized thousands of times
and never once said into this evaluation by a person. The `why` column is where
that distinction lives.

| # | prompt | class | takes | in the synthesis inventory? | why it is in the list |
|---|---|---|---|---|---|
| 1 | `hey youtab` | positive | 12 | yes | The phrase itself, and the spelling most people say. Spread across the device, distance and noise blocks. |
| 2 | `hey you tab` | positive | 8 | yes | Same phrase, unstressed carrier — and half of the hardest minimal pair in the inventory. Say it without pausing between "you" and "tab"; this is the take E002 lost. |
| 3 | `hey yoo tab` | positive | 6 | yes | Hard /t/ rather than the flapped American realisation. Both are things people say and espeak-ng renders them differently. |
| 4 | `hey, youtab` | positive | 6 | yes | A natural beat after the carrier word. Trained on, and the one pause that is allowed. |
| 5 | `hey youtab` shouted from 2–3 m | positive | 4 | yes | Raised voice changes vowel quality, not only level. Recorded separately so a miss can be attributed. |
| 6 | `okay youtab` | near | 4 | **no** | The only near phrase that carries the real keyword under a different carrier word, and it was never recorded from E002. People say "okay X" out of habit from other assistants, so this fires often in the field if the model has keyed on "youtab" alone rather than on the whole phrase. |
| 7 | `hey google` | near | 4 | **no** | A competing wake word with the same carrier and a stressed /uː/ in the next syllable. Anyone with another assistant in the house says this near the microphone all day. Never recorded from E002. |
| 8 | `hey` | near | 4 | yes | The carrier word alone. Synthesized thousands of times and never once recorded from a person — E002 did not say it — and it is the single most common thing an always-on microphone hears that begins like the wake word. |
| 9 | `hey siri` | near | 2 | **no** | Same class as `hey google`, second carrier. |
| 10 | `hey you tap` | near | 6 | yes | The other half of the hardest pair: one voicing feature from `hey you tab`. Record it immediately after take 2, same device, same distance, so the two are directly comparable. |
| 11 | `hey your tab` | near | 4 | yes | Measured as one of the top confusions of the first trained model. |
| 12 | `hey youtube` | near | 4 | yes | The obvious confusion. Measured as *not* the worst one, which is why the list does not stop here. |
| 13 | `youtab` | near | 4 | yes | The name with no carrier. Must not fire. |
| 14 | `hey new tab` | near | 2 | yes | The frame "hey <something> tab", which is what actually fires. |
| 15 | `hey do tab` | near | 2 | yes | Same frame, nonsense filler, to separate the frame from the words in it. |
| 16 | `hey you talk` | near | 2 | yes | Same onset, different coda. |
| 17 | `hey utah` | near | 2 | yes | Two syllables, same stress pattern, no /b/. |
| 18 | `hey stab` | near | 2 | yes | Rhymes with the final syllable, no /juː/. |
| 19 | `hey you` | near | 2 | yes | The prefix, stopped before the name. |
| 20 | `okay tab` | near | 2 | yes | The other carrier with the bare noun. |
| 21 | `open a new tab` | near | 2 | yes | Ordinary speech about tabs, which is what an agent's user talks about. |
| 22 | `check the network tab` | near | 2 | yes | The same, in a developer's vocabulary. |
| 23 | `hey, how are you` | near | 2 | yes | The most common thing that starts with the carrier word. |
| 24 | `eight` | near | 2 | yes | Measured: /eɪ/ with no /h/ onset once pinned the operating threshold by itself. |
| 25 | `happy` | near | 2 | yes | Measured: /h/ onset that is not "hey" took its place in the next round. |
| 26 | one minute of ordinary conversation, no wake word | near | 1 | n/a | The per-hour false-activation rate is about continuous speech, not isolated phrases. One minute per speaker will not measure a rate, but it will catch a model that fires on nothing in particular. |

## After the session

1. Copy the files into one directory per speaker under the capture root, named
   for the label only: `E003/`, `E004/`. Never inside this repository — the
   manifest tool refuses a destination under the checkout, and the commit gate
   refuses the audio.
2. Freeze it immediately, before anything reads it:

   ```bash
   python scripts/wakeword/freeze_manifest.py freeze \
       --root <capture-root>/E003 \
       --out  <capture-root>/E003.manifest.json \
       --dataset E003 --split train --usage training \
       --note "phone+laptop, close+3m, quiet+TV, en-GB, mid pitch"
   ```

   For a sealed speaker, `--split evaluate --usage sealed-evaluation`. That is
   not a label: `assert_usable_for` raises if anything later tries to train on
   it, and the CLI exits non-zero. Freeze before the first read, so the frozen
   set is the recorded set and not the set as it stood after somebody tidied it.
3. Record the coverage cell in `--note`: device, distance, noise, accent, pitch
   band. It is the only place that information survives, and a measurement
   without it cannot be attributed to a condition.
4. Delete the originals from the recording device, and confirm the cloud copy
   is gone as well as the local one.
5. File the consent record next to the manifest.

## If a speaker withdraws

Delete the speaker's directory, their manifest, and their consent record. Say
so in the qualification record: a measurement taken over a set that no longer
exists has to be re-stated over the set that does. Nothing in this repository
has to change, because nothing in this repository ever contained their audio —
which is the point of the whole arrangement.
