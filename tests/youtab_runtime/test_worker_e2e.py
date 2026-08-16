from __future__ import annotations

import json
import subprocess
import sys

from .helpers import keypair, signed_envelope


def test_worker_accepts_signed_command_without_public_listener() -> None:
    private, public = keypair()
    envelope = signed_envelope(private)

    completed = subprocess.run(
        [sys.executable, "-m", "youtab_runtime.worker", "--public-key", public],
        input=envelope.model_dump_json() + "\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    result = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert result["accepted"] is True
    assert result["runtime_authority"] is False
    assert result["public_listener"] is False


def test_worker_rejects_tampered_command() -> None:
    private, public = keypair()
    envelope = signed_envelope(private).model_copy(update={"objective": "tampered"})

    completed = subprocess.run(
        [sys.executable, "-m", "youtab_runtime.worker", "--public-key", public],
        input=envelope.model_dump_json() + "\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    result = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert result["accepted"] is False
    assert "invalid command signature" in result["error"]
