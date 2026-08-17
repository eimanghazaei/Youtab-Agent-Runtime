# Recording your voice for "Hey Youtab"

Thank you for helping test and train Youtab's wake word. This document is
everything you need: what to record, how many times, how to name the files,
and how to hand them over. It takes about **65 minutes**, done in one sitting
whenever suits you.

Nothing here requires technical skill beyond using your phone's voice
recorder. If anything is unclear, ask your coordinator before you start
rather than guessing — a guess here becomes a file we cannot use.

## Why real recordings

Earlier rounds of this model were trained entirely on computer-generated
speech. It measured well on computer-generated tests and then missed real
people more often than it should have. A small batch — 75 utterances — from
one real person cut how often it missed a genuine wake word 2.4x over, at a
matched false-accept rate. A follow-up check went
further, to test whether the recording channel — a phone, a lossy codec —
was itself the problem: the same person's ordinary close-up speech fired
15 times out of 15, essentially perfectly, but the *same* person speaking a
little differently, standing further away, or talking with real background
noise running fired only 36.7%, 53.3% and 40.0% of the time respectively —
missing roughly half to two-thirds of genuine attempts. So the channel isn't
the problem: the model is missing the ordinary variation in how people
actually talk, which no synthetic voice produced. That variation is what
this recording round exists to give it.

## Before you start

1. **Read this whole document once** before recording anything.
2. **Sign the consent form** — `CONSENT_RECORD_TEMPLATE.md`, filled in and
   given to you by your coordinator. It tells you, specifically, whether your
   recordings will be used to train the detector, to help choose between
   versions of it, or kept completely separate as a final, fair test of the
   finished thing. Different speakers in this round are used differently;
   your form states which applies to you. The recording steps below are
   identical either way, so just follow this document regardless of which
   box is checked.
3. **Fill in the device and environment form.** Copy
   `speaker_metadata.template.json`, fill in every bracketed `<...>` with a
   real answer, and save it as `metadata.json` inside your own recording
   folder (see "Folder and file naming" below). It asks for your device make
   and model, the recording app, the room you're in, its approximate size,
   its floor and wall surfaces, and anything nearby that usually makes noise.
   This takes about 5 minutes.
4. **Pick a main room** to record most of this in — somewhere you can also
   speak from further away or from an adjoining room, and where at least two
   of the following are genuinely available and can actually be switched on:
   a television, a kitchen with running appliances, a window facing a street,
   or a fan.
5. **Use your phone's own voice recorder app** (Voice Memos, the built-in
   Android recorder, or similar), or a dedicated recorder if you have one.
   Whatever it produces — `.m4a`, `.wav`, whatever your app writes by
   default — is fine and should be left exactly as it is. See "The one rule
   behind everything" below.

## The one rule behind everything

**One condition, one genuine recording.** If a section below asks for
"quiet", record yourself actually speaking quietly — never take a normal
recording and turn the volume down in an app. If a section is missing, go
back and record it for real; never generate it by editing another file. This
applies to every section, without exception, because a processed file
teaches the model the wrong thing: it would learn what your phone's software
does to audio, not what a person quietly saying "hey youtab" actually sounds
like.

**Nothing is trimmed, converted, normalised, denoised, or re-recorded to
replace a take you didn't like.** Leave your recorder's raw output exactly as
it comes out — including the takes where you stumbled, coughed, or think you
sounded odd. Difficult, natural speech is the entire point of this round;
deleting it back out would undo the reason you were asked to record it.
Whether a particular recording is usable is decided later, by fixed rules
applied the same way to everyone's recordings — never by a speaker's own
judgement of their own take, and never by editing it into something you
judge better.

## What you're recording — six sections

| # | Section | Covers |
|---|---|---|
| 1 | The wake phrase, five ways | natural close-mic positives; normal, slow, fast, quiet, loud delivery |
| 2 | Far-field | genuinely standing further away |
| 3 | Real background noise | the wake phrase with real TV/kitchen/street/fan noise actually running |
| 4 | Hard near phrases | phrases that sound close to "hey youtab" but are not, plus the phrase itself said in its trickier forms |
| 5 | Free-speech negatives | ordinary talking, no wake phrase |
| 6 | Background-only | your room, recorded, with nobody speaking |

Record them in this order; it matches the folders below and keeps the
device/room setup for each section together.

### Section 1 — The wake phrase, five ways

Say **"hey youtab"** naturally — the way you'd actually get the assistant's
attention, not slowly enunciated like a dictionary entry — close to your
phone (arm's length is fine), five different ways:

| Condition | How | Takes |
|---|---|---|
| `normal` | Your ordinary speaking voice and pace | 5 |
| `slow` | Noticeably slower than normal, but still one natural phrase | 5 |
| `fast` | Noticeably faster, as if in a hurry | 5 |
| `quiet` | Genuinely quiet — as if not to wake someone, not a whisper if that feels unnatural to you | 5 |
| `loud` | Genuinely loud — as if calling across a room, not shouted so hard it distorts | 5 |

Record each take as its own separate recording (start, say the phrase once,
stop), 25 takes in total for this section.

### Section 2 — Far-field

Stand at least **5 metres away** from your phone — an adjoining room with the
door open works well if your home doesn't have a room that large — and say
"hey youtab" in your normal voice and pace, 5 times. Record your actual
distance on the metadata form (`farfield_distance`).

### Section 3 — Real background noise

Pick **at least two** of: a television, a kitchen with an appliance or tap
running, an open window facing a street, a fan. Turn one on for real, let it
settle for a few seconds, then say "hey youtab" close to your phone (as in
Section 1's `normal` condition) 5 times with it genuinely running. Repeat for
your second noise source. If you use more than two, that's welcome — just
give each its own folder as described below.

Record on the metadata form which sources you used
(`noise_sources_used`) — it must name at least two, and they must match the
folders you actually create.

### Section 4 — Hard near phrases

This section fixes four specific gaps found in an earlier speaker's
recordings: "okay youtab" — the real name under a different lead-in word —
was never recorded; "hey google" was never recorded; bare "hey" on its own
was never recorded as its own item; and "hey you tab" was recorded with a
pause in the middle that broke the name apart. All four are in the table
below.

Say each phrase **naturally, as one continuous sentence** — no artificial
pause anywhere in it, and in particular, **no pause inside the name**.
"hey&nbsp;youtab", however it's spelled below, is one word to say, not two.
A pause after "hey" is fine and is how people actually say it; a pause
between "you" and "tab" is not, and produced exactly this problem in an
earlier round.

Two of the phrases below — marked **Wake word** — are genuine ways of saying
"hey youtab" itself, so should trigger the assistant. The rest are marked
**Should NOT wake it**: say them exactly as an ordinary sentence, the way
you'd actually say them, not as a trap or a trick.

**Record rows 1 and 2 back to back, on the same device, without switching
anything in between.** They are a matched pair — the same phrase with one
sound changed — and being recorded together is what makes them useful as a
pair.

| # | Phrase | Type | File slug | Note |
|---|---|---|---|---|
| 1 | "hey you tab" | **Wake word** | `hey-you-tab` | One continuous name — no pause between "you" and "tab". |
| 2 | "hey you tap" | Should NOT wake it | `hey-you-tap` | Record straight after row 1, same device. The single most likely mix-up: it differs from row 1 by one sound. |
| 3 | "hey yoo tab" | **Wake word** | `hey-yoo-tab` | Also one continuous name, hard-T version. |
| 4 | "okay youtab" | Should NOT wake it | `okay-youtab` | The real name, wrong lead-in word. Never recorded before this round. |
| 5 | "hey google" | Should NOT wake it | `hey-google` | The lead-in word, a different (well-known) name. Never recorded before this round. |
| 6 | "hey" (on its own) | Should NOT wake it | `hey` | Say it alone, as if starting to say something and not finishing. Never recorded as its own item before this round. |
| 7 | "hey your tab" | Should NOT wake it | `hey-your-tab` | |
| 8 | "hey new tab" | Should NOT wake it | `hey-new-tab` | |
| 9 | "hey utah" | Should NOT wake it | `hey-utah` | |
| 10 | "hey do tab" | Should NOT wake it | `hey-do-tab` | |
| 11 | "hey stab" | Should NOT wake it | `hey-stab` | |
| 12 | "youtab" (on its own) | Should NOT wake it | `youtab` | |
| 13 | "hey there" | Should NOT wake it | `hey-there` | |
| 14 | "youtab is running" | Should NOT wake it | `youtab-is-running` | |
| 15 | "hey you had" | Should NOT wake it | `hey-you-had` | |
| 16 | "hey cab" | Should NOT wake it | `hey-cab` | |
| 17 | "hey you talk" | Should NOT wake it | `hey-you-talk` | |
| 18 | "a new tab" | Should NOT wake it | `a-new-tab` | |
| 19 | "eight" | Should NOT wake it | `eight` | |
| 20 | "two" | Should NOT wake it | `two` | |
| 21 | "visual" | Should NOT wake it | `visual` | |
| 22 | "four" | Should NOT wake it | `four` | |
| 23 | "zero" | Should NOT wake it | `zero` | |
| 24 | "house" | Should NOT wake it | `house` | |
| 25 | "down" | Should NOT wake it | `down` | |
| 26 | "happy" | Should NOT wake it | `happy` | |
| 27 | "stop" | Should NOT wake it | `stop` | |
| 28 | "yes" | Should NOT wake it | `yes` | |
| 29 | "left" | Should NOT wake it | `left` | |
| 30 | "no" | Should NOT wake it | `no` | |

Say each phrase **3 times**, close to your phone as in Section 1's `normal`
condition — 90 takes in total for this section. Rows 19–30 above are single
words; that's correct, not a mistake in the table — say the one word, as you
normally would.

*(For coordinators and engineers: rows 1 and 3 are
`phrases.POSITIVE_SPELLINGS`; every other row except 4 and 5 is
`phrases.HARD_NEGATIVES`, and everything from row 2 onward that phrases.py
also weights as `CONFUSABLE_NEGATIVES` is the model's own measured, ranked
list of what it actually confused "hey youtab" with, not a guessed list. Rows
4 and 5 are not in `phrases.py` yet — see `speaker_recording_spec.py`, which
this table and `validate_speaker_submission.py` are both generated from and
checked against.)*

### Section 5 — Free-speech negatives

Talk naturally for about **5 minutes**, about anything — your day, a hobby,
reading something aloud — as one continuous recording. Just don't say "hey
youtab" or any phrase from the table above. This does not need to be
interesting or planned; ordinary rambling is exactly what's wanted.

### Section 6 — Background-only

Record about **3 minutes** of your room with nobody speaking — leave the
recorder running and step back or stay quiet. This is not silence in a
technical sense; whatever your room actually sounds like (a fridge, traffic
outside, nothing at all) is the point.

## Why these counts, not round numbers

The shipping targets for this model include a false-accept rate on
deliberate near misses of at most 2%, and on ordinary recorded speech of at
most 0.2 activations per hour (see `README.md`'s target table). A standard
way to size a sample that can *prove* a rate is below a target — the "rule of
three" — says that observing zero failures across `n` trials bounds the true
rate at roughly `3 ÷ n` with about 95% confidence. Applied here: certifying
the 2% near-miss target from a single phrase would need about `3 ÷ 0.02 =
150` takes of that one phrase, and certifying the 0.2/hour speech target
would need about `3 ÷ 0.2 = 15` hours of continuous talking. Neither is
something one volunteer should be asked to do in a sitting, and asking for it
would defeat the point — speech repeated 150 times identically, or extended
to 15 hours, stops being the natural speech this round exists to capture.
That certification is `evaluate_model.py`'s job, at the pooled, many-speaker
scale those targets are actually about.

What this package's counts are sized for instead is **redundancy against a
single bad take**, at a scale five people can complete in parallel. Three
repetitions of a near phrase is the floor that survives one recording being
excluded later (a cough, a dropped phone) and still leaves two genuine
examples — two would leave only one, and one is a single point that can't be
told apart from a fluke. The three conditions an earlier check found broken —
varied delivery, far-field, and real noise — get five repetitions each rather
than three, because they are specifically what this round exists to fix, and
five lets two exclusions happen and still leave a clear majority reading (at
least three of five agreeing) rather than a coin flip. Near phrases trade
depth for breadth on purpose: thirty distinct phrases matter more here than
extra takes of any one of them.

## Time estimate

| Section | What | Time |
|---|---|---|
| Read-through and setup | | 5 min |
| Consent form | | 5 min |
| Device & environment form | | 5 min |
| 1. Wake phrase, five ways | 25 takes | 7 min |
| 2. Far-field | 5 takes | 4 min |
| 3. Real background noise | 10 takes | 6 min |
| 4. Hard near phrases | 90 takes | 18 min |
| 5. Free-speech negative | 1 continuous take, ~5 min | 6 min |
| 6. Background-only | 1 continuous take, ~3 min | 4 min |
| Self-check and handoff prep | | 5 min |
| **Total** | **132 audio files** | **≈ 65 min** |

## Folder and file naming

Create one folder named with your speaker ID (your coordinator gives you
this — do not use your name). Everything below goes inside it. **Names
matter exactly as written**: our loader maps a file to what it contains by
its name and folder alone, and an unrecognized name is treated as an error,
not a guess. Keep whatever file extension your recorder produces (`.m4a`,
`.wav`, `.caf` — whatever it is); never convert it.

```
<speaker-id>/
  metadata.json                              your filled-in device/environment form

  positive_normal/
    hey-youtab_normal_001.m4a                ... through _005

  positive_slow/
    hey-youtab_slow_001.m4a                  ... through _005

  positive_fast/
    hey-youtab_fast_001.m4a                  ... through _005

  positive_quiet/
    hey-youtab_quiet_001.m4a                 ... through _005

  positive_loud/
    hey-youtab_loud_001.m4a                  ... through _005

  positive_farfield/
    hey-youtab_farfield_001.m4a              ... through _005

  positive_noise_tv/                         one folder per noise source you used
    hey-youtab_noise-tv_001.m4a              ... through _005          (name: tv, kitchen, street, or fan)

  positive_noise_kitchen/
    hey-youtab_noise-kitchen_001.m4a         ... through _005

  near_phrase/
    hey-you-tab_001.m4a                      ... through _003
    hey-you-tap_001.m4a                      ... through _003
    ...one sub-series per phrase in the Section 4 table, using its File slug...
    no_001.m4a                               ... through _003

  negative_freespeech/
    freespeech_001.m4a                       one continuous file (a few, if your app splits long recordings)

  background_only/
    background_001.m4a
```

The pattern, spelled out:

| Folder | Filename |
|---|---|
| `positive_<condition>/` | `hey-youtab_<condition>_NNN.ext` |
| `positive_noise_<source>/` | `hey-youtab_noise-<source>_NNN.ext` |
| `near_phrase/` | `<phrase-slug>_NNN.ext` |
| `negative_freespeech/` | `freespeech_NNN.ext` |
| `background_only/` | `background_NNN.ext` |

`NNN` is a three-digit take number starting at `001`. If your recorder app
names files its own way ("Voice Memo 3", "recording_2026-08-17_1"), rename
each file to match the pattern above before handing your folder over — an
unrenamed file is exactly the kind of "unrecognized file" the loader errors
on rather than guessing about.

## Uploading your recordings

- Your recordings **never leave your own machine except to your
  coordinator**, by whatever private method they tell you to use. They are
  never committed to git, never pushed to a code repository, and never
  attached to a chat, ticket, or email thread that isn't your coordinator
  directly.
- **Turn off cloud-sync for wherever you record and store these files**
  before you start. iCloud Photos/Drive, Google Photos backup, Google Drive
  or OneDrive desktop sync, and Dropbox all auto-upload anything placed in a
  synced folder — recording straight into one of these, even briefly, can
  push a real person's raw voice recording to a cloud account without you
  noticing. If you're not sure whether a folder syncs, use a folder you know
  doesn't (for example, one you create fresh, outside your Photos/Camera
  Roll and outside any Drive/Dropbox folder).
- Hand your folder to your coordinator exactly as instructed by them — a USB
  drive, a direct cable transfer, or a private, non-syncing link they
  provide. Do not use a personal cloud-sync folder or email to send the
  audio.
- Hand your **signed consent form separately**, by the method described in
  `CONSENT_RECORD_TEMPLATE.md` — never in the same folder or transfer as the
  audio.

## Consent, in brief

Full workflow, retention, and withdrawal details are in
`CONSENT_RECORD_TEMPLATE.md`. In short: you sign before you record, your
coordinator holds the signed original outside of any code repository, you
can withdraw at any time by telling your coordinator, and withdrawal deletes
your raw files but — importantly — cannot undo a model that has already been
trained using them before you withdrew. Read the full form; it explains this
properly.

## Before you upload: quality self-check

This checklist is about **completeness**, not about judging your own
performance. Nothing on it asks you to delete, redo, or "clean up" a
recording — see "The one rule behind everything" above for why.

- [ ] Every folder listed above exists and has files in it. If a whole
      section is missing, go back and record it — don't skip it.
- [ ] File names match the pattern shown, including for any file your app
      auto-named — check a few by eye.
- [ ] No file mixes two conditions (e.g. you didn't say a "normal" take and
      a "slow" take in the same recording).
- [ ] Rows 1 and 2 of the Section 4 table ("hey you tab" / "hey you tap")
      were recorded back to back, on the same device.
- [ ] The Section 5 and 6 recordings are each one continuous, unedited take
      of roughly the target length — check your recorder's own timer, don't
      guess.
- [ ] Playback check: listen to two or three files at random and confirm the
      words are audible — even a quiet or fast take should still be
      recognisable as words, just delivered that way.
- [ ] If your recorder shows a level meter and it pinned at maximum during
      the `loud` takes, that's fine — leave the recording as it is, and
      mention it in the `notes` field of your metadata form rather than
      re-recording.
- [ ] `metadata.json` is filled in completely, with no bracketed `<...>`
      placeholder text left in it.
- [ ] The consent form is signed and has gone to your coordinator
      separately — it is not included with the audio handoff.

A stumble, a cough mid-sentence, a take you think sounded bad — leave it in.
It is frequently the most useful recording in the folder: difficult, natural
speech is exactly what earlier, computer-generated training data could not
produce. Any exclusions happen later, on our side, using rules fixed in
advance and applied the same way to everyone — never based on how a speaker
felt about their own take.
