"""WAVE-30E — opt-in Ollama keep_alive (warm-path only, additive)."""
from __future__ import annotations


def _custom_profile():
    # The plugin package dir name has a hyphen; reach the profile via the registry.
    from providers import get_provider_profile
    prof = get_provider_profile("custom") or get_provider_profile("ollama")
    assert prof is not None
    return prof


def test_keep_alive_sent_only_when_provided():
    prof = _custom_profile()
    # Not provided → no keep_alive key (default behaviour unchanged).
    extra, _top = prof.build_api_kwargs_extras(ollama_num_ctx=8192)
    assert "keep_alive" not in extra
    assert extra.get("options", {}).get("num_ctx") == 8192

    # Provided → passed through verbatim as an extra_body field.
    extra2, _ = prof.build_api_kwargs_extras(ollama_num_ctx=8192, ollama_keep_alive="30m")
    assert extra2["keep_alive"] == "30m"
    assert extra2["options"]["num_ctx"] == 8192

    # Empty string is treated as unset (never sends a blank keep_alive).
    extra3, _ = prof.build_api_kwargs_extras(ollama_keep_alive="  ")
    assert "keep_alive" not in extra3


def test_keep_alive_indefinite_is_bounded_and_evict_forwarded():
    # WAVE-30H task 5: an indefinite ("-1") pin is a memory-safety hazard on a
    # shared runtime; the bounded/fail-closed policy caps it to the ceiling
    # (default 1800s) rather than forwarding an unbounded pin. Evict (0) is still
    # forwarded, and an in-bounds finite value ("30m") is forwarded UNCHANGED.
    prof = _custom_profile()
    extra_indef, _ = prof.build_api_kwargs_extras(ollama_keep_alive="-1")
    assert extra_indef["keep_alive"] == 1800  # bounded to the ceiling, never unbounded
    extra_evict, _ = prof.build_api_kwargs_extras(ollama_keep_alive=0)
    assert extra_evict["keep_alive"] == 0
    extra_over, _ = prof.build_api_kwargs_extras(ollama_keep_alive="2h")
    assert extra_over["keep_alive"] == 1800  # 7200s clamped to the ceiling
