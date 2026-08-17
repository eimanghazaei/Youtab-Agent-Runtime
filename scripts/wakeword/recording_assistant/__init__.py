"""Local, guided recording assistant for the compact "Hey Youtab" sitting.

A small operator tool -- not a shipped runtime dependency -- that walks a human
speaker through the compact recording package one prompt at a time, auto-names
every file into the layout ``validate_speaker_submission.py`` and
``import_speaker.py`` accept, and never uploads anything.

Modules:

* ``compact_plan`` -- the compact repetition counts, as data (phrase set from
  ``speaker_recording_spec``).
* ``core`` -- all non-audio, non-GUI logic: plan/steps, filename assignment,
  resumable progress, quality inspection, manifest, metadata, session control.
* ``audio`` -- thin ``sounddevice`` capture / ``pyttsx3`` guidance wrappers,
  imported lazily so ``core`` needs no audio stack.
* ``app`` -- the ``tkinter`` shell plus ``--self-test`` and ``--dry`` headless
  modes.
"""
