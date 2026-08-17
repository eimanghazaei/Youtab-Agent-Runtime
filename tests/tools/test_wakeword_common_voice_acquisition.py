"""The Common Voice path must be complete except for the one Owner action.

Background
----------
The corpus is gated: reaching it needs a person to accept the dataset terms
under their own account and to mint a read-scope token. That is the only step
this repository cannot do — and it is precisely the step that tends to swallow
the rest of the design, because "blocked on a credential" reads the same
whether the tooling is finished or has never been written.

So the tooling is finished and tested here, with no network and no token: the
derivation, the bound, the revision policy, the frozen seed, the lock, the
decode, the resume, the usage marking and the refusal messages are all
exercised over synthetic bytes. What is left when these pass is exactly two
clicks and an environment variable.

What is worth failing over, and how it is tested
------------------------------------------------
* **It must not fetch without a credential**, and must say what is missing
  rather than prompting or retrying.
* **It must not fetch an unbounded amount.** Two layers: a budget above the
  derived ceiling is refused outright, and a single stream that crosses the
  remaining budget is abandoned mid-download.
* **The hours must be derived from acceptance target 2**, not chosen, and a
  subset too short to bound it must not be frozen at all.
* **The seed must be frozen before anything is scored**, which is checked by
  asserting the record exists at the moment of the first fetch.
* **The decode must happen once**, produce exactly the format the pipeline
  reads, and be verified — including against a truncated file, which reads as
  silence rather than as an error.
* **The subset must never be trainable.** ``usage="sealed-evaluation"`` is
  checked through ``freeze_manifest.assert_usable_for`` itself, on the manifest
  the tool actually wrote.
* **It must not leak the token**, into a file, a report, an exception message
  or a subprocess's argv and environment.

Scaling the hours honestly
--------------------------
The production floor is 14.98 h of audio, which no hermetic test can write. The
``scaled`` fixture therefore moves one number — the *acceptance rate* — and
never the rule: with a pretend target of 1000 false activations per hour the
same ``2.9957 / rate`` derivation asks for 10.8 seconds instead of 14.98 hours,
so every refusal and every threshold in the module is exercised at fixture
size. The byte arithmetic stays at production values. One test asserts the
unscaled constants separately.

No test here opens a socket. ``fetch_one`` is replaced by a fake that writes
bytes, the one test that exercises the real ``fetch_one`` replaces
``urllib.request.urlopen``, and the decoder is a Python function that writes
WAV headers.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import wave
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "scripts" / "wakeword" / "COMMON_VOICE.md"

TOKEN = "hf_thisisnotarealtokenitisatestfixture"


def _load():
    spec = importlib.util.spec_from_file_location(
        "acquire_common_voice", REPO / "scripts" / "wakeword" / "acquire_common_voice.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass in the freeze_manifest module this
    # imports resolves annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


acv = _load()
fm = acv.freeze_manifest

#: A syntactically valid revision. Forty hex characters, and deliberately not
#: a real commit — nothing here resolves it.
REVISION = "0" * 39 + "a"

#: The pretend acceptance rate the fixtures are scaled to. See the module
#: docstring: 2.9957 / 1000 h is 10.784 s, so a 14.4 s target clears it the way
#: 20 h clears 14.98 h in production.
FAKE_RATE = 1000.0
FAKE_TARGET_HOURS = 0.004  # 14.4 s, against a 10.784 s requirement

#: One decoded second per clip, so hours arithmetic is readable in the tests.
CLIP_SECONDS = 1.0
CLIPS_PER_SHARD = 5


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def local_host(monkeypatch):
    """The acquisition refuses to run on a shared runner; these tests are not one.

    Cleared for every test because the suite itself runs in CI, where
    ``CI=true`` is set — the guard is about where the *audio* lands, and one
    test sets a marker back to prove the refusal still bites.
    """
    for marker in acv.CI_MARKERS:
        monkeypatch.delenv(marker, raising=False)


@pytest.fixture()
def no_token(monkeypatch):
    for name in acv.TOKEN_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def with_token(monkeypatch):
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setenv("HF_TOKEN", TOKEN)


@pytest.fixture()
def scaled(monkeypatch):
    """Move the acceptance rate, never the derivation that reads it."""
    monkeypatch.setattr(acv, "ACCEPTANCE_FA_PER_HOUR", FAKE_RATE)


def _wav_bytes(seconds: float = CLIP_SECONDS) -> bytes:
    """A WAV in exactly the format the pipeline reads."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(acv.CHANNELS)
        handle.setsampwidth(acv.SAMPLE_WIDTH)
        handle.setframerate(acv.SAMPLE_RATE)
        handle.writeframes(b"\x00\x01" * int(acv.SAMPLE_RATE * seconds))
    return buffer.getvalue()


@pytest.fixture()
def fake_decoder(monkeypatch):
    """A decoder that writes a valid 16 kHz mono WAV and counts its calls."""
    calls: list[tuple[str, str]] = []

    def resolve_decoder(explicit=None):
        return acv.Decoder(executable="/usr/bin/ffmpeg", version="ffmpeg version 6.1-fixture")

    def decode_one(decoder, source, target):
        calls.append((source.name, target.name))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_wav_bytes())

    monkeypatch.setattr(acv, "resolve_decoder", resolve_decoder)
    monkeypatch.setattr(acv, "decode_one", decode_one)
    return calls


def _tsv(
    clips: list[str], *, client_id: str = "cid-1", sentence: str = "an ordinary sentence"
) -> str:
    header = "client_id\tpath\tsentence_id\tsentence\tup_votes\tdown_votes\tlocale\tsegment"
    rows = [
        f"{client_id}\t{clip}\tsid-{index}\t{sentence}\t2\t0\ten\t"
        for index, clip in enumerate(clips)
    ]
    return "\n".join([header, *rows]) + "\n"


def _tar_bytes(clips: list[str], *, pad: int = 0) -> bytes:
    """A tar shard of MP3-named members. The bytes are not MP3; nothing decodes them.

    ``pad`` adds one non-clip member, which is what makes a shard cost more
    bytes than the audio it contributes — the real shape of the problem the
    byte budget exists for.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for clip in clips:
            payload = f"mp3 bytes for {clip}".encode() * 4
            info = tarfile.TarInfo(f"en_train/{clip}")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        if pad:
            filler = tarfile.TarInfo("en_train/other.bin")
            filler.size = pad
            tar.addfile(filler, io.BytesIO(b"\x00" * pad))
    return buffer.getvalue()


class Corpus:
    """The published bytes a fake Hub hands back, and the listing that names them."""

    def __init__(self, shards: int = 5, *, rows: str | None = None, pad: int = 0) -> None:
        self.clips = {
            f"audio/en/train/en_train_{shard}.tar": [
                f"common_voice_en_{shard}{index}.mp3" for index in range(CLIPS_PER_SHARD)
            ]
            for shard in range(shards)
        }
        self.every = [clip for clips in self.clips.values() for clip in clips]
        self.files: dict[str, bytes] = {
            "transcript/en/validated.tsv": (rows or _tsv(self.every)).encode("utf-8"),
            **{path: _tar_bytes(clips, pad=pad) for path, clips in self.clips.items()},
        }
        self.calls: list[str] = []

    def listing(self) -> tuple[str, ...]:
        return tuple(self.files)


def serve(monkeypatch, corpus: Corpus) -> Corpus:
    """Install a fake Hub for ``corpus``. No socket is opened anywhere."""

    def fetch_one(repo, revision, path, into, token, *, limit):
        assert token == TOKEN, "the fetcher was called without the caller's token"
        assert limit > 0, "the fetcher was called with no budget left"
        payload = corpus.files[path]
        if len(payload) > limit:
            raise acv.BoundExceeded(
                f"{path} is {len(payload)} bytes and only {limit} of the budget remain"
            )
        corpus.calls.append(path)
        into.mkdir(parents=True, exist_ok=True)
        target = into / Path(path).name
        if not target.exists():
            target.write_bytes(payload)
        return target

    monkeypatch.setattr(acv, "fetch_one", fetch_one)
    return corpus


@pytest.fixture()
def hub(monkeypatch):
    """A Hub that hands back the corpus's bytes and counts every request."""
    return serve(monkeypatch, Corpus())


def _run(out: Path, corpus, *, files: tuple[str, ...] | None = None, **overrides) -> dict:
    """One acquisition at fixture scale, with the fixture's own defaults."""
    values = {
        "max_bytes": acv.ceiling_bytes(FAKE_TARGET_HOURS),
        "establish_lock": True,
        "target_hours": FAKE_TARGET_HOURS,
    }
    values.update(overrides)
    return acv.acquire(out, REVISION, files or corpus.listing(), **values)


# ── the derivation, which is where the numbers come from ─────────────────────


def test_the_hours_are_derived_from_acceptance_target_two():
    """15 hours is not a round number, it is 2.9957 / 0.2."""
    assert acv.ACCEPTANCE_FA_PER_HOUR == 0.2
    assert acv.hours_required() == pytest.approx(2.9957 / 0.2)
    assert acv.hours_required() == pytest.approx(14.9785, abs=1e-4)
    # The target has to clear the floor, and its bound has to clear the target.
    assert acv.TARGET_HOURS >= acv.hours_required()
    assert acv.bound_per_hour(acv.TARGET_HOURS) <= acv.ACCEPTANCE_FA_PER_HOUR
    assert acv.bound_per_hour(acv.TARGET_HOURS) == pytest.approx(0.1498, abs=1e-4)
    # And the human recording programme's 0.05 h cannot: the whole reason this
    # corpus exists.
    assert acv.bound_per_hour(0.05) > acv.ACCEPTANCE_FA_PER_HOUR


def test_the_byte_ceiling_is_derived_from_the_hours_not_typed_in():
    """A cap chosen by hand is a cap somebody can argue with."""
    assert acv.ceiling_bytes(acv.TARGET_HOURS) == int(
        acv.TARGET_HOURS * acv.MP3_BYTES_PER_HOUR * acv.BUDGET_HEADROOM
    )
    assert acv.MP3_BYTES_PER_HOUR == acv.MP3_BITRATE_BITS_PER_SECOND // 8 * 3600
    assert acv.DECODED_BYTES_PER_HOUR == 16_000 * 2 * 3600
    # Non-vacuity: the ceiling really is far below the full English corpus,
    # which is tens of gigabytes.
    assert acv.ceiling_bytes(acv.TARGET_HOURS) < 1_000_000_000


# ── the plan, which works with nothing at all ────────────────────────────────


def test_the_plan_runs_with_no_token_and_no_network(no_token):
    plan = acv.plan()

    assert plan["dataset"] == "mozilla-foundation/common_voice_17_0"
    assert plan["license"] == "CC0-1.0"
    assert plan["token_present"] is False
    assert plan["target_hours"] == 20
    # The bound has to be derived in the artifact itself, not only in prose
    # somebody may not read.
    assert "2.9957/n" in plan["why_that_many_hours"]
    assert "14.98" in plan["why_that_many_hours"]
    assert plan["derivation"]["hours_required"] == pytest.approx(14.9785, abs=1e-4)
    assert plan["role"].startswith("recorded negatives only")
    assert len(plan["owner_action"]) == 2
    assert any("read" in step.lower() and "token" in step.lower() for step in plan["owner_action"])
    assert any("terms" in step.lower() for step in plan["owner_action"])
    # Non-vacuity: the plan must name what it will write, or "produces" is
    # decoration.
    assert any("manifest" in line for line in plan["produces"])
    assert any("lock" in line for line in plan["produces"])
    assert any("selection" in line for line in plan["produces"])
    assert any("state" in line for line in plan["produces"])


def test_the_plan_states_what_cc0_does_not_cover():
    """A licence summary that only lists permissions is a sales pitch."""
    plan = acv.plan()
    assert "terms of access" in plan["license_does_not_cover"]
    assert "commit" in plan["license_does_not_cover"]


def test_the_plan_names_the_fields_it_excludes_on_and_refuses_to_claim_more():
    plan = acv.plan()
    for field in ("client_id", "path", "sentence", "locale", "up_votes", "down_votes"):
        assert field in plan["overlap_exclusion_fields"]
    joined = " ".join(plan["identity_not_claimed"]).lower()
    assert "pseudonym" in joined
    assert "no disjointness" in joined
    assert "speech commands" in joined
    assert plan["usage"] == "sealed-evaluation"
    assert plan["forbidden_purposes"] == ["training", "validation"]


def test_the_cli_defaults_to_the_plan(capsys, no_token):
    assert acv.main([]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["license"] == "CC0-1.0"


# ── preflight: everything but the credential ─────────────────────────────────


def test_preflight_with_no_token_says_exactly_what_is_missing(no_token, tmp_path, monkeypatch):
    monkeypatch.setattr(acv, "fetch_one", lambda *a, **k: pytest.fail("preflight fetched"))
    monkeypatch.setattr(
        acv, "resolve_decoder", lambda explicit=None: acv.Decoder("/usr/bin/ffmpeg", "ffmpeg 6.1")
    )
    listing = tmp_path / "files.txt"
    listing.write_text(
        "transcript/en/validated.tsv\naudio/en/train/en_train_0.tar\n", encoding="utf-8"
    )

    report = acv.preflight(
        tmp_path / "cv", revision=REVISION, file_list=listing, target_hours=acv.TARGET_HOURS
    )

    assert report["ready"] is False
    assert report["blocked_on_owner_only"] is True
    assert report["credential"] == {
        "present": False,
        "variable": None,
        "value_read": "never printed, never written, never passed as an argument",
    }
    assert len(report["missing"]) == 1
    missing = report["missing"][0]
    assert "HF_TOKEN or HUGGING_FACE_HUB_TOKEN" in missing
    assert "https://huggingface.co/datasets/mozilla-foundation/common_voice_17_0" in missing
    assert "READ-scope" in missing
    # It still did the arithmetic it can do with no credential.
    assert report["disk"]["required_bytes"] == (
        acv.ceiling_bytes(acv.TARGET_HOURS) + int(acv.TARGET_HOURS * acv.DECODED_BYTES_PER_HOUR)
        + acv.STAGING_BYTES
    )
    assert report["disk"]["free_bytes"] > 0
    assert report["disk"]["enough"] in (True, False)
    assert report["listing"]["clip_sources"] == 1
    assert report["decoder"]["present"] is True
    assert not (tmp_path / "cv").exists(), "preflight created the work directory"


def test_preflight_exits_two_when_only_the_owner_step_is_missing(
    no_token, tmp_path, capsys, monkeypatch
):
    """Exit 2 is "you have not done the Owner step", not "it failed"."""
    monkeypatch.setattr(
        acv, "resolve_decoder", lambda explicit=None: acv.Decoder("/usr/bin/ffmpeg", "ffmpeg 6.1")
    )
    listing = tmp_path / "files.txt"
    listing.write_text(
        "transcript/en/validated.tsv\naudio/en/train/en_train_0.tar\n", encoding="utf-8"
    )

    code = acv.main(
        [
            "--preflight",
            "--out",
            str(tmp_path / "cv"),
            "--revision",
            REVISION,
            "--file-list",
            str(listing),
        ]
    )

    assert code == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "preflight"


def test_preflight_reports_a_missing_decoder_and_a_full_disk_as_its_own_failures(
    with_token, tmp_path, monkeypatch
):
    """Not exit 2: these are the operator's machine, not the Owner's account."""
    monkeypatch.setattr(acv.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        acv.shutil, "disk_usage", lambda _path: type("u", (), {"free": 1_000})()
    )
    listing = tmp_path / "files.txt"
    listing.write_text(
        "transcript/en/validated.tsv\naudio/en/train/en_train_0.tar\n", encoding="utf-8"
    )

    report = acv.preflight(tmp_path / "cv", revision=REVISION, file_list=listing)

    assert report["ready"] is False
    assert report["blocked_on_owner_only"] is False
    assert report["decoder"]["present"] is False
    assert report["disk"]["enough"] is False
    problems = " ".join(report["missing"])
    assert "ffmpeg" in problems
    assert "free space" in problems
    assert "16 kHz mono 16-bit PCM" in problems


def test_preflight_reports_what_a_resume_would_skip(with_token, tmp_path, monkeypatch):
    monkeypatch.setattr(
        acv, "resolve_decoder", lambda explicit=None: acv.Decoder("/usr/bin/ffmpeg", "ffmpeg 6.1")
    )
    out = tmp_path / "cv"
    out.mkdir()
    (out / "common_voice_en.state.json").write_text(
        json.dumps(
            {
                "schema_version": acv.STATE_SCHEMA_VERSION,
                "complete": False,
                "downloads": {"a.tar": {"bytes": 10, "sha256": "x"}},
                "clips": {"one.mp3": {"wav": "one.wav", "seconds": 1.0}},
                "hours": 0.0003,
                "decoder": "ffmpeg version 6.1-fixture",
            }
        ),
        encoding="utf-8",
    )

    report = acv.preflight(out, revision=REVISION)

    assert report["resume"] == {
        "state": "common_voice_en.state.json",
        "readable": True,
        "complete": False,
        "files_downloaded": 1,
        "clips_decoded": 1,
        "hours": 0.0003,
        "decoder": "ffmpeg version 6.1-fixture",
    }


# ── the credential boundary ──────────────────────────────────────────────────


def test_a_fetch_without_a_token_stops_and_names_the_owner_action(
    no_token, scaled, tmp_path, monkeypatch, fake_decoder
):
    def explode(*_args, **_kwargs):
        raise AssertionError("the network was reached without a credential")

    monkeypatch.setattr(acv, "fetch_one", explode)
    corpus = Corpus()

    with pytest.raises(acv.CredentialRequired) as caught:
        _run(tmp_path / "cv", corpus)

    message = str(caught.value)
    assert "https://huggingface.co/datasets/mozilla-foundation/common_voice_17_0" in message
    assert "READ scope" in message
    assert "gated" in message
    assert not (tmp_path / "cv" / "clips").exists(), "a directory was created anyway"


def test_the_cli_reports_the_missing_credential_as_its_own_exit_code(
    no_token, tmp_path, capsys, monkeypatch
):
    """Exit 2, not 1: "you have not done the Owner step" is not "it failed"."""
    monkeypatch.setattr(acv, "fetch_one", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(
        acv, "resolve_decoder", lambda explicit=None: acv.Decoder("/usr/bin/ffmpeg", "ffmpeg 6.1")
    )
    listing = tmp_path / "files.txt"
    listing.write_text(
        "transcript/en/validated.tsv\naudio/en/train/en_train_0.tar\n", encoding="utf-8"
    )

    code = acv.main(
        ["--out", str(tmp_path / "cv"), "--revision", REVISION, "--file-list", str(listing)]
    )

    assert code == 2
    assert "accept the dataset terms" in capsys.readouterr().err


def test_the_command_line_has_no_way_to_pass_a_token():
    """argv is world-readable on Linux, so there is no --token to offer."""
    with pytest.raises(SystemExit):
        acv.main(["--token", TOKEN])


# ── the bound ────────────────────────────────────────────────────────────────


def test_a_budget_above_the_derived_ceiling_is_refused(with_token, scaled, tmp_path, hub):
    """The full-archive path: assert the bound, do not merely default it."""
    ceiling = acv.ceiling_bytes(FAKE_TARGET_HOURS)

    with pytest.raises(acv.BoundExceeded) as caught:
        _run(tmp_path / "cv", hub, max_bytes=ceiling + 1)

    message = str(caught.value)
    assert str(ceiling) in message
    assert "governed ceiling" in message
    assert not hub.calls, "a byte was requested before the bound was checked"
    # And the same refusal at full-archive scale, with production constants.
    with pytest.raises(acv.BoundExceeded):
        acv.assert_within_bound(40_000_000_000, acv.TARGET_HOURS)


def test_asking_for_more_hours_cannot_raise_the_ceiling(with_token, tmp_path, hub):
    with pytest.raises(acv.Refused, match=r"MAX_TARGET_HOURS"):
        _run(tmp_path / "cv", hub, target_hours=acv.MAX_TARGET_HOURS + 1, max_bytes=1_000)
    assert not hub.calls


def test_too_few_hours_is_refused_as_a_measurement_that_cannot_measure(with_token, tmp_path, hub):
    with pytest.raises(acv.Refused, match=r"below the 14.98 h"):
        _run(tmp_path / "cv", hub, target_hours=1.0, max_bytes=1_000_000)
    assert not hub.calls


def test_one_oversized_file_is_abandoned_mid_stream(tmp_path, monkeypatch):
    """The real fetcher, with the socket replaced: no listing can pull an archive.

    This is the layer that makes the bound structural rather than arithmetical.
    A shard whose size nobody knew in advance stops at the budget, and its
    partial file is deleted so the next run cannot hash a truncated download
    into the lock.
    """
    sent: list[str] = []

    class Response:
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self._left = 8 << 20

        def read(self, size):
            chunk = b"\x00" * min(size, self._left)
            self._left -= len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def urlopen(request, timeout=None):
        sent.append(request.full_url)
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        return Response()

    monkeypatch.setattr(acv.urllib.request, "urlopen", urlopen)
    into = tmp_path / "clips"

    with pytest.raises(acv.BoundExceeded, match=r"mid-stream"):
        acv.fetch_one(acv.REPO_ID, REVISION, "audio/en/en.tar", into, TOKEN, limit=1 << 20)

    assert sent, "non-vacuity: the request really was made"
    assert list(into.iterdir()) == [], "a partial download was left behind"


def test_a_content_length_over_the_budget_is_never_started(tmp_path, monkeypatch):
    class Response:
        headers = {"Content-Length": "40000000000"}

        def read(self, _size):  # pragma: no cover - must never be reached
            raise AssertionError("the body was read despite a declared size over budget")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(acv.urllib.request, "urlopen", lambda *a, **k: Response())

    with pytest.raises(acv.BoundExceeded, match=r"not started"):
        acv.fetch_one(acv.REPO_ID, REVISION, "audio/en/en.tar", tmp_path, TOKEN, limit=1 << 20)


def test_the_budget_stops_the_fetch_and_the_shortfall_is_the_refusal(
    with_token, scaled, tmp_path, fake_decoder, monkeypatch
):
    """A budget too small to reach the hours ends as a refusal, not as a corpus.

    The shards here carry a large non-clip member, so a shard costs far more
    bytes than the seconds it contributes — which is why the budget runs out
    before the hours do.
    """
    corpus = serve(monkeypatch, Corpus(pad=40_000))

    with pytest.raises(acv.Refused) as caught:
        _run(tmp_path / "cv", corpus, max_bytes=int(FAKE_TARGET_HOURS * acv.MP3_BYTES_PER_HOUR))

    message = str(caught.value)
    assert "which is below" in message
    assert "budget exhausted at" in message
    assert "Append more shards" in message
    assert len(corpus.calls) < len(corpus.listing()), "every file was fetched despite the budget"
    assert not (tmp_path / "cv" / "common_voice_en.manifest.json").exists()


# ── the pin ──────────────────────────────────────────────────────────────────


def test_a_branch_name_is_refused_as_a_revision(with_token, tmp_path, hub):
    """"main" is not a version. The same name resolves to different bytes later."""
    for revision in ("main", "refs/convert/parquet", "v17.0", "0" * 39):
        with pytest.raises(ValueError, match=r"40-character commit sha"):
            acv.acquire(
                tmp_path / "cv",
                revision,
                hub.listing(),
                max_bytes=1_000_000,
                establish_lock=True,
                target_hours=acv.TARGET_HOURS,
            )
    assert not hub.calls


def test_the_first_fetch_has_to_ask_for_trust_on_first_use(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    """No lock and no --establish-lock is a decision, not a default."""
    with pytest.raises(acv.Refused, match=r"--establish-lock"):
        _run(tmp_path / "cv", hub, establish_lock=False)
    assert not hub.calls, "files were fetched before the lock decision was made"


def test_the_lock_records_url_size_digest_and_licence(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    out = tmp_path / "cv"
    result = _run(out, hub)

    lock = json.loads((out / "common_voice_en.lock.json").read_text(encoding="utf-8"))
    assert lock["revision"] == REVISION
    assert lock["license"] == "CC0-1.0"
    assert lock["dataset"] == acv.REPO_ID
    assert result["files_fetched"] == len(lock["files"]) == len(hub.calls)
    for path, entry in lock["files"].items():
        assert len(entry["sha256"]) == 64
        assert entry["bytes"] > 0
        assert entry["license"] == "CC0-1.0"
        assert entry["url"] == (
            f"https://huggingface.co/datasets/{acv.REPO_ID}/resolve/{REVISION}/{path}"
        )
        assert REVISION in entry["url"]


def test_a_changed_file_fails_against_the_lock(with_token, scaled, tmp_path, hub, fake_decoder):
    """The control that makes the lock mean anything."""
    out = tmp_path / "cv"
    _run(out, hub)
    fetched = sorted(hub.calls)[0]

    (out / "clips" / Path(fetched).name).write_bytes(b"republished with different bytes")

    with pytest.raises(acv.Refused, match=r"SHA-256 mismatch"):
        _run(out, hub, establish_lock=False)


# ── the seed, frozen before anything is scored ───────────────────────────────


def test_the_seed_is_recorded_before_the_first_byte_is_requested(
    with_token, scaled, tmp_path, hub, fake_decoder, monkeypatch
):
    """A seed chosen after seeing results is not a seed. So it is written first."""
    out = tmp_path / "cv"
    selection = out / "common_voice_en.selection.json"
    seen_at_first_fetch: list[bool] = []
    real_fetch = acv.fetch_one

    def watched(*args, **kwargs):
        seen_at_first_fetch.append(selection.is_file())
        return real_fetch(*args, **kwargs)

    monkeypatch.setattr(acv, "fetch_one", watched)
    _run(out, hub)

    assert seen_at_first_fetch and all(seen_at_first_fetch), "the seed was not frozen first"
    record = json.loads(selection.read_text(encoding="utf-8"))
    assert record["selection_seed"] == acv.SELECTION_SEED
    assert record["revision"] == REVISION
    assert record["listing"] == list(hub.listing())
    assert record["rules"]["locale_must_be"] == "en"
    assert record["rules"]["identity_inference"].startswith("none")
    assert " hey youtab " in record["rules"]["wake_phrase_texts_excluded"]


def test_a_different_seed_afterwards_is_refused(with_token, scaled, tmp_path, hub, fake_decoder):
    out = tmp_path / "cv"
    _run(out, hub)

    with pytest.raises(acv.Refused, match=r"selection_seed"):
        _run(out, hub, seed=acv.SELECTION_SEED + 1, establish_lock=False)


def test_the_selection_order_is_seeded_and_not_the_listing_order():
    items = [f"audio/en/train/en_train_{index}.tar" for index in range(12)]

    first = acv.ranked(items, acv.SELECTION_SEED)
    again = acv.ranked(items, acv.SELECTION_SEED)
    other = acv.ranked(items, acv.SELECTION_SEED + 1)

    assert first == again, "the same seed must give the same order"
    assert sorted(first) == sorted(items), "the order must be a permutation"
    assert first != items, "the order is the listing order, so the seed does nothing"
    assert first != other, "two seeds gave one order"


def test_a_listing_may_be_extended_but_not_reordered(with_token, scaled, tmp_path, hub):
    out = tmp_path / "cv"
    listing = hub.listing()
    selection = out / "common_voice_en.selection.json"
    out.mkdir(parents=True)

    acv.freeze_selection(
        selection,
        repo=acv.REPO_ID,
        revision=REVISION,
        seed=acv.SELECTION_SEED,
        target_hours=FAKE_TARGET_HOURS,
        listing=listing,
        exclude_ids=(),
    )
    extended = acv.freeze_selection(
        selection,
        repo=acv.REPO_ID,
        revision=REVISION,
        seed=acv.SELECTION_SEED,
        target_hours=FAKE_TARGET_HOURS,
        listing=listing + ("audio/en/train/en_train_9.tar",),
        exclude_ids=(),
    )
    assert extended["extensions"][0]["added"] == ["audio/en/train/en_train_9.tar"]

    with pytest.raises(acv.Refused, match=r"does not extend it"):
        acv.freeze_selection(
            selection,
            repo=acv.REPO_ID,
            revision=REVISION,
            seed=acv.SELECTION_SEED,
            target_hours=FAKE_TARGET_HOURS,
            listing=tuple(reversed(listing)),
            exclude_ids=(),
        )


# ── overlap exclusion, on published metadata only ────────────────────────────


def test_a_transcript_containing_the_wake_phrase_is_excluded_and_counted(
    with_token, scaled, tmp_path, fake_decoder, monkeypatch
):
    """Negative by construction has to be checked, not assumed."""
    plain = Corpus()
    rows = ["client_id\tpath\tsentence_id\tsentence\tup_votes\tdown_votes\tlocale\tsegment"]
    for index, clip in enumerate(plain.every):
        sentence = "Hey, Youtab! open the door" if index % 5 == 0 else "an ordinary sentence"
        rows.append(f"cid-{index}\t{clip}\tsid-{index}\t{sentence}\t2\t0\ten\t")
    corpus = serve(monkeypatch, Corpus(rows="\n".join(rows) + "\n"))

    result = _run(tmp_path / "cv", corpus)

    reason = "the published transcript contains the wake phrase"
    assert result["clips_excluded"].get(reason, 0) > 0
    # And nothing bearing the phrase reached the corpus.
    excluded_clips = {clip for index, clip in enumerate(corpus.every) if index % 5 == 0}
    decoded = {call[0] for call in fake_decoder}
    assert decoded, "non-vacuity: something was decoded"
    assert not (decoded & excluded_clips)


def test_near_miss_text_is_deliberately_kept():
    """Filtering the confusable phrases out would flatter the measurement."""
    candidates = {
        "a.mp3": acv.Candidate("a.mp3", "cid", "hey youtube is loud", "en", "2", "0", "s1"),
        "b.mp3": acv.Candidate("b.mp3", "cid", "hey you tab that page", "en", "2", "0", "s2"),
    }
    phrase_texts = acv.wake_phrase_texts()

    assert (
        acv.ineligibility("a.mp3", candidates, exclude_ids=frozenset(), phrase_texts=phrase_texts)
        is None
    )
    # "hey you tab" *is* a spelling of the wake phrase, so it goes.
    assert (
        acv.ineligibility("b.mp3", candidates, exclude_ids=frozenset(), phrase_texts=phrase_texts)
        == "the published transcript contains the wake phrase"
    )
    assert "kept" in acv.selection_rules(())["near_miss_text_kept"] or True
    assert "deliberately" in acv.selection_rules(())["near_miss_text_kept"]


def test_client_ids_are_excluded_by_exact_match_on_the_published_field():
    candidates = {
        "a.mp3": acv.Candidate("a.mp3", "held-out-1", "a sentence", "en", "2", "0", "s1"),
        "b.mp3": acv.Candidate("b.mp3", "kept-1", "a sentence", "en", "2", "0", "s2"),
    }
    excluded = frozenset({"held-out-1"})

    assert (
        acv.ineligibility("a.mp3", candidates, exclude_ids=excluded, phrase_texts=())
        == "client_id is on the held-out list"
    )
    assert acv.ineligibility("b.mp3", candidates, exclude_ids=excluded, phrase_texts=()) is None
    # The record pins which list was used without carrying the ids around.
    rules = acv.selection_rules(("held-out-1",))
    assert rules["excluded_client_id_count"] == 1
    assert rules["excluded_client_id_sha256"] == hashlib.sha256(b"held-out-1").hexdigest()


def test_the_other_published_rules_each_have_a_countable_reason():
    candidates = {
        "wrong_locale.mp3": acv.Candidate("wrong_locale.mp3", "c", "s", "de", "2", "0", "s"),
        "no_text.mp3": acv.Candidate("no_text.mp3", "c", "", "en", "2", "0", "s"),
        "voted_down.mp3": acv.Candidate("voted_down.mp3", "c", "s", "en", "1", "3", "s"),
    }
    reasons = {
        clip: acv.ineligibility(clip, candidates, exclude_ids=frozenset(), phrase_texts=())
        for clip in candidates
    }

    assert reasons["wrong_locale.mp3"] == "locale is 'de', not 'en'"
    assert "wake phrase cannot be ruled out" in reasons["no_text.mp3"]
    assert reasons["voted_down.mp3"] == "the corpus's own listeners did not validate it"
    assert (
        acv.ineligibility("absent.mp3", candidates, exclude_ids=frozenset(), phrase_texts=())
        == "no metadata row"
    )


def test_metadata_without_the_required_columns_is_refused(
    with_token, scaled, tmp_path, fake_decoder, monkeypatch
):
    corpus = serve(monkeypatch, Corpus(rows="path\tsentence\nfoo.mp3\thello\n"))

    with pytest.raises(acv.Refused, match=r"client_id"):
        _run(tmp_path / "cv", corpus)


def test_a_listing_with_no_metadata_is_refused():
    with pytest.raises(acv.Refused, match=r"no \.tsv"):
        acv.classify(("audio/en/train/en_train_0.tar",))
    with pytest.raises(acv.Refused, match=r"no clip source"):
        acv.classify(("transcript/en/validated.tsv",))
    with pytest.raises(acv.Refused, match=r"neither corpus metadata"):
        acv.classify(("transcript/en/validated.tsv", "audio/en/train/en_train_0.parquet"))


# ── the decode, once ─────────────────────────────────────────────────────────


def test_the_decode_happens_once_and_a_rerun_verifies_instead(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    out = tmp_path / "cv"
    first = _run(out, hub)
    fetched, decoded = len(hub.calls), len(fake_decoder)
    assert fetched and decoded, "non-vacuity: the first run really did the work"

    again = _run(out, hub, establish_lock=False)

    assert len(hub.calls) == fetched, "a verified download was fetched again"
    assert len(fake_decoder) == decoded, "a verified clip was decoded again"
    assert again["manifest_sha256"] == first["manifest_sha256"]
    assert again["clips_decoded_this_run"] == 0
    assert again["files_fetched_this_run"] == 0
    assert again["resumed"] is True


def test_a_decode_in_the_wrong_format_is_refused(with_token, scaled, tmp_path, hub, monkeypatch):
    """44.1 kHz stereo would reach the feature extractor as a different signal."""
    monkeypatch.setattr(
        acv, "resolve_decoder", lambda explicit=None: acv.Decoder("/usr/bin/ffmpeg", "ffmpeg 6.1")
    )

    def decode_stereo(decoder, source, target):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(44_100)
            handle.writeframes(b"\x00\x01\x00\x01" * 4410)
        target.write_bytes(buffer.getvalue())

    monkeypatch.setattr(acv, "decode_one", decode_stereo)

    with pytest.raises(acv.Refused, match=r"44100 Hz / 2 ch"):
        _run(tmp_path / "cv", hub)


def test_a_truncated_decode_is_refused_rather_than_read_as_silence(tmp_path):
    full = _wav_bytes()
    truncated = tmp_path / "clipped.wav"
    truncated.write_bytes(full[: len(full) // 2])

    with pytest.raises(acv.Refused, match=r"truncated decode reads as silence"):
        acv.wav_seconds(truncated)

    intact = tmp_path / "intact.wav"
    intact.write_bytes(full)
    assert acv.wav_seconds(intact) == pytest.approx(CLIP_SECONDS)


def test_a_different_decoder_on_a_resume_is_refused(
    with_token, scaled, tmp_path, hub, fake_decoder, monkeypatch
):
    out = tmp_path / "cv"
    _run(out, hub)

    monkeypatch.setattr(
        acv,
        "resolve_decoder",
        lambda explicit=None: acv.Decoder("/usr/bin/ffmpeg", "ffmpeg version 7.0-other"),
    )
    with pytest.raises(acv.Refused, match=r"decoded by each"):
        _run(out, hub, establish_lock=False)


def test_the_decoder_command_line_is_the_pipeline_format(tmp_path, monkeypatch, with_token):
    """The flags are the contract with read_wav16, so they are asserted."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env")
        Path(argv[-1]).write_bytes(_wav_bytes())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(acv.subprocess, "run", fake_run)
    decoder = acv.Decoder(executable="/usr/bin/ffmpeg", version="ffmpeg version 6.1")
    acv.decode_one(decoder, tmp_path / "a.mp3", tmp_path / "a.wav")

    argv = captured["argv"]
    assert argv[0] == "/usr/bin/ffmpeg"
    for flag, value in (("-ar", "16000"), ("-ac", "1"), ("-c:a", "pcm_s16le"), ("-f", "wav")):
        assert argv[argv.index(flag) + 1] == value
    assert "-map_metadata" in argv and "+bitexact" in argv
    assert (tmp_path / "a.wav").is_file()
    assert not list(tmp_path.glob("*.partial"))


# ── what it produces, and what that marking forbids ──────────────────────────


def test_the_result_is_frozen_and_can_never_be_trained_on(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    """The requirement, through the gate the consumer actually calls."""
    out = tmp_path / "cv"
    result = _run(out, hub)

    manifest = fm.load(out / "common_voice_en.manifest.json")
    assert manifest["usage"] == "sealed-evaluation"
    assert manifest["split"] == "evaluate"
    assert manifest["file_count"] == result["clips_selected"] > 0
    assert "CC0-1.0" in manifest["note"]
    assert manifest["dataset"].startswith("mozilla-foundation/common_voice_17_0@")
    assert (out / "common_voice_en.manifest.SHA256SUMS").is_file()

    # build_human_dataset.py calls exactly this before it reads a clip.
    with pytest.raises(fm.SealedDatasetError, match=r"cannot be used for training"):
        fm.assert_usable_for(manifest, "training")
    with pytest.raises(fm.SealedDatasetError):
        fm.assert_usable_for(manifest, "validation")
    fm.assert_usable_for(manifest, "evaluation")
    # And the acquisition re-checks that on the record it wrote.
    acv.assert_never_trainable(manifest)


def test_the_usage_check_has_teeth(with_token, scaled, tmp_path, hub, fake_decoder):
    """A manifest marked trainable must fail the post-condition, not pass it."""
    out = tmp_path / "cv"
    _run(out, hub)
    manifest = fm.load(out / "common_voice_en.manifest.json")

    with pytest.raises(acv.Refused, match=r"exists to bound the false-activation rate"):
        acv.assert_never_trainable({**manifest, "usage": "training"})
    with pytest.raises(acv.Refused):
        acv.assert_never_trainable({**manifest, "usage": "validation"})


def test_the_report_records_the_version_the_hours_and_the_residual_risk(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    result = _run(tmp_path / "cv", hub)

    assert result["revision"] == REVISION
    assert result["license"] == "CC0-1.0"
    assert result["selection_seed"] == acv.SELECTION_SEED
    assert result["hours_acquired"] >= result["hours_required"] > 0
    assert result["clean_run_bound_per_hour"] <= FAKE_RATE
    assert result["decoder"] == "ffmpeg version 6.1-fixture"
    assert result["clips_selected"] == len(fake_decoder)
    assert "client_id" in result["metadata_fields_used"]
    assert "sentence" in result["metadata_fields_used"]
    assert len(result["identity_not_claimed"]) == len(acv.IDENTITY_NOT_CLAIMED)
    # A report is attachable to a governance record, so it carries no host path.
    text = json.dumps(result)
    assert str(tmp_path) not in text
    assert "/mnt/" not in text and "C:\\" not in text


def test_the_hours_stop_the_download_before_the_listing_runs_out(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    """The bound is not only bytes: enough is enough."""
    result = _run(tmp_path / "cv", hub)

    assert len(hub.calls) < len(hub.listing()), "every shard was fetched regardless of the target"
    assert result["hours_acquired"] * 3600 >= acv.hours_required() * 3600
    assert result["clips_selected"] <= CLIPS_PER_SHARD * len(hub.clips)


# ── resumability ─────────────────────────────────────────────────────────────


def test_an_interrupted_acquisition_resumes_without_redoing_verified_work(
    with_token, scaled, tmp_path, hub, monkeypatch
):
    """The interruption is a decoder that dies partway, which is the real case."""
    monkeypatch.setattr(
        acv, "resolve_decoder", lambda explicit=None: acv.Decoder("/usr/bin/ffmpeg", "ffmpeg 6.1")
    )
    decoded: list[str] = []
    stop_after = 6

    def decode(decoder, source, target):
        if len(decoded) == stop_after and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt("the operator stopped it")
        decoded.append(source.name)
        target.write_bytes(_wav_bytes())

    interrupted: list[bool] = []
    monkeypatch.setattr(acv, "decode_one", decode)
    out = tmp_path / "cv"

    with pytest.raises(KeyboardInterrupt):
        _run(out, hub)

    # Nothing reads as complete: no manifest, and the state says so in writing.
    assert not (out / "common_voice_en.manifest.json").exists()
    state = json.loads((out / "common_voice_en.state.json").read_text(encoding="utf-8"))
    assert state["complete"] is False
    interrupted_fetches = len(hub.calls)
    assert interrupted_fetches > 1, "non-vacuity: the first run got somewhere"
    assert len(decoded) == stop_after
    # The lock was recorded as the files arrived, so the resume has something
    # to verify against even though the first run never reached the end.
    lock = json.loads((out / "common_voice_en.lock.json").read_text(encoding="utf-8"))
    assert len(lock["files"]) == interrupted_fetches

    result = _run(out, hub)

    assert result["hours_acquired"] > 0
    assert (out / "common_voice_en.manifest.json").is_file()
    assert json.loads(
        (out / "common_voice_en.state.json").read_text(encoding="utf-8")
    )["complete"] is True
    # Verified bytes were not fetched again, and no clip was decoded twice —
    # the six the first run finished are re-verified from the tree instead.
    assert len(set(hub.calls)) == len(hub.calls), "a file was downloaded twice"
    assert len(hub.calls) > interrupted_fetches, "the resume never reached a new shard"
    assert len(decoded) == len(set(decoded)), "a clip was decoded twice"
    assert result["clips_decoded_this_run"] == len(decoded) - stop_after


def test_a_short_subset_is_completed_by_extending_the_listing(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    """The documented remedy for a shortfall, end to end."""
    out = tmp_path / "cv"
    tsv, *shards = hub.listing()

    with pytest.raises(acv.Refused, match=r"Append more shards"):
        _run(out, hub, files=(tsv, shards[0], shards[1]))
    assert not (out / "common_voice_en.manifest.json").exists()
    decoded_before = len(fake_decoder)
    assert decoded_before == 2 * CLIPS_PER_SHARD, "the short run decoded what it could"

    result = _run(out, hub, files=(tsv, *shards[:4]))

    assert result["hours_acquired"] * 3600 >= acv.hours_required() * 3600
    assert (out / "common_voice_en.manifest.json").is_file()
    assert result["clips_decoded_this_run"] == len(fake_decoder) - decoded_before
    selection = json.loads((out / "common_voice_en.selection.json").read_text(encoding="utf-8"))
    assert selection["extensions"], "the extension was not recorded"
    assert selection["extensions"][0]["added"] == list(shards[2:4])
    assert selection["selection_seed"] == acv.SELECTION_SEED, "the seed survived the extension"


def test_a_manifest_beside_an_unfinished_state_is_refused(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    """A partial state must never read as complete — from either side."""
    out = tmp_path / "cv"
    _run(out, hub)
    state_path = out / "common_voice_en.state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["complete"] = False
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(acv.Refused, match=r"never finished"):
        _run(out, hub, establish_lock=False)


def test_a_stray_wav_in_the_decoded_tree_is_refused(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    """The manifest is frozen over the tree, so the tree has to be the selection."""
    out = tmp_path / "cv"
    _run(out, hub)
    (out / "decoded" / "somebody_elses_recording.wav").write_bytes(_wav_bytes())

    with pytest.raises(acv.Refused, match=r"would\s+not have taken it"):
        _run(out, hub, establish_lock=False)
    # And again, identically: a refusal over the tree must not demote the
    # finished record into "never finished", which would hide the real problem.
    with pytest.raises(acv.Refused, match=r"would\s+not have taken it"):
        _run(out, hub, establish_lock=False)


def test_decoded_bytes_that_changed_after_verification_are_refused(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    out = tmp_path / "cv"
    _run(out, hub)
    state = json.loads((out / "common_voice_en.state.json").read_text(encoding="utf-8"))
    victim = out / "decoded" / next(iter(state["clips"].values()))["wav"]
    victim.write_bytes(_wav_bytes(CLIP_SECONDS * 2))

    with pytest.raises(acv.Refused, match=r"no longer matches the digest"):
        _run(out, hub, establish_lock=False)


def test_a_listed_file_that_is_not_a_shard_is_refused(
    with_token, scaled, tmp_path, fake_decoder, monkeypatch
):
    """Its bytes matched the lock, so this is a finding about the listing."""
    corpus = Corpus(shards=1)
    corpus.files["audio/en/train/en_train_0.tar"] = b"this is not a tar archive"
    serve(monkeypatch, corpus)

    with pytest.raises(acv.Refused, match=r"does not read as a tar archive"):
        _run(tmp_path / "cv", corpus)


def test_a_damaged_record_is_refused_rather_than_guessed_at(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    """A machine that lost power mid-write is a normal event, not a traceback."""
    out = tmp_path / "cv"
    _run(out, hub)
    (out / "common_voice_en.state.json").write_text("{ truncated", encoding="utf-8")

    with pytest.raises(acv.Refused, match=r"not readable JSON"):
        _run(out, hub, establish_lock=False)


# ── the raw data stays local ──────────────────────────────────────────────────


def test_acquiring_into_the_repository_is_refused(with_token, scaled, hub):
    with pytest.raises(acv.Refused, match=r"refusing to acquire into the repository"):
        _run(REPO / "scripts" / "wakeword" / "cv", hub)
    assert not hub.calls


def test_acquiring_on_a_shared_runner_is_refused(with_token, scaled, tmp_path, hub, monkeypatch):
    """A CI workspace is an artifact and its stdout is a public log."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    with pytest.raises(acv.Refused, match=r"shared runner"):
        _run(tmp_path / "cv", hub)
    assert not hub.calls


# ── the token reaches nothing ────────────────────────────────────────────────


def test_the_token_reaches_nothing_that_is_written_to_disk(
    with_token, scaled, tmp_path, hub, fake_decoder
):
    """A credential in a manifest is a credential in a backup, forever."""
    out = tmp_path / "cv"
    _run(out, hub)

    written = [path for path in out.rglob("*") if path.is_file()]
    assert len(written) >= 8, written  # clips + decoded + lock + selection + state + manifest
    for path in written:
        assert TOKEN not in path.read_bytes().decode("utf-8", "ignore"), f"token in {path.name}"


def test_the_token_reaches_no_line_of_output(
    with_token, scaled, tmp_path, hub, fake_decoder, capsys
):
    """Every mode, including the failing ones, over stdout and stderr."""
    listing = tmp_path / "files.txt"
    listing.write_text("\n".join(hub.listing()) + "\n", encoding="utf-8")
    out = tmp_path / "cv"

    codes = [
        acv.main(["--plan"]),
        acv.main(["--preflight", "--out", str(out), "--revision", REVISION,
                  "--file-list", str(listing)]),
        acv.main(["--out", str(out), "--revision", "main", "--file-list", str(listing)]),
    ]
    captured = capsys.readouterr()

    assert codes[0] == 0 and codes[2] == 1
    assert captured.out and captured.err, "non-vacuity: there was output to inspect"
    for stream in (captured.out, captured.err):
        assert TOKEN not in stream


def test_a_token_in_an_exception_message_is_scrubbed_before_it_is_printed(
    with_token, scaled, tmp_path, hub, fake_decoder, capsys, monkeypatch
):
    """The realistic leak: a library that puts the request in its error text."""

    def leaky(*_args, **_kwargs):
        raise acv.Refused(f"GET failed with Authorization: Bearer {TOKEN}")

    monkeypatch.setattr(acv, "fetch_one", leaky)
    listing = tmp_path / "files.txt"
    listing.write_text("\n".join(hub.listing()) + "\n", encoding="utf-8")

    code = acv.main(
        [
            "--out",
            str(tmp_path / "cv"),
            "--revision",
            REVISION,
            "--file-list",
            str(listing),
            "--establish-lock",
            "--target-hours",
            str(FAKE_TARGET_HOURS),
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert TOKEN not in captured.err
    assert "<redacted credential>" in captured.err


def test_writing_a_credential_to_a_file_is_refused_outright(with_token, tmp_path):
    """The backstop behind scrubbing: a leak fails loudly instead of quietly."""
    with pytest.raises(acv.Refused, match=r"refusing to write or print"):
        acv._write_json(tmp_path / "leak.json", {"note": f"token {TOKEN}"})
    assert not (tmp_path / "leak.json").exists()

    with pytest.raises(acv.Refused):
        acv.assert_no_credential(f"Bearer {TOKEN}", "a log line")
    acv.assert_no_credential("nothing sensitive here", "a log line")


def test_the_token_is_in_no_subprocess_argv_or_environment(with_token, tmp_path, monkeypatch):
    """argv is world-readable on Linux, and a child that cannot see it cannot print it."""
    seen: list[dict] = []

    def fake_run(argv, **kwargs):
        seen.append({"argv": list(argv), "env": dict(kwargs.get("env") or {})})
        if "-version" in argv:
            return subprocess.CompletedProcess(argv, 0, "ffmpeg version 6.1-fixture\n", "")
        Path(argv[-1]).write_bytes(_wav_bytes())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(acv.subprocess, "run", fake_run)
    monkeypatch.setattr(acv.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    decoder = acv.resolve_decoder()
    acv.decode_one(decoder, tmp_path / "a.mp3", tmp_path / "a.wav")

    assert len(seen) == 2, "non-vacuity: both subprocesses were captured"
    for call in seen:
        assert TOKEN not in " ".join(call["argv"])
        for name in acv.TOKEN_VARS:
            assert name not in call["env"], f"{name} was passed to a child process"
        assert TOKEN not in json.dumps(call["env"])


# ── the document and the script must agree ───────────────────────────────────


def test_the_plan_document_matches_the_tool():
    text = DOC.read_text(encoding="utf-8")
    assert acv.REPO_ID in text
    assert "CC0-1.0" in text
    assert "20 hours" in text
    # The derivation, in the document, with the numbers the module computes.
    assert "14.98" in text
    assert "2.9957" in text
    assert "0.1498" in text or "0.15" in text
    # The Owner action, stated once in each place; drift here means somebody
    # follows instructions that no longer match the refusal message.
    for var in acv.TOKEN_VARS:
        assert var in text
    assert "read" in text.lower() and "scope" in text.lower()
    # And the honesty constraints: the corpus layout and the full-corpus size
    # are explicitly NOT asserted, because nothing has fetched them.
    assert "not asserted here" in text
    assert "Nothing here has been downloaded" in text or "nothing has been fetched" in text


def test_the_document_states_the_marking_the_decode_and_the_residual_risk():
    text = DOC.read_text(encoding="utf-8")
    assert "sealed-evaluation" in text
    assert "--preflight" in text
    assert "resum" in text.lower()
    assert acv.DECODER_NAME in text
    assert "16 kHz mono 16-bit" in text
    # Every field the exclusion reads has to be named in the document too.
    for field in acv.REQUIRED_TSV_FIELDS + acv.OPTIONAL_TSV_FIELDS:
        assert field in text, f"{field} is used by the tool and unmentioned in the doc"
    for phrase in ("Residual risk", "pseudonym"):
        assert phrase in text
