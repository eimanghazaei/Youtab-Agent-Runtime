# Consent record — "Hey Youtab" voice recording

One copy of this form per speaker. The coordinator fills in the "What it will be
used for" box and the retention date before the speaker sees the form; the
speaker reads the whole form, asks any questions, and signs it.

This file is a **template**. The filled, signed copy is a record about a named
person: it is never committed to git, never uploaded to a shared code
repository, and never sent through a chat, ticket or email thread. Save a digital
copy — if one is kept at all — as `consent-record-<speaker-label>.md`;
`.gitignore` and `tests/tools/test_wakeword_no_human_data_committed.py` both
refuse that filename inside the repository, because a consent record contains the
one piece of personal data the recordings themselves deliberately do not: who the
speaker is.

**Two copies.** One stays with the coordinator, in the encrypted store described
under "Where the signed form goes". One goes to the speaker, so that withdrawing
does not require asking anyone for permission or remembering who to contact.

---

## Speaker

| | |
|---|---|
| Name | |
| Speaker label (the only identifier that travels with the audio) | E0__ |
| Date of recording | |
| Contact for withdrawal (email, phone — whatever they prefer) | |

Nothing else is asked about the speaker. No date of birth, no address, no
employer, no gender, no photograph. The recording folder itself carries the
speaker label and a description of the device and room — never a name, never a
contact detail, and never a transcript of anything other than the fixed phrase
list.

## What is being recorded

Short spoken phrases: the wake phrase "hey youtab", a battery of phrases that
sound like it but are not, about five minutes of ordinary talking, and about
three minutes of the room with nobody speaking. Roughly 70 minutes in total,
including the paperwork. Full instructions are in
`SPEAKER_RECORDING_PACKAGE.md`, provided alongside this form.

Nothing is asked about the speaker, their opinions or their life; the content of
the recordings is a fixed list of phrases, plus five minutes of talking about
whatever the speaker chooses.

The speaker also fills in a short form describing their recording device and
room (make and model of device, recording app, room size, wall and floor
surfaces, and what usually makes noise nearby). That form travels with the audio
under the speaker label; this consent form does not travel with the audio.

## What it will be used for

*(Coordinator: tick exactly one box below before giving this form to the speaker,
and initial it. This is the honest answer to "what happens to my recordings" for
this specific speaker — it is not the same for everyone in this round.)*

- [ ] **Training.** The recordings are used, along with other speakers'
      recordings, to teach the detector what "hey youtab" and similar phrases
      sound like. They become part of the model's training data.
- [ ] **Validation.** The recordings are used to help choose between candidate
      versions of the detector while it is being built — which version, which
      training epoch and which sensitivity setting to move forward with — but are
      never used to teach the detector directly.
- [ ] **Sealed final evaluation.** The recordings are kept completely separate
      from anything the detector is built or tuned on, and are used only at the
      end, to measure how well the finished detector works on a voice it has
      never seen in any form. This is the closest measurement to how the detector
      will perform for a real, new user.

Coordinator initials: __________　　Date marked: ____________

The choice is recorded in the dataset's frozen manifest and enforced by tooling,
not by anyone remembering: a sealed set cannot be used for training even by
accident — `assert_usable_for` raises and the command exits non-zero.

The detector is a small numerical model. It learns what the phrase sounds like.
It does not store the recordings, and it cannot reproduce a voice.

## Where the recordings live

* On **local disk**, on the coordinator's machine, in a folder named only by the
  speaker label.
* **Never committed** to a code repository. This is enforced by an automated
  check, not by anyone remembering.
* **Never uploaded** — no cloud storage, no file-sharing link, no email
  attachment, no third-party transcription or analysis service.
* **Never given to anyone else**, inside or outside the project.
* The copy on the recording device (phone, laptop) is deleted after transfer,
  including any automatic cloud backup made by the recording app.

## Where the signed form goes — and where it must not go

* The signed form goes **directly to the coordinator**, by a private channel
  (handed over in person, or a photo/scan sent directly to them), and is held
  **encrypted and access-controlled**, outside any code repository, readable only
  by the people who administer this recording round.
* It does **not** go into git, a pull request, or any shared code repository —
  the same rule the recording package gives for the audio, applied more strictly,
  because this form has a name on it.
* It does **not** travel with the audio when the speaker hands the recordings
  over. The coordinator is the one who files a scan of it into the speaker's
  governed archive as `CONSENT.pdf`, so that consent proof cannot drift away from
  the recordings it authorises. Separate in transit; bound together, under access
  control, at rest.

## What is kept alongside the recordings

A list of filenames with a SHA-256 checksum for each, and the device and
environment form — device, app, room, distance, noise sources. No name, no
contact details, no transcript of anything other than the fixed phrase list.

## How long it is kept

**Data minimisation first.** The dataset holds the speaker label and nothing that
identifies the person. This form is the only artifact in the workflow that
carries identity, and it carries the minimum needed to prove consent and to
honour a withdrawal: name, label, date, and one contact route.

**Recordings and the device/environment form** are kept while they remain in
governed use — that is, while any model in use was trained, tuned or qualified on
them — and are deleted no later than **24 months** after the last such model is
retired, or within 7 days of a withdrawal, whichever comes first.

**This consent record** is kept for exactly as long as the recordings or any
model derived from them remain in governed use, and is destroyed together with
them. Consent proof that expires before the data it authorises would leave the
data unauthorised; a consent record kept after the data is gone is personal data
held for no reason. Both are wrong, so the two lifetimes are the same one.

Retention period agreed for this speaker: ___________________________
*(Coordinator: the retirement date of the last model, plus 24 months, or the
round's own end date if earlier. Fill this in before the speaker signs.)*

> **This retention policy is in force as written, and is marked pending legal
> review.** Review may tighten it — a shorter tail, fewer fields, a narrower
> definition of governed use — and any tightening applies to records already
> signed. Nothing in this round waits on that review: no form is held back, no
> recording is delayed, and no speaker is asked to sign a blank. If the review
> requires a change that materially affects a speaker, that speaker is told and
> may withdraw on the spot.

## How to withdraw

Say so — by any means, to the contact below. **No reason is needed** and none
will be asked for.

**Within 7 days:**

* Every recording of the speaker is deleted, along with the frozen manifest and
  its checksum sidecars, the device/environment form, and this record.
* The recordings are removed from any future training, validation or evaluation
  run.
* The speaker is told, in writing, when it is done.

**What withdrawal cannot undo.** If a model has already been **trained** using
the recordings before the withdrawal, that trained model's weights already
reflect statistical patterns learned in part from that voice. The model does not
contain the audio and cannot be made to reproduce it, but machine-learning
weights cannot be selectively edited to remove one contributor's influence after
the fact. The only way to remove it is to retrain from scratch without the data,
which is not automatic and will not happen immediately. Withdrawing stops the
data being used again; it does not change a model version that has already been
trained and, potentially, already shipped. If the speaker asks for it, that model
is retired and retrained without their data rather than shipped further, and the
qualification record is re-stated over the set that remains.

If the recordings are marked **Sealed final evaluation** above, they are by
design never used to train anything, so this limitation does not apply to them:
withdrawing removes them from any future measurement entirely.

| | |
|---|---|
| Project contact for withdrawal | |
| Reachable at | |

## Questions

Ask the coordinator before signing, and at any point afterwards.

Coordinator / Owner contact: ___________________________

---

## Confirmation

> I have read this record and `SPEAKER_RECORDING_PACKAGE.md`. I understand what
> is being recorded, what it will be used for in my case, that it stays on local
> disk and is never uploaded or published, that raw recordings — including ones I
> think went badly — are kept rather than deleted or re-done, how long it is
> kept, how to withdraw, and what withdrawal cannot undo once a model has been
> trained on my data.

| | |
|---|---|
| Speaker printed name | |
| Speaker label (assigned by the coordinator, not a name) | E0__ |
| Speaker signature | |
| Date | |
| Coordinator printed name | |
| Coordinator signature (confirming receipt) | |
| Date | |
