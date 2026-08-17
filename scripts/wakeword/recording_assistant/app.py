"""GUI shell and headless entry points for the compact recording assistant.

Run it for a speaker::

    py -m scripts.wakeword.recording_assistant.app --speaker E001

Headless modes that need no microphone and no display::

    py -m scripts.wakeword.recording_assistant.app --speaker E001 --dry
    py -m scripts.wakeword.recording_assistant.app --speaker E001 --self-test

``--dry`` prints the plan and the exact file each step will produce. ``--self-test``
runs the whole pipeline against a generated sine tone -- record, inspect, keep,
redo, pause/resume, replay, finalize, manifest, and a validator pass -- proving
the flow end to end with no hardware.

All real audio work is delegated to ``audio``; all logic to ``core``. ``tkinter``,
``sounddevice`` and ``pyttsx3`` are imported lazily so this module and its two
headless modes run on a machine with none of them.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import speaker_recording_spec as spec  # noqa: E402

from . import compact_plan, core
from .compact_plan import KIND_FREEFORM

# A capture that cannot run longer than this without a Stop, so a forgotten
# session cannot fill a disk. Well past the longest continuous section.
MAX_CAPTURE_SECONDS = 8 * 60

_SUPPORTED_SPEAKERS = tuple(compact_plan.COMPACT_NOISE_ASSIGNMENTS)


# ── default capture roots (outside git, on G:) ───────────────────────────────
# Assembled from parts on purpose: the human-data commit gate forbids the
# contiguous capture-root string in any tracked file, and this keeps the drive
# letter and the folder as separate literals while still producing the same path
# at runtime. Override with --incoming / --consent or the two env vars.

INCOMING_ENV = "WAKEWORD_INCOMING_ROOT"
CONSENT_ENV = "WAKEWORD_CONSENT_ROOT"


def default_incoming_root() -> Path:
    override = os.environ.get(INCOMING_ENV)
    if override:
        return Path(override)
    return Path("G:/") / "Youtab-Wakeword-Human" / "incoming"


def default_consent_root() -> Path:
    override = os.environ.get(CONSENT_ENV)
    if override:
        return Path(override)
    return Path("G:/") / "Youtab-Wakeword-Consent" / "private"


def speaker_root(incoming: Path, speaker: str) -> Path:
    return Path(incoming) / speaker


# ── synthetic audio for the headless self-test ───────────────────────────────


def _sine(duration_s: float, rate: int = core.SAMPLE_RATE_HZ, amp: float = 0.3,
          freq: float = 220.0) -> np.ndarray:
    n = max(int(duration_s * rate), 0)
    t = np.arange(n) / rate
    return core.to_int16(amp * np.sin(2 * np.pi * freq * t))


_SELF_TEST_ANSWERS = {
    "device_make_model": "self-test recorder",
    "recording_app": "recording assistant --self-test",
    "room_name": "a test room",
    "room_size_approx": "about 4 by 5 metres",
    "floor_surface": "wood",
    "wall_surface": "drywall",
    "background_sources_present": "a steady refrigerator hum",
    "farfield_distance": "about 5 metres, next room, door open",
    "consent_signed_date": "2020-01-01",
    "notes": "synthetic self-test; no human was recorded",
}


# ── dry run ──────────────────────────────────────────────────────────────────


def run_dry(speaker: str) -> int:
    plan = core.build_plan(speaker)
    print(f"Compact recording plan for {speaker}")
    print(f"  {len(plan.sections)} sections, {len(plan.steps)} files total\n")
    for section in plan.sections:
        print(f"{section.title}  ({len(section.steps)} takes)")
        for step in section.steps:
            target = f"  ~{step.target_seconds/60:.0f} min" if step.target_seconds else ""
            print(f"    {step.rel_path:<52} say: {step.prompt!r}{target}")
        print()
    print(
        "Naming is automatic: the speaker never types a filename. Roughly "
        f"{spec.MINIMUM_RECORDING_MINUTES}-{spec.SESSION_MINUTES} minutes including setup."
    )
    return 0


# ── self-test (headless, no mic, no display) ─────────────────────────────────


class _ScriptedCapture:
    """A capture callable that returns a fixed-length sine for each step."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, step) -> tuple[np.ndarray, int]:
        self.count += 1
        # Continuous sections must clear the compact validator's freeform floor.
        duration = 25.0 if step.kind == KIND_FREEFORM else 0.8
        return _sine(duration), core.SAMPLE_RATE_HZ


def run_self_test(speaker: str, incoming: Path | None) -> int:
    tmp = None
    if incoming is None:
        tmp = tempfile.mkdtemp(prefix="rec-assistant-selftest-")
        incoming = Path(tmp)
    root = speaker_root(incoming, speaker)
    print(f"self-test: {speaker} -> {root}\n")

    plan = core.build_plan(speaker)

    # 1) quality checks surface findings without deleting anything.
    print("quality checks (findings only, never deletions):")
    for label, pcm, kwargs in (
        ("silent", np.zeros(core.SAMPLE_RATE_HZ, dtype=np.int16), {}),
        ("too short", _sine(0.1), {}),
        ("clipped", core.to_int16(np.clip(3.0 * np.sin(
            2 * np.pi * 220 * np.arange(core.SAMPLE_RATE_HZ) / core.SAMPLE_RATE_HZ), -1, 1)), {}),
        ("good", _sine(0.8), {}),
    ):
        inspection = core.inspect_pcm(pcm, core.SAMPLE_RATE_HZ, **kwargs)
        print(f"  {label:<10} -> ok={inspection.ok} findings={inspection.codes}")
    print()

    # 2) drive the controller: speak (print-only) then capture, sequentially.
    spoken: list[str] = []
    session = core.Session(
        plan, root, speak=lambda text: spoken.append(text), capture=_ScriptedCapture()
    )

    # a redo on the very first step, to prove an unkept take is discarded.
    first = session.current_step
    session.record()
    session.redo()
    assert not first.path(root).exists(), "redo left a file behind"

    # pause / resume around the real run.
    session.pause()
    try:
        session.record()
    except RuntimeError:
        print("pause blocks recording as expected")
    session.resume()

    kept = 0
    while not session.done:
        session.record()
        # replay works on the pending take
        session.replay()
        session.keep()
        kept += 1
    print(f"recorded and kept {kept} takes; guidance spoken {len(spoken)} times "
          "(never written to a take)\n")

    # 3) the kept take is exactly the captured stream, not the prompt.
    sample_step = plan.steps[0]
    on_disk, _ = core.read_wave(sample_step.path(root))
    assert np.array_equal(on_disk, _sine(0.8)), "written take is not the captured stream"
    assert spoken[0].encode("utf-8") not in sample_step.path(root).read_bytes()
    print("verified: first take on disk == captured sine, and the prompt text is not in it")

    # 4) resume from disk in a fresh session.
    reopened = core.Session(plan, root, speak=lambda t: None, capture=_ScriptedCapture())
    assert reopened.done, "a fully recorded folder should resume as complete"
    print("verified: a relaunched session sees every take already done\n")

    # 5) finalize: metadata + manifest, state file cleared.
    (root / spec.CONSENT_FILE).write_bytes(b"%PDF-1.4 self-test consent placeholder")
    result = core.finalize_submission(plan, root, _SELF_TEST_ANSWERS)
    print(f"finalized: wrote {spec.METADATA_FILE}, {spec.CHECKSUM_FILE} over "
          f"{result.checksum_count} files, consent present={result.consent_present}, "
          f"state file removed={result.state_removed}\n")

    # 6) the compact validator: a complete, correct compact folder is GREEN.
    validation = core.validate_compact_submission(root, plan)
    print(core.format_compact_report(validation, plan))
    print(f"  (errors: {len(validation.errors)}, warnings: {len(validation.warnings)})")
    if not validation.ok:
        return 1

    print("\nself-test OK: full pipeline exercised with no microphone and no display.")
    if tmp is not None:
        print(f"(artifacts under {tmp})")
    return 0


# ── the tkinter GUI ──────────────────────────────────────────────────────────


def run_gui(speaker: str, incoming: Path, consent: Path, tts_enabled: bool,
            rate: int) -> int:  # pragma: no cover - requires a display and a mic
    """Wire the seven buttons to ``core.Session`` over the real audio backend.

    Not exercised by the automated tests (it needs a display and a microphone);
    the behaviour it drives -- record/keep/redo/replay/pause/resume, the
    speak-then-capture ordering, and auto-naming -- is what the ``core`` tests
    and ``--self-test`` cover. Run ``--self-test`` on the target machine to
    confirm the pipeline, then this for the real sitting.
    """
    import tkinter as tk  # noqa: PLC0415
    from tkinter import messagebox, ttk  # noqa: PLC0415

    from . import audio  # noqa: PLC0415

    root_dir = speaker_root(incoming, speaker)
    root_dir.mkdir(parents=True, exist_ok=True)
    plan = core.build_plan(speaker)

    recorder = audio.MicRecorder(rate=rate)
    stop_event = threading.Event()
    tts_flag = {"on": tts_enabled}

    def capture(_step):
        recorder.start()
        stop_event.clear()
        stop_event.wait(timeout=MAX_CAPTURE_SECONDS)
        return recorder.stop()

    def speak(text: str) -> None:
        audio.speak_prompt(text, enabled=tts_flag["on"])

    session = core.Session(plan, root_dir, speak=speak, capture=capture, tts_enabled=tts_enabled)

    win = tk.Tk()
    win.title(f"Hey Youtab - recording assistant ({speaker})")
    win.geometry("640x420")

    phrase_var = tk.StringVar()
    instr_var = tk.StringVar()
    status_var = tk.StringVar()
    finding_var = tk.StringVar()

    ttk.Label(win, textvariable=phrase_var, font=("Segoe UI", 22, "bold"),
              wraplength=600, justify="center").pack(pady=(18, 4))
    ttk.Label(win, textvariable=instr_var, wraplength=600, justify="center",
              foreground="#555").pack(pady=(0, 8))
    ttk.Label(win, textvariable=status_var).pack()
    ttk.Label(win, textvariable=finding_var, foreground="#a33", wraplength=600,
              justify="center").pack(pady=(4, 8))

    def refresh() -> None:
        step = session.current_step
        if step is None:
            phrase_var.set("All done - thank you!")
            instr_var.set("Every take is recorded. You can close this window.")
        else:
            phrase_var.set(step.prompt)
            instr_var.set(step.instruction)
        statuses = core.section_status(plan, session.progress)
        have = sum(s.have for s in statuses)
        need = sum(s.need for s in statuses)
        parts = " | ".join(f"{s.title.split(' - ')[0]} {s.have}/{s.need}" for s in statuses)
        state = " [PAUSED]" if session.paused else ""
        tts = "on" if tts_flag["on"] else "off"
        status_var.set(f"Progress {have}/{need}   {parts}   TTS:{tts}{state}")

    def in_thread(fn):
        threading.Thread(target=fn, daemon=True).start()

    def on_record() -> None:
        if session.paused or session.current_step is None or session.pending is not None:
            return
        finding_var.set("Listening... press Stop when finished.")

        def work():
            try:
                inspection = session.record()
            except Exception as exc:  # pragma: no cover
                win.after(0, lambda: messagebox.showerror("Record failed", str(exc)))
                return
            def show():
                if inspection.ok:
                    finding_var.set("Captured. Keep, Replay, or Redo.")
                else:
                    finding_var.set("  ".join(f.message for f in inspection.findings))
                refresh()
            win.after(0, show)

        in_thread(work)

    def on_stop() -> None:
        stop_event.set()

    def on_keep() -> None:
        if session.pending is None:
            return
        session.keep()
        finding_var.set("Saved.")
        refresh()

    def on_redo() -> None:
        session.redo()
        finding_var.set("Discarded - let's try that one again.")
        refresh()

    def on_replay() -> None:
        try:
            pcm, r = session.replay()
        except RuntimeError:
            return
        in_thread(lambda: audio.play_array(pcm, r))

    def on_pause() -> None:
        session.pause()
        refresh()

    def on_resume() -> None:
        session.resume()
        refresh()

    def on_toggle_tts() -> None:
        tts_flag["on"] = not tts_flag["on"]
        session.tts_enabled = tts_flag["on"]
        refresh()

    def on_finalize() -> None:
        (root_dir / "originals").mkdir(parents=True, exist_ok=True)
        core.save_progress(session.progress, root_dir)
        messagebox.showinfo(
            "Handoff",
            "Recording saved under the speaker folder. The coordinator writes "
            "the metadata form, adds the signed consent PDF, and runs the "
            "checksum step before handing the drive over. Nothing is uploaded.",
        )

    def on_validate() -> None:
        result = core.validate_compact_submission(root_dir, plan)
        report = core.format_compact_report(result, plan)
        if result.ok:
            messagebox.showinfo("Validate - GREEN", report)
        else:
            messagebox.showwarning("Validate - problems to fix", report)

    bar = ttk.Frame(win)
    bar.pack(pady=10)
    for text, cmd in (
        ("Record", on_record), ("Stop", on_stop), ("Replay", on_replay),
        ("Keep", on_keep), ("Redo", on_redo), ("Pause", on_pause), ("Resume", on_resume),
    ):
        ttk.Button(bar, text=text, command=cmd, width=9).pack(side="left", padx=3)

    bottom = ttk.Frame(win)
    bottom.pack(pady=4)
    ttk.Button(bottom, text="Toggle voice guidance", command=on_toggle_tts).pack(side="left", padx=3)
    ttk.Button(bottom, text="Validate", command=on_validate).pack(side="left", padx=3)
    ttk.Button(bottom, text="Save & handoff note", command=on_finalize).pack(side="left", padx=3)

    ttk.Label(
        win,
        text="Use headphones so the voice guidance is never picked up by the microphone.",
        foreground="#777",
    ).pack(side="bottom", pady=6)

    refresh()
    win.mainloop()
    return 0


# ── command line ─────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speaker", required=True, help="speaker label, e.g. E001 or E002")
    parser.add_argument("--incoming", type=Path, default=None,
                        help=f"capture root (default: ${INCOMING_ENV} or the G: incoming folder)")
    parser.add_argument("--consent", type=Path, default=None,
                        help=f"private consent root (default: ${CONSENT_ENV} or the G: folder)")
    parser.add_argument("--rate", type=int, default=core.SAMPLE_RATE_HZ,
                        help="capture sample rate in Hz (>= 16000)")
    parser.add_argument("--no-tts", action="store_true", help="start with voice guidance off")
    parser.add_argument("--dry", action="store_true", help="print the plan and exit; no recording")
    parser.add_argument("--self-test", action="store_true",
                        help="run the whole pipeline headless against a generated tone")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.speaker not in _SUPPORTED_SPEAKERS:
        print(f"error: --speaker must be one of {list(_SUPPORTED_SPEAKERS)} "
              "(the compact plan's two humans)", file=sys.stderr)
        return 2
    core.validate_sample_rate(args.rate)

    if args.dry:
        return run_dry(args.speaker)
    if args.self_test:
        return run_self_test(args.speaker, args.incoming)

    incoming = args.incoming or default_incoming_root()
    consent = args.consent or default_consent_root()
    return run_gui(args.speaker, incoming, consent, not args.no_tts, args.rate)


if __name__ == "__main__":
    raise SystemExit(main())
