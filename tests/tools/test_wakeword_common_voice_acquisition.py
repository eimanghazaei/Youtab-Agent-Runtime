"""The Common Voice path must be complete except for the one Owner action.

Background
----------
The corpus is gated: reaching it needs a person to accept the dataset terms
under their own account and to mint a read-scope token. That is the only step
this repository cannot do — and it is precisely the step that tends to swallow
the rest of the design, because "blocked on a credential" reads the same
whether the tooling is finished or has never been written.

So the tooling is finished and tested here, with no network and no token: the
bound, the revision policy, the lock, the manifest and the refusal messages are
all exercised over synthetic bytes. What is left when these pass is exactly two
clicks and an environment variable.

Three things are worth failing over, and each has a test:

* **It must not fetch without a credential**, and must say what is missing
  rather than prompting or retrying.
* **It must not fetch an unbounded amount.** A corpus acquisition that quietly
  becomes tens of gigabytes is the failure the whole bound exists against.
* **It must not leak the token**, into a file, a manifest or an error message.

No test here opens a socket. ``fetch_one`` is replaced by a fake that writes
bytes, and one test asserts the real one is never reached.
"""

from __future__ import annotations

import importlib.util
import json
import sys
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


@pytest.fixture()
def no_token(monkeypatch):
    for name in acv.TOKEN_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def with_token(monkeypatch):
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    monkeypatch.setenv("HF_TOKEN", TOKEN)


@pytest.fixture()
def fake_hub(monkeypatch):
    """A Hub that hands back deterministic bytes and counts the calls."""
    calls: list[tuple[str, str, str]] = []

    def fetch_one(repo, revision, path, into, token):
        assert token == TOKEN, "the fetcher was called without the caller's token"
        calls.append((repo, revision, path))
        into.mkdir(parents=True, exist_ok=True)
        target = into / Path(path).name
        if not target.exists():
            target.write_bytes(f"audio bytes for {path}".encode() * 512)
        return target

    monkeypatch.setattr(acv, "fetch_one", fetch_one)
    return calls


def _files(count: int = 4) -> tuple[str, ...]:
    return tuple(f"audio/en/train/en_train_{i}.tar" for i in range(count))


# ── the plan, which works with nothing at all ────────────────────────────────


def test_the_plan_runs_with_no_token_and_no_network(no_token):
    plan = acv.plan()

    assert plan["dataset"] == "mozilla-foundation/common_voice_17_0"
    assert plan["license"] == "CC0-1.0"
    assert plan["token_present"] is False
    assert plan["target_hours"] == 20
    # The bound has to be derived in the artifact itself, not only in prose
    # somebody may not read.
    assert "3/n" in plan["why_that_many_hours"]
    assert plan["role"].startswith("recorded negatives only")
    assert len(plan["owner_action"]) == 2
    assert any("read" in step.lower() and "token" in step.lower() for step in plan["owner_action"])
    assert any("terms" in step.lower() for step in plan["owner_action"])
    # Non-vacuity: the plan must name what it will write, or "produces" is
    # decoration.
    assert any("manifest" in line for line in plan["produces"])
    assert any("lock" in line for line in plan["produces"])


def test_the_plan_states_what_cc0_does_not_cover():
    """A licence summary that only lists permissions is a sales pitch."""
    plan = acv.plan()
    assert "terms of access" in plan["license_does_not_cover"]
    assert "commit" in plan["license_does_not_cover"]


def test_the_cli_defaults_to_the_plan(capsys, no_token):
    assert acv.main([]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["license"] == "CC0-1.0"


# ── the credential boundary ──────────────────────────────────────────────────


def test_a_fetch_without_a_token_stops_and_names_the_owner_action(no_token, tmp_path, monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("the network was reached without a credential")

    monkeypatch.setattr(acv, "fetch_one", explode)

    with pytest.raises(acv.CredentialRequired) as caught:
        acv.acquire(tmp_path / "cv", REVISION, _files(), max_bytes=1_000_000, establish_lock=True)

    message = str(caught.value)
    assert "https://huggingface.co/datasets/mozilla-foundation/common_voice_17_0" in message
    assert "READ scope" in message or "READ" in message
    assert "gated" in message
    assert not (tmp_path / "cv" / "clips").exists(), "a directory was created anyway"


def test_the_cli_reports_the_missing_credential_as_its_own_exit_code(
    no_token, tmp_path, capsys, monkeypatch
):
    """Exit 2, not 1: "you have not done the Owner step" is not "it failed"."""
    monkeypatch.setattr(acv, "fetch_one", lambda *a, **k: pytest.fail("network"))
    listing = tmp_path / "files.txt"
    listing.write_text("audio/en/train/en_train_0.tar\n", encoding="utf-8")

    code = acv.main(
        ["--out", str(tmp_path / "cv"), "--revision", REVISION, "--file-list", str(listing)]
    )

    assert code == 2
    assert "accept the dataset terms" in capsys.readouterr().err


# ── the pin ──────────────────────────────────────────────────────────────────


def test_a_branch_name_is_refused_as_a_revision(with_token, tmp_path):
    """"main" is not a version. The same name resolves to different bytes later."""
    for revision in ("main", "refs/convert/parquet", "v17.0", "0" * 39):
        with pytest.raises(ValueError, match=r"40-character commit sha"):
            acv.acquire(
                tmp_path / "cv", revision, _files(), max_bytes=1_000_000, establish_lock=True
            )


def test_the_first_fetch_has_to_ask_for_trust_on_first_use(with_token, fake_hub, tmp_path):
    """No lock and no --establish-lock is a decision, not a default."""
    with pytest.raises(acv.CredentialRequired, match=r"--establish-lock"):
        acv.acquire(tmp_path / "cv", REVISION, _files(), max_bytes=10_000_000, establish_lock=False)
    assert not fake_hub, "files were fetched before the lock decision was made"


def test_establishing_the_lock_records_every_digest(with_token, fake_hub, tmp_path):
    out = tmp_path / "cv"
    result = acv.acquire(out, REVISION, _files(), max_bytes=10_000_000, establish_lock=True)

    # Non-vacuity: four files really were fetched and hashed.
    assert result["files_fetched"] == 4
    assert len(fake_hub) == 4
    assert result["bytes_fetched"] > 0

    lock = json.loads((out / "common_voice_en.lock.json").read_text(encoding="utf-8"))
    assert lock["revision"] == REVISION
    assert lock["license"] == "CC0-1.0"
    assert len(lock["files"]) == 4
    assert all(len(digest) == 64 for digest in lock["files"].values())


def test_a_changed_file_fails_against_the_lock(with_token, fake_hub, tmp_path):
    """The control that makes the lock mean anything."""
    out = tmp_path / "cv"
    acv.acquire(out, REVISION, _files(), max_bytes=10_000_000, establish_lock=True)

    (out / "clips" / "en_train_2.tar").write_bytes(b"republished with different bytes")

    with pytest.raises(RuntimeError, match=r"SHA-256 mismatch"):
        acv.acquire(out, REVISION, _files(), max_bytes=10_000_000, establish_lock=False)


def test_an_unchanged_corpus_verifies_against_the_lock(with_token, fake_hub, tmp_path):
    out = tmp_path / "cv"
    first = acv.acquire(out, REVISION, _files(), max_bytes=10_000_000, establish_lock=True)
    again = acv.acquire(out, REVISION, _files(), max_bytes=10_000_000, establish_lock=False)
    assert again["manifest_sha256"] == first["manifest_sha256"]


# ── the bound ────────────────────────────────────────────────────────────────


def test_the_byte_budget_stops_the_fetch(with_token, fake_hub, tmp_path):
    """Eight files offered, two fetched, six never requested.

    The budget is checked before each request, so the loop stops at the first
    file that would begin past it. The overshoot is bounded by one file's size
    and no more — nothing knows a shard's size before asking for it.
    """
    out = tmp_path / "cv"
    one_file = len(b"audio bytes for audio/en/train/en_train_0.tar" * 512)

    result = acv.acquire(out, REVISION, _files(8), max_bytes=one_file * 2, establish_lock=True)

    assert result["files_fetched"] == 2, "the budget did not stop the loop where expected"
    assert len(fake_hub) == 2, "files were requested past the budget"
    assert result["bytes_fetched"] <= one_file * 2 + one_file


# ── what it produces ─────────────────────────────────────────────────────────


def test_the_result_is_frozen_and_marked_as_training_data(with_token, fake_hub, tmp_path):
    out = tmp_path / "cv"
    acv.acquire(out, REVISION, _files(), max_bytes=10_000_000, establish_lock=True)

    manifest = fm.load(out / "common_voice_en.manifest.json")
    assert manifest["usage"] == "training"
    assert manifest["split"] == "train"
    assert manifest["file_count"] == 4
    assert "CC0-1.0" in manifest["note"]
    assert manifest["dataset"].startswith("mozilla-foundation/common_voice_17_0@")

    # A set the model is fitted on cannot also be the measurement.
    fm.assert_usable_for(manifest, "training")
    with pytest.raises(ValueError, match=r"does not permit evaluation"):
        fm.assert_usable_for(manifest, "evaluation")

    assert (out / "common_voice_en.manifest.SHA256SUMS").is_file()


def test_the_token_reaches_nothing_that_is_written_to_disk(with_token, fake_hub, tmp_path):
    """A credential in a manifest is a credential in a backup, forever."""
    out = tmp_path / "cv"
    acv.acquire(out, REVISION, _files(), max_bytes=10_000_000, establish_lock=True)

    written = [p for p in out.rglob("*") if p.is_file()]
    assert len(written) >= 7, written  # non-vacuity: clips + lock + manifest + sums
    for path in written:
        assert TOKEN not in path.read_bytes().decode("utf-8", "ignore"), f"token found in {path.name}"


# ── the document and the script must agree ───────────────────────────────────


def test_the_plan_document_matches_the_tool():
    text = DOC.read_text(encoding="utf-8")
    assert acv.REPO_ID in text
    assert "CC0-1.0" in text
    assert "20 hours" in text
    # The Owner action, stated once in each place; drift here means somebody
    # follows instructions that no longer match the refusal message.
    for var in acv.TOKEN_VARS:
        assert var in text
    assert "read" in text.lower() and "scope" in text.lower()
    # And the honesty constraints: the corpus layout and the full-corpus size
    # are explicitly NOT asserted, because nothing has fetched them.
    assert "not asserted here" in text
    assert "Nothing here has been downloaded" in text or "nothing has been fetched" in text
