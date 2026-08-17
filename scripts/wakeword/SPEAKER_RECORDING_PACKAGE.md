# Recording package — "Hey Youtab", speakers E003 to E007

Thank you for helping train and test Youtab's wake word. This document is
everything you need: what to record, how many times, how to name the files, and
how to hand them over. It takes about **51 to 71 minutes**, done in one sitting
whenever suits you — see "Minimum vs expected" under "Time estimate" for what
the two numbers mean.

Nothing here requires technical skill beyond using your phone's voice recorder.
If anything is unclear, ask your coordinator before you start rather than
guessing — a guess here becomes a file we cannot use.

**The recordings never enter this repository, are never uploaded anywhere, and
never leave the machine they are copied to.** That is a promise made to the
speaker, and it is enforced rather than remembered —
`tests/tools/test_wakeword_no_human_data_committed.py` fails the build if audio,
a feature tensor, a transcript or a consent record is ever committed.

## Quick start — the one page

Everything below explains *why*; this page is enough to actually record. Read
Part A in full at least once before you start — it has the detail behind every
line here — but if this is the only page you have open while recording, it is
enough.

**What to say.** The wake phrase is **"Hey Youtab."** Say it the way you'd
actually get the assistant's attention — not slowly, not performed — and never
pause *inside* the name: "hey" then "youtab" run together as one word, in
whatever accent is genuinely yours. A short pause right after "hey" is fine.

**How many times.** Six sections, recorded in the order they're listed, ≈155
files, ≈51 minutes of actual recording (≈71 minutes counting the paperwork
around it — see "Time estimate" below). The exact phrase list, in the exact
order to record it, is "The near-phrase battery" table further down — record
every row in the order it's printed, including the ones that must *not* wake
the assistant.

**Where it goes.**

```
E003/                          your folder, named for your speaker label only
  originals/                   everything you record, exactly as your recorder wrote it
  RECORDING_METADATA.json      the device/room form you fill in
  CONSENT.pdf                  your coordinator adds this
  SHA256SUMS                   your coordinator adds this
```

The full layout, with every subfolder, is under "Folder and file naming"
below.

**What not to do.**

- Don't pause between "you" and "tab" — that one mistake has already ruined a
  session; see "The single most important instruction in this document" below.
- Don't delete, trim, denoise or re-record a take because you don't like it —
  leave it in; see "The one rule behind everything" below for why.
- Don't manufacture a condition — e.g. turning the volume down in an app
  instead of actually speaking quietly.
- Don't put your name, initials or email in any filename or folder name — the
  speaker label your coordinator gives you is the only identifier that travels
  with the audio.
- Turn off cloud sync before you record anything.

**Before you hand it over**, run (or ask your coordinator to run):

```bash
python scripts/wakeword/validate_speaker_submission.py <your-folder>/E003
```

It lists exactly what's missing and, section by section, how much has been
accepted so far. Fix what it flags and re-run it until it says everything is
present.

Not sure how to type over a hundred filenames without a typo? You don't have
to — see the automatic renamer under "Folder and file naming".

## Who this document is for

**Part A** is for the speaker, and is the same for every speaker in this round.
**Part B** is for the coordinator: the phrase taxonomy and where each label
comes from, the coverage the group has to cover between them, the arithmetic
behind the take counts, and how a submission is assembled and frozen. A speaker
is welcome to read Part B; nothing in it is a secret. It is separate because
none of it changes what you record.

## Why this round exists

Every positive this model has ever been trained on came from one text-to-speech
family. That is the standing explanation for why no candidate has passed
acceptance: the model has heard hundreds of synthetic voices and two real ones.
Two real speakers exist today — E001, which is training-only, and E002, which is
a sealed single-use evaluation set and is spent the moment it is measured
against.

The measurements say the same thing from the other side. A small batch — 75
utterances — from one real person cut how often the model missed a genuine wake
word 2.4x over, at a matched false-accept rate. A follow-up check went further,
to test whether the recording channel — a phone, a lossy codec — was itself the
problem: the same person's ordinary close-up speech fired 15 times out of 15,
essentially perfectly, but the *same* person speaking a little differently,
standing further away, or talking with real background noise running fired only
36.7%, 53.3% and 40.0% of the time respectively — missing roughly half to
two-thirds of genuine attempts. So the channel isn't the problem: the model is
missing the ordinary variation in how people actually talk, which no synthetic
voice produced.

So this round exists to produce (a) real positives that training can learn from,
(b) a validation voice that threshold and candidate selection can be made
against, and (c) a *new* sealed evaluation set, so that measuring one does not
consume the other.

### Five speakers, and how each is used

The five speaker labels in this round are **E003 to E007**.

Each of the five is assigned exactly one use — training, validation, or sealed
final qualification — **before any recording begins**, and the assignment never
changes afterwards. Both directions of a late change are unrecoverable:
promoting a sealed speaker into training spends the only measurement nobody has
tuned against, and demoting a training speaker into the sealed set retro-fits a
measurement to a model that has already seen that voice. The table lives in
`scripts/wakeword/speaker_recording_spec.py` (`SPEAKER_ASSIGNMENTS`) and is
pinned by `tests/tools/test_wakeword_speaker_recording_spec.py`, so changing it
means editing a test, which is a review decision rather than an edit.

This document does not say which speaker holds which role, and it is identical
for every speaker, because the recording instructions are identical for every
role and because knowing you are the final exam changes how you speak. What you
are told — in full, in writing, before you sign anything — is on your own
consent form: `CONSENT_RECORD_TEMPLATE.md` has your coordinator tick exactly one
use category for you, and that is the honest and complete answer for your
recordings.

---

# Part A — for the speaker

## Before you start

1. **Read this whole document once** before recording anything.
2. **Sign the consent form** — `CONSENT_RECORD_TEMPLATE.md`, filled in and given
   to you by your coordinator. It tells you specifically whether your recordings
   will be used to train the detector, to help choose between versions of it, or
   kept completely separate as a final, fair test of the finished thing. No
   recording begins before this exists, and you keep a copy of it.
3. **Fill in the device and environment form.** Copy
   `RECORDING_METADATA.template.json`, fill in every bracketed `<...>` with a
   real answer, and save it as `RECORDING_METADATA.json` in your recording
   folder (see "Folder and file naming"). It asks for your device make and
   model, the recording app, the room you're in, its approximate size, its floor
   and wall surfaces, and anything nearby that usually makes noise. About 5
   minutes.
4. **Use the label your coordinator gives you** — `E003`, `E004`, … No name, no
   initials, no email in any filename or folder name. The label is the only
   identifier that travels with the audio; the consent record is the only place
   your name appears.
5. **Turn off cloud sync on the recording device.** This is the one step people
   skip. iCloud Photos/Drive, Google Photos backup, Google Drive, OneDrive and
   Dropbox all auto-upload anything placed in a synced folder, so a recording
   promised to stay on local disk can be on somebody's server before the session
   ends. Turn it off for the app, record into a folder you know does not sync,
   copy the files off over a cable or the local network, then delete them from
   the device — including any automatic cloud copy.
6. **Pick a main room** to record most of this in — somewhere you can also speak
   from further away or from an adjoining room, and where at least two of the
   following are genuinely available and can actually be switched on: a
   television, a kitchen with running appliances, a window facing a street, or a
   fan.
7. **Use your phone's own voice recorder app** (Voice Memos, the built-in Android
   recorder, or similar), or whatever device your coordinator asks you to use.
   Whatever it produces — `.m4a`, `.wav`, whatever your app writes by default —
   is fine and should be left exactly as it is.

## The one rule behind everything

**One condition, one genuine recording.** If a section below asks for "quiet",
record yourself actually speaking quietly — never take a normal recording and
turn the volume down in an app. If a section is missing, go back and record it
for real; never generate it by editing another file. This applies to every
section, without exception, because a processed file teaches the model the wrong
thing: it would learn what your phone's software does to audio, not what a
person quietly saying "hey youtab" actually sounds like.

**Nothing is trimmed, converted, normalised, denoised, deleted, or re-recorded
to replace a take you didn't like.** Leave your recorder's raw output exactly as
it comes out — including the takes where you stumbled, coughed, or think you
sounded odd. Difficult, natural speech is the entire point of this round;
deleting it back out would undo the reason you were asked to record it. Whether
a particular recording is usable is decided later, by fixed rules applied the
same way to everyone's recordings — never by a speaker's own judgement of their
own take, and never by editing it into something you judge better.

## How to say it

Read these before starting. They are the difference between a usable session and
one that measures the wrong thing.

* **Say the name as one unit: "youtab", not "you… tab".** A small pause after
  *hey* is fine — the model is trained on that spelling. A pause *inside the
  name* is not. It splits the wake phrase into two words the detector has never
  been trained on, and the trailing 1.28 s the model actually looks at may not
  even contain the whole phrase any more. **This is the single most important
  instruction in this document.** In the E002 session, `hey you tab` was spoken
  with a pause between "you" and "tab" in every take, which fragmented all of
  them and left the hardest confusion in the whole inventory — `hey you tab`
  against `hey you tap` — essentially untested. If you hear yourself pause,
  record the phrase again as a new take; do not delete the one you paused in.
* **Do not perform it.** Say it the way you would say it to a device across the
  room while doing something else. Over-enunciated wake words are the ones that
  work in the lab and fail in the kitchen.
* **Vary naturally between takes.** Slightly faster, slightly quieter, mid-yawn,
  mid-sentence. Identical takes measure one utterance many times.
* **Leave about a second of silence around each take.** The detector scores a
  2-second window; a take clipped at the start is a take that was never
  recorded.
* **One take per file**, except in sections 5 and 6, which are one long
  recording each.

### Pronunciation notes, for any first language

The phrase itself never changes — it is always **"Hey Youtab,"** however you
say it. These notes exist only to show what sound is being aimed for if
English is not your first language; they are not a correction, and your own
accent is exactly what this round needs (see "accent and first language" in
the coverage table in Part B) — do not try to imitate a reference recording or
flatten out how you naturally speak.

Two parts: "hey" (rhymes with "day"), then "you" and "tab" run together as one
word, stressed on "you" — "YOO-tab", not "you TAB". `phrases.py`, the
product's own pronunciation contract, records what a speech synthesizer
actually produces for it: `hˈeɪ jˈuːɾæb` when said as one word (a soft,
flapped "t", the way most people say it) and `hˈeɪ jˈuː tˈæb` when the name is
said in two parts with a hard "t" — both are genuine ways of saying it, which
is why rows 1 and 3 of the near-phrase battery below ask for both.

A rough guide for a few first-language backgrounds — say it naturally; this is
not a target to hit exactly:

| First language | A rough guide |
|---|---|
| Persian / Farsi (فارسی) | «های یوتَب» — «های» then «یوتب» run together, stress on «یو». |
| Spanish | "jei yútab" — the "h" as in "jamón", then "yu-tab" as one word, stress on "yu". |
| Mandarin | 嘿有tab ("hēi yóu-tab") — "hey", then "you" and "tab" run together, no gap. |
| Hindi / Urdu | "हे यूटैब" (hey yoo-taib) — "yoo" and "tab" said as one connected word. |
| Arabic | «هاي يوتاب» — "hey", then "yoo-tab" run together as one connected word. |
| French | "hé you-tab" — breathe out on the "h" (English "h" is aspirated; French "h" is silent); keep "you-tab" as one word. |

Whatever comes out naturally is the point of this round: see "Why this round
exists" above for why real accented speech, not a corrected version of it, is
what training and evaluation are missing.

## What you're recording — six sections

| # | Section | Covers |
|---|---|---|
| 1 | The wake phrase, five ways | natural close-mic positives; normal, slow, fast, quiet, loud delivery |
| 2 | Far-field | genuinely standing further away, in a normal and then a raised voice |
| 3 | Real background noise | the wake phrase with real TV/kitchen/street/fan noise actually running |
| 4 | The near-phrase battery | phrases that sound close to "hey youtab" but are not, plus the phrase itself said in its trickier forms |
| 5 | Free-speech negatives | ordinary talking, no wake phrase |
| 6 | Background-only | your room, recorded, with nobody speaking |

Record them in this order; it matches the folders below and keeps the
device/room setup for each section together.

### Sections 1 to 3 — the wake phrase

Say **"hey youtab"** naturally — the way you'd actually get the assistant's
attention, not slowly enunciated like a dictionary entry. Record each take as
its own separate recording: start, say the phrase once, stop.

| Folder | Filename | Takes | How |
|---|---|---|---|
| `positive_normal/` | `hey-youtab_normal_NNN.ext` | 5 | Your ordinary speaking voice and pace. |
| `positive_slow/` | `hey-youtab_slow_NNN.ext` | 5 | Noticeably slower than normal, but still one natural phrase. |
| `positive_fast/` | `hey-youtab_fast_NNN.ext` | 5 | Noticeably faster, as if in a hurry. |
| `positive_quiet/` | `hey-youtab_quiet_NNN.ext` | 5 | Genuinely quiet — as if not to wake someone; not a whisper if that feels unnatural to you. |
| `positive_loud/` | `hey-youtab_loud_NNN.ext` | 5 | Genuinely loud — as if calling across a room, not shouted so hard it distorts. |
| `positive_farfield/` | `hey-youtab_farfield_NNN.ext` | 5 | At least 5 metres away (an adjoining room with the door open works), in your normal voice and pace. |
| `positive_farfield_loud/` | `hey-youtab_farfield-loud_NNN.ext` | 5 | From the same distance, raised as you naturally would to be heard across that space. A raised voice changes vowel quality and not only level, so it is recorded separately — otherwise a miss cannot be attributed to the distance or to the delivery. |

The first five are close to your phone; arm's length is fine. For the two
far-field folders, record how far you actually stood in the `farfield_distance`
field of your metadata form — if your home has no room that large, an adjoining
room with the door open is the intended setup, and 2–3 m recorded honestly is
better than 5 m claimed.

**Section 3, real background noise.** Pick **at least two** of: a television, a
kitchen with an appliance or tap running, an open window facing a street, a fan.
Turn one on for real, let it settle for a few seconds, then say "hey youtab"
close to your phone (as in the `normal` condition) **5 times** with it genuinely
running. Repeat for your second source. If you use more than two, that's welcome
— just give each its own folder, `positive_noise_<source>/`, with `<source>` one
of `tv`, `kitchen`, `street`, `fan`. Record which ones you used in the
`noise_sources_used` field of your metadata form; it must name at least two, and
they must match the folders you actually create.

### Section 4 — the near-phrase battery

This section is where the model's mistakes live. Four of its rows fix specific
gaps in the earlier speaker's session: "okay youtab" — the real name under a
different lead-in word — was never recorded; "hey google" and "hey siri" were
never recorded; bare "hey" on its own was never recorded as its own item; and
"hey you tab" was recorded with a pause in the middle that broke the name apart.

Say each phrase **naturally, as one continuous sentence** — no artificial pause
anywhere in it, and in particular, **no pause inside the name**.
"hey&nbsp;youtab", however it's spelled below, is one word to say, not two. A
pause after "hey" is fine and is how people actually say it; a pause between
"you" and "tab" is not.

Two of the phrases below — marked **Wake word** — are genuine ways of saying
"hey youtab" itself, so they *should* trigger the assistant. The rest are marked
**Should NOT wake it**: say them exactly as an ordinary sentence, the way you'd
actually say them, not as a trap or a trick.

**Record rows 1 and 2 back to back, on the same device, without switching
anything in between.** They are a matched pair — the same phrase with one sound
changed — and being recorded together is what makes them useful as a pair.

Say each phrase the number of times in its `Takes` column, close to your phone
as in the `normal` condition — **108 takes** in total for this section. Rows 23
to 34 are single words; that's correct, not a mistake in the table — say the one
word, as you normally would.

| # | Phrase | Must it wake Youtab? | Takes | File slug | How to say it |
|---|---|---|---|---|---|
| 1 | "hey you tab" | **Wake word** | 5 | `hey-you-tab` | Say it as one continuous name: no pause between "you" and "tab". A pause here is the exact defect that made this phrase nearly untestable from an earlier speaker. |
| 2 | "hey you tap" | Should NOT wake it | 5 | `hey-you-tap` | Record it immediately after "hey you tab." on the same device, without changing anything in between, so the two form a clean minimal pair. |
| 3 | "hey yoo tab" | **Wake word** | 5 | `hey-yoo-tab` | Same rule: one continuous name, no pause inside it. |
| 4 | "okay youtab" | Should NOT wake it | 3 | `okay-youtab` |  |
| 5 | "okay tab" | Should NOT wake it | 3 | `okay-tab` |  |
| 6 | "hey google" | Should NOT wake it | 3 | `hey-google` |  |
| 7 | "hey siri" | Should NOT wake it | 3 | `hey-siri` |  |
| 8 | "hey" | Should NOT wake it | 3 | `hey` | Say it alone, as if starting to say something and not finishing. |
| 9 | "hey your tab" | Should NOT wake it | 3 | `hey-your-tab` |  |
| 10 | "hey new tab" | Should NOT wake it | 3 | `hey-new-tab` |  |
| 11 | "hey utah" | Should NOT wake it | 3 | `hey-utah` |  |
| 12 | "hey do tab" | Should NOT wake it | 3 | `hey-do-tab` |  |
| 13 | "hey stab" | Should NOT wake it | 3 | `hey-stab` |  |
| 14 | "youtab" | Should NOT wake it | 3 | `youtab` |  |
| 15 | "hey there" | Should NOT wake it | 3 | `hey-there` |  |
| 16 | "youtab is running" | Should NOT wake it | 3 | `youtab-is-running` |  |
| 17 | "hey you had" | Should NOT wake it | 3 | `hey-you-had` |  |
| 18 | "hey cab" | Should NOT wake it | 3 | `hey-cab` |  |
| 19 | "hey you talk" | Should NOT wake it | 3 | `hey-you-talk` |  |
| 20 | "a new tab" | Should NOT wake it | 3 | `a-new-tab` |  |
| 21 | "hey youtube" | Should NOT wake it | 3 | `hey-youtube` |  |
| 22 | "hey you" | Should NOT wake it | 3 | `hey-you` |  |
| 23 | "eight" | Should NOT wake it | 3 | `eight` |  |
| 24 | "two" | Should NOT wake it | 3 | `two` |  |
| 25 | "visual" | Should NOT wake it | 3 | `visual` |  |
| 26 | "four" | Should NOT wake it | 3 | `four` |  |
| 27 | "zero" | Should NOT wake it | 3 | `zero` |  |
| 28 | "house" | Should NOT wake it | 3 | `house` |  |
| 29 | "down" | Should NOT wake it | 3 | `down` |  |
| 30 | "happy" | Should NOT wake it | 3 | `happy` |  |
| 31 | "stop" | Should NOT wake it | 3 | `stop` |  |
| 32 | "yes" | Should NOT wake it | 3 | `yes` |  |
| 33 | "left" | Should NOT wake it | 3 | `left` |  |
| 34 | "no" | Should NOT wake it | 3 | `no` |  |

Every row of this table has a reason, and the reason is written down per row in
Part B — including which `phrases.py` list decides whether it should wake the
assistant. Nothing in it is filler.

### Section 5 — free-speech negatives

Talk naturally for about **5 minutes**, about anything — your day, a hobby,
reading something aloud — as one continuous recording. Just don't say "hey
youtab" or any phrase from the table above. This does not need to be interesting
or planned; ordinary rambling is exactly what's wanted. Talking about ordinary
computer things — browser tabs, windows, files — is welcome rather than avoided:
that is what an agent's user actually says out loud near a microphone.

### Section 6 — background-only

Record about **3 minutes** of your room with nobody speaking — leave the
recorder running and step back or stay quiet. This is not silence in a technical
sense; whatever your room actually sounds like (a fridge, traffic outside,
nothing at all) is the point.

## Time estimate

| Section | What | Time |
|---|---|---|
| Read-through and setup |  | 5 min |
| Consent form |  | 5 min |
| Device and environment form |  | 5 min |
| 1. Wake phrase, five deliveries | 25 takes | 7 min |
| 2a. Far-field, normal voice | 5 takes | 4 min |
| 2b. Far-field, raised voice | 5 takes | 2 min |
| 3. Real background noise | 10 takes | 6 min |
| 4. Near-phrase battery | 108 takes | 22 min |
| 5. Free-speech negative | 1 continuous take, ~5 min | 6 min |
| 6. Background-only | 1 continuous take, ~3 min | 4 min |
| Self-check and handoff prep |  | 5 min |
| **Total** | **155 audio files** | **≈ 71 min** |

**Minimum vs expected.** The rows numbered 1–6 above — the recording itself,
every required take performed once — add up to **≈ 51 min minimum**. The other
20 minutes (read-through, the consent form, the device form, and the
self-check before handoff) is paperwork around the recording, not slack inside
it: budget **≈ 71 min expected** for the whole sitting, and treat the two
numbers as the same session rather than two different ones.

## Folder and file naming

Create one folder named with your speaker label — the one your coordinator gave
you, never your name — and put everything inside it. **Names matter exactly as
written**: our loader maps a file to what it contains by its name and folder
alone, and an unrecognised name is treated as an error, not a guess. Keep
whatever file extension your recorder produces (`.m4a`, `.wav`, `.caf` —
whatever it is); never convert it.

```
E003/                                        one folder per speaker, named for the label alone
  originals/                                 everything you record, exactly as your recorder wrote it
    positive_normal/
      hey-youtab_normal_001.m4a              ... through _005
    positive_slow/
      hey-youtab_slow_001.m4a                ... through _005
    positive_fast/
      hey-youtab_fast_001.m4a                ... through _005
    positive_quiet/
      hey-youtab_quiet_001.m4a               ... through _005
    positive_loud/
      hey-youtab_loud_001.m4a                ... through _005
    positive_farfield/
      hey-youtab_farfield_001.m4a            ... through _005
    positive_farfield_loud/
      hey-youtab_farfield-loud_001.m4a       ... through _005
    positive_noise_tv/                       one folder per noise source you used
      hey-youtab_noise-tv_001.m4a            ... through _005    (source: tv, kitchen, street or fan)
    positive_noise_kitchen/
      hey-youtab_noise-kitchen_001.m4a       ... through _005
    near_phrase/
      hey-you-tab_001.m4a                    ... through _005
      hey-you-tap_001.m4a                    ... through _005
      ...one sub-series per row of the Section 4 table, using its File slug...
      no_001.m4a                             ... through _003
    negative_freespeech/
      freespeech_001.m4a                     one continuous file (a few, if your app splits long recordings)
    background_only/
      background_001.m4a
  RECORDING_METADATA.json                    your filled-in device/environment form
  CONSENT.pdf                                your signed consent form — your coordinator puts this here
  SHA256SUMS                                 checksum listing — your coordinator generates this
```

The pattern, spelled out:

| Folder | Filename |
|---|---|
| `originals/positive_<condition>/` | `hey-youtab_<condition>_NNN.ext` |
| `originals/positive_noise_<source>/` | `hey-youtab_noise-<source>_NNN.ext` |
| `originals/near_phrase/` | `<phrase-slug>_NNN.ext` |
| `originals/negative_freespeech/` | `freespeech_NNN.ext` |
| `originals/background_only/` | `background_NNN.ext` |

`NNN` is a three-digit take number starting at `001`. If your recorder app names
files its own way ("Voice Memo 3", "recording_2026-08-17_1"), rename each file to
match the pattern above before handing your folder over — an unrenamed file is
exactly the kind of unrecognised file the loader errors on rather than guessing
about.

**You do not have to type those names by hand, and 155 of them is a lot of
chances for a typo.** Leave every file exactly as your recorder named it —
don't reorder or delete anything inside a section's folder — and there is an
automatic renamer: it sorts each section's files by the order they were
recorded in (oldest first) and assigns the exact name above, without ever
opening or reading the audio itself.

```bash
python scripts/wakeword/validate_speaker_submission.py --rename plan  <capture-root>/E003
python scripts/wakeword/validate_speaker_submission.py --rename apply <capture-root>/E003
```

`--rename plan` prints what it would rename each file to, in recording order,
without touching a single file — read it over first. `--rename apply` does the
rename. If a folder's file count doesn't match what that section requires
(one extra take, one missing one), it reports that folder and leaves it
untouched rather than guessing which file is which — go back and fix the count,
then run it again. Either you or your coordinator can run this; if typing a
command isn't something you want to do, hand your folder over exactly as your
recorder wrote it and ask your coordinator to run it for you.

**You produce `originals/` and `RECORDING_METADATA.json`.** `CONSENT.pdf` and
`SHA256SUMS` are added by your coordinator when they receive the folder; you do
not need any checksum tool. The four together are the whole submission, and
there is no fifth entry: `validate_speaker_submission.py` refuses anything it
does not recognise rather than skipping it.

## Handing it over

- Your recordings **never leave your own machine except to your coordinator**,
  by whatever private method they tell you to use. They are never committed to
  git, never pushed to a code repository, and never attached to a chat, ticket
  or email thread that isn't your coordinator directly.
- **Do not use a personal cloud-sync folder or email to send the audio.** A USB
  drive, a direct cable transfer, or a private non-syncing link your coordinator
  provides.
- Hand your **signed consent form separately** from the audio, by the method
  described in `CONSENT_RECORD_TEMPLATE.md` — not in the same transfer. Your
  coordinator is the one who files it into the submission folder as
  `CONSENT.pdf`, in a store that is encrypted and access-controlled; keeping the
  two apart in transit and bound together in the governed archive is deliberate,
  and the consent form explains why.
- After the transfer is confirmed, delete the recordings from the recording
  device, and confirm the cloud copy is gone as well as the local one.

## Consent, in brief

Full workflow, retention and withdrawal details are in
`CONSENT_RECORD_TEMPLATE.md`. In short: you sign before you record, you keep a
copy, your coordinator holds the signed original outside any code repository,
you can withdraw at any time by telling your coordinator, and withdrawal deletes
your recordings within 7 days but — importantly — cannot undo a model that has
already been trained using them before you withdrew. Read the full form; it
explains this properly.

## Before you hand it over: quality self-check

This checklist is about **completeness**, not about judging your own
performance. Nothing on it asks you to delete, redo, or "clean up" a recording —
see "The one rule behind everything" above for why.

- [ ] Every folder listed above exists and has files in it. If a whole section is
      missing, go back and record it — don't skip it.
- [ ] File names match the pattern shown, including for any file your app
      auto-named — check a few by eye.
- [ ] No file mixes two conditions (e.g. you didn't say a "normal" take and a
      "slow" take in the same recording).
- [ ] Rows 1 and 2 of the Section 4 table ("hey you tab" / "hey you tap") were
      recorded back to back, on the same device.
- [ ] The Section 5 and 6 recordings are each one continuous, unedited take of
      roughly the target length — check your recorder's own timer, don't guess.
- [ ] Playback check: listen to two or three files at random and confirm the
      words are audible — even a quiet or fast take should still be recognisable
      as words, just delivered that way.
- [ ] If your recorder shows a level meter and it pinned at maximum during the
      `loud` takes, that's fine — leave the recording as it is, and mention it in
      the `notes` field of your metadata form rather than re-recording.
- [ ] `RECORDING_METADATA.json` is filled in completely, with no bracketed
      `<...>` placeholder text left in it.
- [ ] The consent form is signed, you have your copy, and it has gone to your
      coordinator separately from the audio.
- [ ] If you're comfortable running a command, run the upload-verification
      check yourself before handing anything over — it lists exactly what's
      missing and how much of each section was accepted:
      `python scripts/wakeword/validate_speaker_submission.py <your-folder>/E003`.
      If not, your coordinator runs it with you before you finish.

A stumble, a cough mid-sentence, a take you think sounded bad — leave it in. It
is frequently the most useful recording in the folder: difficult, natural speech
is exactly what earlier, computer-generated training data could not produce. Any
exclusions happen later, on our side, using rules fixed in advance and applied
the same way to everyone — never based on how a speaker felt about their own
take.

---

# Part B — for the coordinator

## The phrase taxonomy, and where every label comes from

`scripts/wakeword/phrases.py` is the product's wake-phrase contract: one module,
imported by every stage, so training and evaluation can never drift apart on
what counts as the wake word and what counts as a near miss. This package does
not get a second opinion about it. Every row of the Section 4 battery carries
the `phrases.py` tuple that decides its label, and
`tests/tools/test_wakeword_speaker_recording_spec.py` checks each claim against
the real tuple rather than against a hand-copied list.

Three things follow that are worth stating before the table, because getting any
of them wrong has already cost a session.

**`hey you tab.` is a POSITIVE, not a near miss.** It is in
`phrases.POSITIVE_SPELLINGS` with weight 2, alongside `hey youtab.` (5),
`hey yoo tab.` (2), `hey, youtab.` (1) and `hey youtab!` (1). espeak-ng flaps
the /t/ in the compound spelling (`hˈeɪ jˈuːɾæb`) and keeps a hard /t/ when the
name is split (`hˈeɪ jˈuː tˈæb`); both are things people actually say, so the
model is trained on the mixture. This has already caused one mislabelling
incident, so it is worth being explicit about the cost of getting it wrong:
`phrases.py` says in its own docstring that labelling an utterance which
includes the wake phrase as a negative "would teach the model to suppress a real
fire". Recording `hey you tab` as if it were a near miss does exactly that — and
recording it *with a pause inside the name* achieves the same thing more quietly,
by collecting a positive of a phrase the detector was never trained on.

**Bare `hey.` is already decided.** It is in `phrases.HARD_NEGATIVES`, in the
"carrier word without the name" block next to `hey there.` and `hey, hold on.`.
Its negative label is read off the contract and is not a judgement call; what
was missing was a *recording* of it, not a decision about it. Keeping those two
facts apart is the whole point of the "recorded from E002?" column below.

**Three phrases were decided beside the contract, and are in it now.** `okay
youtab.`, `hey google.` and `hey siri.` were in no list in `phrases.py` when this
package was written. A phrase that is absent from the contract and unlabelled in
the package is a phrase whose label gets invented by whoever ingests it, so each
one carried a decision, a basis and a consequence in
`speaker_recording_spec.TAXONOMY_DECISIONS`. An Owner decision has since made
those readings binding: **all three are in `phrases.HARD_NEGATIVES`**,
`TAXONOMY_DECISIONS` is empty, and the `basis in phrases.py` column below names
the tuple for them exactly as it does for every other negative row. Nothing else
about them changed — same three takes each, same position in the recording order,
same time budget.

Their basis is still written down here, because it is now the reason those
entries are in the contract.

**`okay youtab.` → negative (must not fire).** `phrases.HARD_NEGATIVES` already
contained the real name under carriers other than "hey" — `open youtab.`,
`youtab.`, `youtab is running.` and `okay tab.` were all in it — and every one of
the five `phrases.POSITIVE_SPELLINGS` entries starts with the carrier "hey". The
carrier is part of the trained phrase, not decoration around it:
`tools/wake_word.py` scores frames against one trained model and its own
docstring calls the configured `wake_word.phrase` "purely cosmetic; engine keys
detection", so what the product fires on is whatever `POSITIVE_SPELLINGS` trained
it on and nothing else. Adding it does not violate the `HARD_NEGATIVES` rule that
"nothing in this list *contains* the wake phrase" — it contains the name, not the
carrier-plus-name phrase, which is why `youtab.` and `open youtab.` were always
allowed to be in it. It is the row that separates a model keyed on the whole
phrase from one keyed on "youtab" alone, which is why `okay tab.` — the same
carrier with the name removed — is recorded next to it as the control.

**`hey google.` → negative (must not fire).** A competing assistant's wake word.
`phrases.HARD_NEGATIVES` is built from the carrier plus a wrong name
(`hey utah.`, `hey yoda.`, `hey nutmeg.`, `hey youtube.`) and from the carrier
alone (`hey.`, `hey there.`); this is the same construction with the best-known
wrong name in it. Firing here would wake Youtab while its owner is talking to a
different device, and it is the phrase an always-on microphone in a
mixed-assistant household hears most often.

**`hey siri.` → negative (must not fire).** Same class as `hey google.`: the
carrier plus a competing assistant's name. `tools/wake_word.py`'s own module
docstring names the pattern — "the 'Hey Siri' / 'Alexa' pattern" — so the
product's framing of these as other products' wake words is already explicit.
Phonetically it adds a second carrier-plus-name shape with a different stressed
vowel, so it is not a duplicate of `hey google.`.

**None of the three is in `phrases.CONFUSABLE_NEGATIVES`, deliberately.** That
list is the *measured* confusable subset, ordered by how often each phrase
actually fired on held-out audio, and it is what synthesis weighted higher.
Synthetic generation is retired, so an entry would change no training today —
but none of these three has ever been measured, by synthesis or by recording, so
an entry would be intuition dressed as evidence. That is the one thing the list
is defined not to be: `hey youtube.` was the confusion this inventory was
designed around and it ranked near the bottom when someone finally measured it.
This recording round is what can earn them a place.

### The battery, row by row

`takes` is per speaker. The `basis in phrases.py` column is the tuple that
decides the label — checked against the real module, so it cannot drift into
being decorative.

The `recorded from E002?` column says whether a real recording of that phrase
exists from the one human speaker recorded before this round. `no` means the
session record states it was never recorded. `fragmented` means it was recorded
and every take was unusable for the reason above, so the phrase is unmeasured in
practice. `undocumented` means that session's record does not say, per phrase,
and this package therefore treats it as unmeasured rather than claiming coverage
nobody wrote down. Every row is re-recorded from every speaker regardless; the
column exists so that "we have that already" is never asserted from memory.

| # | phrase | label | takes | basis in `phrases.py` | recorded from E002? | why it is in the list |
|---|---|---|---|---|---|---|
| 1 | `hey you tab` | positive | 5 | `POSITIVE_SPELLINGS` | fragmented | This IS the wake word — the unstressed-carrier spelling, weighted 2 in phrases.POSITIVE_SPELLINGS, and half of the hardest minimal pair in the inventory. It is also the one phrase an earlier speaker lost: every take was spoken with a pause between "you" and "tab". |
| 2 | `hey you tap` | negative | 5 | `HARD_NEGATIVES` | undocumented | The measured top confusion for the phrase above — one voicing feature away, and it fired on the first trained model's held-out clips more than any other near miss. With the row above fragmented, the hardest confusion in the whole inventory is currently untested in both directions. |
| 3 | `hey yoo tab` | positive | 5 | `POSITIVE_SPELLINGS` | fragmented | Also the wake word — the hard-/t/ spelling, weighted 2 in phrases.POSITIVE_SPELLINGS. espeak-ng flaps the /t/ in the compound spelling and keeps it hard when the name is split; both are things people say, and this is the one that tests the split form. |
| 4 | `okay youtab` | negative | 3 | `HARD_NEGATIVES` | no | The only near phrase that carries the real name under a different carrier word, so it is the row that distinguishes a model keyed on the whole phrase from one keyed on "youtab" alone. People say "okay X" out of habit from other assistants, so a model that fires here fires often in the field. Never recorded before this round; phrases.HARD_NEGATIVES decides that it is a negative. |
| 5 | `okay tab` | negative | 3 | `HARD_NEGATIVES` | undocumented | The other carrier with the bare noun, and the control for the row above: "okay youtab." minus the name. Recorded next to it so a fire can be attributed to the carrier or to the name rather than to the pair of them. |
| 6 | `hey google` | negative | 3 | `HARD_NEGATIVES` | no | A competing wake word with the same carrier and a stressed vowel in the next syllable. Anyone with another assistant in the house says this near the microphone all day. Never recorded before this round; phrases.HARD_NEGATIVES decides that it is a negative. |
| 7 | `hey siri` | negative | 3 | `HARD_NEGATIVES` | no | Same class as "hey google.", second carrier-plus-name shape, different stressed vowel. Never recorded before this round; phrases.HARD_NEGATIVES decides that it is a negative. |
| 8 | `hey` | negative | 3 | `HARD_NEGATIVES` | no | The carrier word alone, and the single most common thing an always-on microphone hears that begins like the wake word. It is in phrases.HARD_NEGATIVES already, so its label is not a judgement call — synthesized thousands of times, and never once recorded from a person. |
| 9 | `hey your tab` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured as one of the top confusions of the first trained model: the frame "hey <something> tab", which is what actually fires. |
| 10 | `hey new tab` | negative | 3 | `HARD_NEGATIVES` | undocumented | The same measured frame with an ordinary word in it — and something a browser user says out loud. |
| 11 | `hey utah` | negative | 3 | `HARD_NEGATIVES` | undocumented | Two syllables, same stress pattern, no /b/. Measured in the confusable set. |
| 12 | `hey do tab` | negative | 3 | `HARD_NEGATIVES` | undocumented | The same frame with nonsense filler, which separates the frame itself from the words that happen to fill it. |
| 13 | `hey stab` | negative | 3 | `HARD_NEGATIVES` | undocumented | Rhymes with the final syllable and has no /juː/ at all. |
| 14 | `youtab` | negative | 3 | `HARD_NEGATIVES` | undocumented | The name with no carrier. Must not fire, or the product wakes every time someone says its name. |
| 15 | `hey there` | negative | 3 | `HARD_NEGATIVES` | undocumented | The carrier plus the commonest thing that follows it in real speech. |
| 16 | `youtab is running` | negative | 3 | `HARD_NEGATIVES` | undocumented | The name inside an ordinary sentence — what a user of this product says while talking *about* it. |
| 17 | `hey you had` | negative | 3 | `HARD_NEGATIVES` | undocumented | Same onset and stress, different coda. Measured in the confusable set. |
| 18 | `hey cab` | negative | 3 | `HARD_NEGATIVES` | undocumented | The final syllable of the name with a different onset before it. |
| 19 | `hey you talk` | negative | 3 | `HARD_NEGATIVES` | undocumented | Same onset, different coda, and one of the measured confusions. |
| 20 | `a new tab` | negative | 3 | `HARD_NEGATIVES` | undocumented | The frame with no carrier at all, and ordinary speech about tabs, which is what an agent's user actually talks about. |
| 21 | `hey youtube` | negative | 3 | `HARD_NEGATIVES` | undocumented | The obvious confusion, and the one this phrase inventory was originally designed around. Measured as *not* the worst one — it fired on 1 held-out clip in 106 — which is exactly why it is worth recording: it is the row that shows the ranking was measured rather than guessed. |
| 22 | `hey you` | negative | 3 | `HARD_NEGATIVES` | undocumented | The prefix, stopped before the name. Bounds how much of the phrase the model needs before it commits. |
| 23 | `eight` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured, not guessed: /eɪ/ with no /h/ onset scored 0.9985 against an operating threshold of 0.9991 and pinned that threshold by itself, which is what cost half the wake words. |
| 24 | `two` | negative | 3 | `HARD_NEGATIVES` | undocumented | Second-highest scorer in the same measured tail (0.9832). |
| 25 | `visual` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured in the same tail (0.8568); the /ʒu/ is the closest thing to /juː/ in that word list. |
| 26 | `four` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured in the same tail (0.6853). |
| 27 | `zero` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured in the same tail (0.5508). |
| 28 | `house` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured in the same tail (0.5412). |
| 29 | `down` | negative | 3 | `HARD_NEGATIVES` | undocumented | Round-3 evidence: 0.999997 on held-out recorded speech, the highest scoring negative of that round. |
| 30 | `happy` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured: an /h/ onset that is not "hey" took over the tail once the model learned to require the onset (0.999995). |
| 31 | `stop` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured alongside "happy" in the round-3 tail (0.992592). |
| 32 | `yes` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured in the round-3 tail (0.987031). |
| 33 | `left` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured in the round-3 tail (0.966623). |
| 34 | `no` | negative | 3 | `HARD_NEGATIVES` | undocumented | Measured in the round-3 tail (0.935737). |

Twelve rows of that table (23 to 34) are the Speech Commands words that were
*measured* at the top of the false-accept tail in earlier rounds, in the order
they scored. They are single words rather than phrases on purpose: what fires is
the vowel and the onset, not the sentence around them.

## Coverage, and why each dimension is there

Every dimension below changes the *signal*, not the words. A model that works on
one laptop, at one distance, in one quiet room, has been measured on one cell of
this table. Cover the cells **across the group** rather than within each
speaker: every speaker does close and far, and the devices and noise conditions
are spread across people by the coordinator. What each speaker actually used is
recorded in their `RECORDING_METADATA.json`, which is the only place that
information survives — a measurement without it cannot be attributed to a
condition.

| dimension | levels to cover | why it matters |
|---|---|---|
| device | phone (handheld), laptop (built-in microphone), wired headset | Three different frequency responses and three different distances-to-mouth. A headset boom is 3 cm from the lips and has almost no room in it; a laptop array is 50 cm away, applies its own beamforming and noise suppression, and is what most desktop users actually have. |
| distance | close (~30 cm), room (2–3 m), far (5 m or an adjoining room) | Distance is not just level. At 2–3 m the direct-to-reverberant ratio collapses and the room's own response is a large part of what the microphone hears — which is exactly what the synthetic impulse responses in training are approximating, and nobody has checked that approximation against a real room. |
| accent and first language | at least two different first languages; at least three English varieties among the speakers | The model's accent coverage comes from phonemizing through twelve espeak-ng voices, which changes the phoneme string and not the acoustics. A real second-language speaker differs in rhythm, vowel space and consonant release all at once, and none of that is in the training distribution. |
| pitch range | at least one low (≈85–120 Hz), one mid, one high (≈200–255 Hz) fundamental | The front end is a mel spectrogram, so fundamental frequency moves where the harmonics land under every filter. The synthetic voice pool is wide but it is a pool of *interpolated* speaker embeddings, which tends to fill the middle and thin out the extremes. |
| noise condition | quiet room, background speech or television, kitchen or street noise | An always-on microphone spends its whole life in the last two. Background *speech* matters most: it is the condition where a false activation is most likely and the one the current negatives — single words and read sentences — represent least. |

## Why these counts, not round numbers

The shipping targets for this model include a false-accept rate on deliberate
near misses of at most 2%, a missed-wake-word rate of at most 5%, and at most
0.2 activations per hour on ordinary recorded speech (see `README.md`'s target
table). The rule of three says that observing zero failures across *n* trials
bounds the true rate at roughly 3/*n* with about 95% confidence. Inverted, that
is what a clean run has to cover to *certify* a target:

| target | trials a clean run needs | the two sealed speakers, pooled |
|---|---|---|
| missed wake words ≤ 5% | 60 positives | 55 × 2 = **110** |
| false accepts on near misses ≤ 2% | 150 near-phrase utterances | 98 × 2 = **196** |

That is the arithmetic the sealed half of this round is sized by, and it is
checked in `tests/tools/test_wakeword_speaker_recording_spec.py` so the counts
and the claim cannot drift apart. The third target — 0.2 activations per hour —
is *not* sized here: certifying it needs about 15 hours of continuous talking,
which is `evaluate_model.py`'s job at the pooled, many-speaker scale, not one
volunteer's sitting. Five minutes of free speech per speaker will not measure a
rate, but it will catch a model that fires on nothing in particular.

Per speaker, the counts are sized for **redundancy against a single bad take**:

* **Three** repetitions of a near phrase is the floor that survives one
  integrity exclusion (a cough, a dropped phone) and still leaves two genuine
  examples — two would leave only one, and one is a single point that cannot be
  told apart from a fluke. Certifying the 2% target from a single phrase would
  need about 150 takes of that one phrase, which no real speaker should be asked
  to say that many times identically; repeated 150 times, it stops being the
  natural speech this round exists to capture. The battery trades depth for
  breadth deliberately: thirty-four distinct phrases matter more here than extra
  takes of any one of them.
* **Five** repetitions for the delivery, far-field and noise conditions, because
  those are the three conditions the follow-up experiment measured broken
  (36.7%, 53.3%, 40.0% against 15/15 close-mic); five lets two exclusions happen
  and still leaves a clear majority reading rather than a coin flip.
* **Five** repetitions for the split-name triple — rows 1 to 3 — rather than
  three. Those are the rows with no usable prior evidence at all, since every
  earlier take of them was fragmented, so they start from zero rather than from
  three.

A single speaker's 55 positives with no misses only bounds *that speaker's*
false-reject rate at about 5.5%, so no one session is enough on its own. The
claim is made across speakers; the per-speaker numbers are for spotting the one
voice the model hates.

## Assembling a submission

1. Copy the speaker's handoff into one folder per speaker under the capture
   root, named for the label alone: `E003/`, `E004/`.
   **Never inside this repository** — the manifest tool refuses a destination
   under the checkout, and the commit gate refuses the audio.
2. Auto-name anything the recorder left in its own naming scheme, before
   generating checksums against it:

   ```bash
   python scripts/wakeword/validate_speaker_submission.py --rename plan  <capture-root>/E003
   python scripts/wakeword/validate_speaker_submission.py --rename apply <capture-root>/E003
   ```

   `plan` prints what would change, in the order each file was recorded
   (oldest first), without touching anything; `apply` performs it. A folder
   whose file count doesn't match what that section requires is reported and
   left alone rather than guessed at — resolve it with the speaker, then
   re-run. Skip this step for a folder the speaker already named by hand.
3. File the signed consent record as `E003/CONSENT.pdf`, in the encrypted,
   access-controlled store described in `CONSENT_RECORD_TEMPLATE.md`. The
   speaker keeps their own copy; the coordinator's copy is what binds the
   recordings to a consent that covers them, which is why it lives in the
   submission folder rather than beside it.
4. Generate `E003/SHA256SUMS` — coreutils format, `<digest>  <path>`, paths
   relative to the speaker folder, covering every other file in it. Verify it
   with `sha256sum -c SHA256SUMS` from inside the folder after any later move;
   that is the check the validator deliberately does not do, because a digest
   recomputed on the machine that wrote it says nothing about the transfer.
5. Validate the layout before anything reads the audio:

   ```bash
   python scripts/wakeword/validate_speaker_submission.py <capture-root>/E003
   ```

   It reports every problem in one pass and exits non-zero on any of them, and
   prints a completion summary — one line per section, how many takes were
   accepted against how many are required — so a shortfall is a number to go
   back for, not a sentence to puzzle over. Run it while the speaker is still
   reachable: a missing section can be recorded, and a misnamed file can be
   asked about, only until they are not.
6. Freeze it, before anything reads it:

   ```bash
   python scripts/wakeword/freeze_manifest.py freeze \
       --root <capture-root>/<label>/originals \
       --out  <capture-root>/<label>.manifest.json \
       --dataset <label> --split train --usage training \
       --note "phone, close+5m, quiet+TV+kitchen, en-GB, mid pitch"
   ```

   `--split`/`--usage` come from that speaker's row in
   `speaker_recording_spec.SPEAKER_ASSIGNMENTS`, not from memory: `--usage
   validation` for the validation speaker and `--split evaluate --usage
   sealed-evaluation` for a sealed one. That is not a label —
   `assert_usable_for` raises if anything later tries to train on a sealed set,
   and the CLI exits non-zero. Freeze before the first read, so the frozen set is
   the recorded set and not the set as it stood after somebody tidied it.
7. Record the coverage cell in `--note`: device, distance, noise, accent, pitch
   band.
8. Confirm the speaker has deleted the recordings from their device, including
   any automatic cloud copy.

## If a speaker withdraws

The process is in `CONSENT_RECORD_TEMPLATE.md` and is the speaker's to trigger,
by any means, with no reason given. On the coordinator's side, within 7 days:
delete the speaker's folder, their manifest and its sidecars, and their consent
record; state in the qualification record that the measurement was taken over a
set that no longer exists, and re-state it over the set that does. Nothing in
this repository has to change, because nothing in this repository ever contained
their audio — which is the point of the whole arrangement. What withdrawal
cannot undo, once a model has been trained on the recordings, is stated honestly
on the consent form rather than glossed.
