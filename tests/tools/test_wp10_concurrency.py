"""WP-10: two imports against one data root must not both publish.

Hermetic; synthetic audio under tmp_path only. Speaker-keyed strings are
assembled from fragments so this file does not trip the commit gate.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "wakeword"))

import import_speaker as imp  # noqa: E402
import speaker_recording_spec as spec  # noqa: E402

from tests.tools.test_wakeword_speaker_import import (  # noqa: E402
    SEALED_LABEL,
    TRAIN_LABEL,
    build_package_submission,
    write_checksums,
)


@pytest.fixture(scope="module")
def sharing_one_take(tmp_path_factory):
    """Two complete submissions that share one byte-identical recording.

    The realistic accident rather than an attack: a coordinator copies a take
    into the wrong folder, or a speaker returns a file they were sent as an
    example. One of the two speakers is sealed.
    """
    base = tmp_path_factory.mktemp("subs")
    train = build_package_submission(base / TRAIN_LABEL, TRAIN_LABEL)
    sealed = build_package_submission(base / SEALED_LABEL, SEALED_LABEL)
    section = spec.POSITIVE_SECTIONS[0]
    rel = "/".join(
        (
            spec.ORIGINALS_DIR,
            section.directory,
            spec.WAKE_PHRASE_SLUG + "_" + section.condition + "_001.wav",
        )
    )
    shutil.copy2(train / rel, sealed / rel)
    write_checksums(sealed)
    return train, sealed, hashlib.sha256((train / rel).read_bytes()).hexdigest()


def _digests(into: Path, label: str) -> set[str]:
    body = json.loads((into / ("speaker_" + label) / imp.MANIFEST_FILENAME).read_text(encoding="utf-8"))
    return {entry["sha256"] for entry in body["files"]}


def test_a_shared_take_cannot_be_published_by_the_second_import(tmp_path, sharing_one_take):
    """Both operators plan, then both publish. Only the first may succeed.

    The reuse check reads every manifest in the data root and plan reads it
    once, so two imports planned before either published each saw a root without
    the other. Nothing between them refused the shared take -- and because one of
    the two is sealed, that is the loss no later cleanup reverses: deleting a
    file does not make the sealed voice unseen again.
    """
    train, sealed, shared = sharing_one_take
    into = tmp_path / "data"
    into.mkdir()

    plan_train = imp.plan(TRAIN_LABEL, train, into)
    plan_sealed = imp.plan(SEALED_LABEL, sealed, into)
    assert plan_train.ok, sorted(c.name for c in plan_train.failures)
    assert plan_sealed.ok, sorted(c.name for c in plan_sealed.failures)

    imp.execute(plan_train, into)
    with pytest.raises(imp.Refused) as refusal:
        imp.execute(plan_sealed, into)
    assert "reuse" in str(refusal.value)

    assert shared in _digests(into, TRAIN_LABEL)
    assert not (into / ("speaker_" + SEALED_LABEL) / imp.MANIFEST_FILENAME).is_file()


def test_a_held_lock_refuses_a_second_publisher(tmp_path, sharing_one_take):
    train, _sealed, _shared = sharing_one_take
    into = tmp_path / "data"
    into.mkdir()
    (into / imp.LOCK_FILENAME).write_text("99999\n", encoding="utf-8")

    state = imp.plan(TRAIN_LABEL, train, into)
    assert state.ok, sorted(c.name for c in state.failures)
    with pytest.raises(imp.Refused) as refusal:
        imp.execute(state, into)
    assert imp.LOCK_FILENAME in str(refusal.value)
    assert not (into / ("speaker_" + TRAIN_LABEL) / imp.MANIFEST_FILENAME).is_file()


def test_concurrent_imports_of_one_speaker_raise_only_refusals(tmp_path, sharing_one_take):
    """Whatever loses the race loses it as a refusal, not as a traceback.

    One shared .writing temporary name means the winner consumes the file the
    loser staged, and the loser then fails inside os.replace after its caller
    was already told the write had succeeded.
    """
    train, _sealed, _shared = sharing_one_take
    into = tmp_path / "data"
    into.mkdir()
    states = [imp.plan(TRAIN_LABEL, train, into) for _ in range(2)]
    unexpected: list[str] = []

    def run(state):
        try:
            imp.execute(state, into)
        except imp.Refused:
            pass
        except BaseException as exc:  # noqa: BLE001
            unexpected.append(type(exc).__name__ + ": " + str(exc))

    threads = [threading.Thread(target=run, args=(state,)) for state in states]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(180)
    assert not unexpected, unexpected
