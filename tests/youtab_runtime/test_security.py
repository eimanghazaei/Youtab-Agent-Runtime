from __future__ import annotations

from pathlib import Path

import pytest

from youtab_runtime.security import redact_secrets, resolve_within, validate_outbound_url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://localhost/admin",
        "https://127.0.0.1/admin",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.2/internal",
        "https://user@example.com/private",
    ],
)
def test_ssrf_shapes_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        validate_outbound_url(url)


def test_public_https_url_is_admitted() -> None:
    assert validate_outbound_url("https://example.com/evidence") == (
        "https://example.com/evidence"
    )


def test_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes"):
        resolve_within(tmp_path, "../outside.txt")


def test_secret_values_are_redacted() -> None:
    text = "api_key=super-secret-value token:another-secret"
    redacted = redact_secrets(text)

    assert "super-secret-value" not in redacted
    assert "another-secret" not in redacted
    assert redacted.count("[REDACTED]") == 2
