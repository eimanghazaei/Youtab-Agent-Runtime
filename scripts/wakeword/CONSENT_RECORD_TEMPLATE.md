# Consent record — "Hey Youtab" voice recording

One copy of this form per speaker. The coordinator fills in the "Use
category" checkbox before the speaker sees the form; the speaker reads the
whole form, asks any questions, and signs it. This file is a **template**:
the filled, signed copy is a record about a named person and must never be
committed to git, uploaded to a shared code repository, or placed in the same
folder as that speaker's audio recordings. See "Where the signed form goes"
below.

---

## What this is

**Youtab** is collecting real voice recordings to train and test its
"Hey Youtab" wake-word detector — the feature that lets the product listen
for its name. You are being asked to record yourself saying the wake phrase,
some phrases that sound like it but are not, some ordinary talking, and a
short recording of your room with nobody speaking. Full instructions are in
`SPEAKER_RECORDING_PACKAGE.md`, provided alongside this form.

You will also fill in a short form describing your recording device and
room (make and model of device, recording app, room size, wall and floor
surfaces, and what background sources are around). That form travels with
your audio; this consent form does not.

## What is collected

- Audio recordings of you saying the wake phrase and related phrases, in
  several deliveries (normal, slow, fast, quiet, loud), from close and from
  further away, and with genuine background noise.
- A short recording of ordinary talking with no wake phrase in it.
- A short recording of your room with nobody speaking.
- The device/environment form described above (not your name — a speaker
  ID, assigned by your coordinator).
- This consent form itself (your name, signature and date).

## Use category

*(Coordinator: check exactly one box below before giving this form to the
speaker, and initial it. This is the honest answer to "what happens to my
recordings" for this specific speaker — it is not the same for everyone.)*

- [ ] **Training.** Your recordings will be used, along with other
  speakers' recordings, to teach the detector what "Hey Youtab" and similar
  phrases sound like. They become part of the model's training data.
- [ ] **Validation.** Your recordings will be used to help choose between
  candidate versions of the detector while it is being built — deciding
  which version and which sensitivity setting to move forward with — but
  will not themselves be used to teach the detector directly.
- [ ] **Held-out evaluation.** Your recordings will be kept completely
  separate from anything the detector is built or tuned on, and used only
  at the end to measure how well the finished detector actually works on a
  voice it has never seen in any form. This is the closest measurement to
  how the detector will perform for a real, new user.

Coordinator initials: __________　　Date marked: ____________

## Who signs, and when

The named speaker signs, after reading this form and
`SPEAKER_RECORDING_PACKAGE.md` and having a chance to ask the coordinator
questions. If the speaker is recording on a shared or work-provided device,
the speaker still signs individually — consent is about the voice being
recorded, not the hardware.

## Where the signed form goes — and where it must not go

- The signed form goes **directly to your coordinator**, outside of any
  code repository, by whatever private channel your coordinator tells you
  to use (handed over in person, or a photo/scan sent directly to them).
- It does **not** go into git, a pull request, or any shared code
  repository — this is the same rule the recording package gives for the
  audio itself, and it applies to this form even more strictly, because
  this form has your name on it.
- It does **not** go in the same folder as your audio recordings, and does
  not travel with them when they are handed off (see
  `SPEAKER_RECORDING_PACKAGE.md`, "Uploading your recordings").

## Retention

Recordings and the device/environment form are kept for as long as they are
part of an actively-used model version, and for a further period after a
model that used them is retired.

> **Placeholder — coordinator to confirm before any form is signed.**
> A specific retention period (e.g. "24 months after the model that used
> this recording is superseded") has not yet been set for this round; that
> is a data-retention decision for the Owner to make, not something a
> template document should assume on their behalf. Do not present a copy of
> this form for signature until this section names an actual duration.

Retention period for this round: ___________________________

## Withdrawing consent

You may withdraw at any time by telling your coordinator. Withdrawal means:

- Your raw audio files and device/environment form are deleted from
  wherever they are stored, and are not used in any future training or
  evaluation run.
- If your recordings have **not yet** been used to train a model, that is
  the end of it — nothing derived from them exists anywhere.

**What withdrawal cannot undo.** If a model has already been **trained**
using your recordings before you withdraw, that trained model's weights
already reflect statistical patterns learned in part from your voice.
Machine-learning weights cannot be selectively edited to remove one
contributor's influence after the fact — the only way to remove it is to
retrain the model from scratch without your data, which is not automatic and
will not happen immediately. Withdrawing after that point stops your data
from being used again; it does not change a model version that has already
been trained and, potentially, already shipped.

If your recordings are marked **held-out evaluation** above, they are, by
design, never used to train anything, so this limitation does not apply to
them — withdrawing removes them from any future evaluation run entirely.

## Questions

Ask your coordinator before you sign, and at any point afterward.
Coordinator / Owner contact: ___________________________

---

## Signature

I have read this form and `SPEAKER_RECORDING_PACKAGE.md`. I understand what
will be recorded, how my specific recordings will be used (checked above),
that raw recordings — including recordings I think went badly — are kept
rather than deleted or redone, how to withdraw, and what withdrawal cannot
undo once a model has been trained on my data.

Speaker printed name: ___________________________

Speaker ID (assigned by coordinator, not your name): ___________________________

Signature: ___________________________　　Date: ____________

Coordinator printed name: ___________________________

Coordinator signature (confirming receipt): ___________________________　　Date: ____________
