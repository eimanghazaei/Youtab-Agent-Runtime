"""Human speech, and everything derived from it, must never enter git.

Background
----------
Qualifying "hey youtab" needs recordings of real people, because every positive
the model has ever seen came from one TTS family. Those recordings are given
for one purpose, on the promise that they stay on local disk — never committed,
never uploaded, never leaving the machine that captured them. The promise
covers more than the ``.m4a`` files: an openWakeWord feature tensor is the
speech resampled into 96 dimensions and is trivially attributable to the person
who spoke it, and a transcript is a verbatim record of what they said.

The failure this guards against is not malice, it is ``git add -A`` after a
recording session. So there are two layers, and they do different jobs:

``.gitignore``
    Stops the accident. It cannot stop ``git add -f``, exactly as the
    infographic rule in that file records after a single ignore pattern was
    sidestepped by a differently spelled directory (#70552).

this gate
    Is what actually enforces the rule. It fails on any *tracked* file whose
    path looks like human audio, a derived tensor or a transcript, and on any
    tracked text whose *content* names a speaker id or a capture root.

The allow-list below is a list of exact paths, not a pattern. A pattern
permitting ``tests/fixtures/wakeword/audio/*.wav`` would also permit a stray
recording dropped into that directory, which is precisely the mistake this is
here to catch. Adding a file to it is a review decision, and the failure
message says so.

Two deliberate limits, recorded rather than papered over:

* Video containers (``.mp4``, ``.webm``, ``.mov``) are **not** in the audio
  inventory. This repository legitimately ships a UI screen recording
  (``website/static/img/docs/.../session-orchestrator-demo.mp4``), and adding
  video suffixes here would mean allow-listing it — which would then also
  allow-list a future screen recording that happened to have someone talking
  over it. If a speaker is ever captured to video, this inventory has to grow
  and that mp4 has to be justified individually.
* The path rules cannot see inside a file. A recording renamed to ``.dat``
  passes them; what catches it is the content scan below and human review of
  any new binary in a diff.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: This file has to name the markers it forbids in order to search for them, so
#: it is the one tracked file exempt from its own content scan. Same convention
#: as ``tests/youtab_agent_cli/test_engine_catalog_is_private.py``, which names
#: the retired catalogue URL in order to forbid it.
SELF = Path(__file__).resolve().relative_to(REPO).as_posix()

# ── what may be tracked ──────────────────────────────────────────────────────

#: Every binary artifact this repository is allowed to carry, by exact path.
#: The 33 fixtures are synthesized speech cut out of the pipeline's own
#: evaluation split (``scripts/wakeword/make_test_fixture.py``); no human ever
#: spoke into a microphone to produce one. ``jo.wav`` is the NeuTTS reference
#: voice that ships with the tool. The two model files are the shipped
#: detector, whose provenance is in ``tools/wakewords/MODEL_CARD.md``.
ALLOWED: frozenset[str] = frozenset(
    {
        "tests/fixtures/wakeword/audio/00_positive_clean.wav",
        "tests/fixtures/wakeword/audio/01_positive_clean.wav",
        "tests/fixtures/wakeword/audio/02_positive_noisy.wav",
        "tests/fixtures/wakeword/audio/03_positive_clean.wav",
        "tests/fixtures/wakeword/audio/04_positive_clean.wav",
        "tests/fixtures/wakeword/audio/05_positive_reverberant.wav",
        "tests/fixtures/wakeword/audio/06_positive_noisy.wav",
        "tests/fixtures/wakeword/audio/07_positive_reverberant.wav",
        "tests/fixtures/wakeword/audio/08_positive_noisy.wav",
        "tests/fixtures/wakeword/audio/09_positive_noisy.wav",
        "tests/fixtures/wakeword/audio/10_positive_noisy.wav",
        "tests/fixtures/wakeword/audio/11_positive_reverberant.wav",
        "tests/fixtures/wakeword/audio/12_near_phrase.wav",
        "tests/fixtures/wakeword/audio/13_near_phrase.wav",
        "tests/fixtures/wakeword/audio/14_near_phrase.wav",
        "tests/fixtures/wakeword/audio/15_near_phrase.wav",
        "tests/fixtures/wakeword/audio/16_near_phrase.wav",
        "tests/fixtures/wakeword/audio/17_near_phrase.wav",
        "tests/fixtures/wakeword/audio/18_near_phrase.wav",
        "tests/fixtures/wakeword/audio/19_near_phrase.wav",
        "tests/fixtures/wakeword/audio/20_near_phrase.wav",
        "tests/fixtures/wakeword/audio/21_synthesized_speech.wav",
        "tests/fixtures/wakeword/audio/22_synthesized_speech.wav",
        "tests/fixtures/wakeword/audio/23_synthesized_speech.wav",
        "tests/fixtures/wakeword/audio/24_recorded_speech.wav",
        "tests/fixtures/wakeword/audio/25_recorded_speech.wav",
        "tests/fixtures/wakeword/audio/26_recorded_speech.wav",
        "tests/fixtures/wakeword/audio/27_recorded_speech.wav",
        "tests/fixtures/wakeword/audio/28_recorded_speech.wav",
        "tests/fixtures/wakeword/audio/29_recorded_speech.wav",
        "tests/fixtures/wakeword/audio/30_background_only.wav",
        "tests/fixtures/wakeword/audio/31_background_only.wav",
        "tests/fixtures/wakeword/audio/32_background_only.wav",
        "tests/fixtures/wakeword/samples.npz",
        "tools/neutts_samples/jo.wav",
        "tools/wakewords/hey_youtab.onnx",
        "tools/wakewords/hey_youtab.tflite",
    }
)

# ── what a forbidden file looks like ─────────────────────────────────────────

#: Containers a capture session actually produces. iOS Voice Memos writes
#: ``.m4a``, Android writes ``.m4a`` or ``.3gp``, every desktop recorder writes
#: ``.wav``.
AUDIO_SUFFIXES = frozenset(
    {
        ".wav",
        ".m4a",
        ".m4b",
        ".mp3",
        ".aac",
        ".amr",
        ".flac",
        ".ogg",
        ".opus",
        ".aiff",
        ".aif",
        ".caf",
        ".wma",
        ".3gp",
    }
)

#: Derived feature tensors — the form the audio takes once the front end has
#: run over it, and the form it would most plausibly be committed in ("it's
#: just numbers").
TENSOR_SUFFIXES = frozenset({".npy", ".npz"})

#: Trained artifacts. Not private data, but a derived work of whatever was
#: trained on, so a new one appearing in a diff is a review decision rather
#: than a file that slips in beside a commit about something else.
MODEL_SUFFIXES = frozenset({".onnx", ".tflite"})

#: A speaker directory, at any depth: ``speaker_E001/``, ``data/speaker_E12/``.
SPEAKER_PATH = re.compile(r"(?:^|/)speaker_E\d+", re.IGNORECASE)

#: Capture roots, and the shape of a per-take log.
DATASET_PATH = re.compile(
    r"(?:^|/)(?:wakeword[-_]human|human[-_]speech|raw[-_]audio|recordings)(?:/|$)",
    re.IGNORECASE,
)
TRANSCRIPT_PATH = re.compile(r"(?:^|/)[^/]*transcripts?[^/]*\.(?:json|jsonl|csv|tsv|txt)$", re.IGNORECASE)

# ── what forbidden content looks like ────────────────────────────────────────

#: Literal markers. Backslashes are normalised to ``/`` and everything is
#: lower-cased before matching, so ``G:\Youtab-Wakeword-Human``,
#: ``g:/youtab-wakeword-human`` and ``/mnt/g/Youtab-Wakeword-Human`` are one
#: needle each rather than six.
FORBIDDEN_TEXT: tuple[str, ...] = (
    "speaker_e0",
    "g:/youtab-wakeword-human",
    "/mnt/g/youtab-wakeword-human",
)

#: A transcript keyed by speaker, in any of the shapes a tool would write it:
#: ``"speaker_id": "E002"``, ``speaker=E002``, ``speakerId:E003``. Prose about
#: "speaker E003" is deliberately not matched — the label itself is not private
#: data, a record of what that person said is.
SPEAKER_KEYED_DATA = re.compile(
    # The optional quote before the separator is what makes this match JSON
    # (`"speaker_id": "E002"`) as well as YAML and CSV.
    r"\bspeaker_?(?:id)?[\"']?\s*[:=]\s*[\"']?E\d{3}\b",
    re.IGNORECASE,
)

#: CI must never name a path on the machine that captured a dataset. A workflow
#: that did would echo it into a public log on every run, before it ever read a
#: byte.
HOST_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/mnt/[a-z]/)")


def _tracked() -> list[str]:
    """Every tracked path, repo-relative and slash-separated.

    ``git ls-files`` rather than a filesystem walk: the subject is what is *in
    the repository*, and a walk would also see the operator's untracked working
    directory — including, if they ignored the instructions, the dataset this
    is trying to keep out.
    """
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def _path_offence(rel: str) -> str | None:
    """Why ``rel`` may not be tracked, or None if it may."""
    suffix = Path(rel).suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in TENSOR_SUFFIXES:
        return "derived feature tensor"
    if suffix in MODEL_SUFFIXES:
        return "trained model artifact"
    if SPEAKER_PATH.search(rel):
        return "speaker directory"
    if DATASET_PATH.search(rel):
        return "human-speech dataset directory"
    if TRANSCRIPT_PATH.search(rel):
        return "transcript"
    return None


def _text_offences(text: str) -> list[str]:
    """Markers of private speech inside a file's content."""
    haystack = text.replace("\\", "/").lower()
    found = [needle for needle in FORBIDDEN_TEXT if needle in haystack]
    match = SPEAKER_KEYED_DATA.search(text)
    if match:
        found.append(f"speaker-keyed record ({match.group(0)!r})")
    return found


def _tracked_text() -> list[tuple[str, str]]:
    """Tracked files that decode as text, with their contents.

    Binary is detected by a NUL byte rather than by extension, the same way
    ``scripts/youtab/secret_gate.py`` does it, so a transcript saved as
    ``.dat`` is still read.
    """
    out: list[tuple[str, str]] = []
    for rel in _tracked():
        if rel == SELF:
            continue
        path = REPO / rel
        try:
            raw = path.read_bytes()
        except OSError:
            continue  # symlink to nowhere, or a path git knows and the FS does not
        if b"\0" in raw:
            continue
        try:
            out.append((rel, raw.decode("utf-8")))
        except UnicodeDecodeError:
            continue
    return out


# ── the gate ─────────────────────────────────────────────────────────────────


def test_no_tracked_file_looks_like_human_audio_or_anything_derived_from_it() -> None:
    tracked = _tracked()

    # Non-vacuity: prove the file list is a real repository before concluding
    # anything from its emptiness. A `git ls-files` that returned nothing --
    # wrong cwd, detached worktree, git absent -- would otherwise report a
    # clean bill of health for a repository it never looked at.
    assert len(tracked) > 1000, (
        f"only {len(tracked)} tracked files found; this check is not looking at "
        "the repository it thinks it is"
    )

    offenders = {rel: why for rel in tracked if (why := _path_offence(rel)) and rel not in ALLOWED}
    assert not offenders, (
        "these tracked files carry recorded speech, something derived from it, "
        "or an artifact that needs review:\n  "
        + "\n  ".join(f"{rel}  ({why})" for rel, why in sorted(offenders.items()))
        + "\n\nIf one of them is a legitimate synthetic fixture or shipped "
        "artifact, add its exact path to ALLOWED in this file — and say in the "
        "commit message who or what produced the audio."
    )


def test_the_allow_list_is_exhaustive_and_every_entry_still_exists() -> None:
    """The allow-list is the inventory, so it must match the repository exactly.

    Two failures at once. An entry that no longer exists means the list has
    rotted into a set of names nobody checks. A matched file missing from the
    list would have failed the gate above, so equality here also proves the
    matcher matches something — without it, a typo in every suffix would make
    the gate above pass by finding nothing.
    """
    matched = {rel for rel in _tracked() if _path_offence(rel)}
    assert matched == set(ALLOWED), (
        f"tracked but not allow-listed: {sorted(matched - set(ALLOWED))}\n"
        f"allow-listed but not tracked: {sorted(set(ALLOWED) - matched)}"
    )
    assert len(matched) == 37, f"the artifact inventory changed size: {len(matched)}"


def test_the_path_rules_catch_what_a_recording_session_actually_produces() -> None:
    """The control that proves the rules have teeth.

    Every path below is one a real capture session or feature-extraction run
    would create. If a refactor breaks the matcher, the gate above goes quietly
    green; this goes red.
    """
    must_flag = (
        "datasets/speaker_E003/take_01.m4a",
        "tests/fixtures/wakeword/audio/33_E002_positive.wav",
        "scripts/wakeword/human/E002/features.npz",
        "data/wakeword-human/manifest.wav",
        "eval/E002_transcript.json",
        "notes/transcripts.csv",
        "recordings/session-1/clip.dat",
        "work/raw_audio/x.dat",
        "tools/wakewords/hey_youtab_r7.onnx",
    )
    for rel in must_flag:
        assert _path_offence(rel), f"{rel} is not recognised as human data"

    must_pass = (
        "scripts/wakeword/README.md",
        "tools/wake_word.py",
        "tests/tools/test_wake_word.py",
        "website/static/img/docs/tui-session-orchestrator/session-orchestrator-demo.mp4",
        "docs/evidence/transcription-notes.md",
    )
    for rel in must_pass:
        assert _path_offence(rel) is None, f"{rel} is falsely flagged as human data"


def test_no_tracked_text_names_a_speaker_or_a_capture_root() -> None:
    """Paths are not the only leak. Content is the other half.

    A JSON of takes, a README that pastes the capture root, an evaluation
    report that quotes what E002 said — none of them match a suffix rule, and
    each one is the private part of the dataset rather than a wrapper around
    it.
    """
    files = _tracked_text()

    # Non-vacuity: the decode filter is aggressive (NUL byte, UTF-8), and a
    # bug in it would leave nothing to scan.
    assert len(files) > 500, f"only {len(files)} tracked text files were read"

    offenders = {rel: found for rel, text in files if (found := _text_offences(text))}
    assert not offenders, (
        "these tracked files name a speaker id or a private capture root:\n  "
        + "\n  ".join(f"{rel}  {found}" for rel, found in sorted(offenders.items()))
    )


def test_the_content_rules_catch_a_transcript_and_a_capture_root() -> None:
    """Control for the scan above, on synthetic text.

    The markers are assembled from fragments so this test does not itself
    become a tracked file containing them.
    """
    speaker_dir = "speaker_" + "E002"
    for sample in (
        f"positives: {speaker_dir}/take_04.m4a",
        r'{"file": "G:\Youtab-Wakeword-Human\E002\01.m4a"}',
        "src = /mnt/g/Youtab-Wakeword-Human/E002",
        '{"speaker_id": "E002", "text": "hey youtab"}',
        "speaker=E003,take=7",
    ):
        assert _text_offences(sample), f"not recognised as private data: {sample!r}"

    for sample in (
        "The package is handed to speaker E003 before the session.",
        "| speaker | E003 | phone |",
        "speaker_label: E003",
    ):
        assert not _text_offences(sample), f"falsely flagged: {sample!r}"


# ── the ignore rules, and CI ─────────────────────────────────────────────────


def _is_ignored(rel: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", "-q", rel],
            capture_output=True,
        ).returncode
        == 0
    )


def test_gitignore_stops_the_accident_before_the_gate_has_to() -> None:
    """`git add -A` in a checkout that contains a dataset must add nothing.

    This gate is the enforcement, but it only runs in CI, by which point the
    audio is in a commit and rewriting history is the only remedy. The ignore
    rules are what make the accident not happen.
    """
    for rel in (
        "speaker_E001/take_01.m4a",
        "data/speaker_E002/features.npz",
        "wakeword-human/E003/01.wav",
        "recordings/session.flac",
        "eval/takes_transcript.json",
        "work/embeddings.npy",
    ):
        assert _is_ignored(rel), f"{rel} would be picked up by `git add -A`"

    # Non-vacuity: `git check-ignore` returning 0 for everything -- a broken
    # invocation, a stray `*` rule -- would satisfy the loop above while
    # meaning nothing.
    for rel in ("scripts/wakeword/README.md", "tools/wake_word.py"):
        assert not _is_ignored(rel), f"{rel} is ignored; the rules are too broad"


def test_no_workflow_step_can_echo_a_dataset_path() -> None:
    """CI logs are public output; a path in a workflow is printed every run.

    A CI checkout contains only tracked files, so a workflow cannot echo a
    dataset it does not name. This keeps it that way: no host-absolute path in
    any workflow, which is also the shape a "just point it at my drive" step
    would take.
    """
    workflows = sorted((REPO / ".github" / "workflows").glob("*.y*ml"))
    assert workflows, "no workflow files found; this check is looking in the wrong place"

    offenders: dict[str, list[str]] = {}
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        hits = [m.group(0) for m in HOST_ABSOLUTE_PATH.finditer(text)]
        hits += _text_offences(text)
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"workflows name host-local paths: {offenders}"

    # Non-vacuity: the pattern has to match the thing it is looking for.
    assert HOST_ABSOLUTE_PATH.search(r"run: python x.py --root G:\data")
    assert HOST_ABSOLUTE_PATH.search("run: ls /mnt/g/data")
    assert not HOST_ABSOLUTE_PATH.search("run: python -m pip install -e '.[dev]'")
