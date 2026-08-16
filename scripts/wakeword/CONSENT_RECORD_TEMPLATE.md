# Consent record — wake-word voice recording

Copy this file, fill it in with the speaker before recording anything, and keep
the filled copy beside their recordings on local disk.

**The filled record is never committed and never uploaded.** Save it as
`consent-record-<label>.md` next to the dataset; `.gitignore` and
`tests/tools/test_wakeword_no_human_data_committed.py` both refuse that
filename inside the repository, because a consent record contains the one piece
of personal data the recordings themselves deliberately do not: who the speaker
is.

Two copies. One stays with the operator, next to the audio. One goes to the
speaker, so that withdrawing does not require asking anyone for permission or
remembering who to contact.

---

## Speaker

| | |
|---|---|
| Name | |
| Speaker label (the only identifier that travels with the audio) | E0__ |
| Date of recording | |
| Contact for withdrawal (email, phone — whatever they prefer) | |

## What is being recorded

Short spoken phrases: the wake phrase "hey youtab", some phrases that sound
like it, and about a minute of ordinary conversation. Roughly 25 minutes in
total. Nothing is asked about the speaker, their opinions or their life; the
content is a fixed list of phrases, and it is attached to this record.

## What it will be used for

- [ ] **Training** — the recordings are used to teach a wake-word detector to
      recognise the phrase "hey youtab".
- [ ] **Evaluation only (sealed)** — the recordings are used *once*, to measure
      how well an already-trained detector performs. They are never used to
      teach it anything.

*(Tick exactly one. The choice is recorded in the dataset's manifest and
enforced by tooling: a sealed set cannot be used for training even by
accident.)*

The detector is a small numerical model. It learns what the phrase sounds like.
It does not store the recordings, and it cannot reproduce a voice.

## Where the recordings live

* On local disk, on the operator's machine, in a directory named only by the
  speaker label.
* **Never committed to a code repository.** This is enforced by an automated
  check, not by anyone remembering.
* **Never uploaded** — no cloud storage, no file-sharing link, no email
  attachment, no third-party transcription or analysis service.
* **Never given to anyone else**, inside or outside the project.
* The copy on the recording device (phone, laptop) is deleted after transfer,
  including any automatic cloud backup made by the recording app.

## What is kept alongside them

A list of filenames with a checksum for each, and a short note describing the
recording conditions — device, distance, room, and the language variety the
speaker speaks. No name, no contact details, no transcript of anything other
than the fixed phrase list.

## How long they are kept

Until the wake word is qualified and the qualification record is complete, and
no longer than **24 months** from the date above. After that they are deleted.

## How to withdraw

Say so — by any means, to the contact below. No reason is needed and none will
be asked for.

Within **7 days**: every recording of the speaker is deleted, along with the
manifest and this record. The speaker is told when it is done.

What withdrawal cannot undo: if a detector has already been trained on the
recordings, the trained model does not contain the audio and cannot be made to
reproduce it, but the numbers it learned were influenced by it. If the speaker
asks, that model is retired and retrained without their data rather than
shipped.

| | |
|---|---|
| Project contact for withdrawal | |
| Reachable at | |

## Confirmation

> I have read this record. I understand what is being recorded, what it will be
> used for, that it stays on local disk and is never uploaded or published, and
> that I can withdraw at any time by contacting the person named above.

| | |
|---|---|
| Speaker signature | |
| Date | |
| Operator signature | |
| Date | |
