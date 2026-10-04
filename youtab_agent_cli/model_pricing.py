"""Pure model price formatting shared by CLI pickers."""

from typing import Any


def _format_price_per_mtok(per_token_str: str) -> str:
    """Convert a per-token price string to a human-friendly $/Mtok string.

    Always uses 2 decimal places so that prices align vertically when
    right-justified in a column (the decimal point stays in the same position).

    Examples:
        "0.000003"   → "$3.00"      (per million tokens)
        "0.00003"    → "$30.00"
        "0.00000015" → "$0.15"
        "0.0000001"  → "$0.10"
        "0.00018"    → "$180.00"
        "0"          → "free"
    """
    try:
        val = float(per_token_str)
    except (TypeError, ValueError):
        return "?"
    if val == 0:
        return "free"
    per_m = val * 1_000_000
    return f"${per_m:.2f}"


def compute_sale_discount(
    prompt: str,
    completion: str,
    original: Any,
) -> tuple[int, str, str] | None:
    """Derive sale chrome from gateway ``pricing.original`` when cheaper.

    Youtab Portal-only feature: callers gate on the provider; this helper only
    sees ``original`` because the Youtab fetch path opted in via
    ``include_sale_original=True``.

    Returns ``(discount_percent, was_prompt_raw, was_completion_raw)`` only when
    ``original`` is a dict and the current prompt (fallback: completion) rate
    is strictly below the corresponding original. Percent is
    ``round((1 - current/original) * 100)`` — never hardcoded, and a discount
    that rounds below 1% is treated as no sale (never render "-0%"). Returns
    ``None`` when there is no sale (missing/equal/invalid original), so UIs
    show normal prices.
    """
    if not isinstance(original, dict):
        return None

    was_prompt = original.get("prompt")
    was_completion = original.get("completion")
    if was_prompt in (None, "") and was_completion in (None, ""):
        return None

    def _finite(raw: Any) -> float | None:
        try:
            n = float(raw)
        except (TypeError, ValueError):
            return None
        return n if n > 0 and n == n else None  # n == n rejects NaN

    def _nonneg(raw: Any) -> float | None:
        try:
            n = float(raw)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 and n == n else None

    # Free / $0 models never show sale chrome, even if a leftover list price
    # is higher (e.g. a :free sibling that inherited pricing.original).
    cur_prompt_any = _nonneg(prompt) if prompt not in (None, "") else None
    cur_comp_any = _nonneg(completion) if completion not in (None, "") else None
    if cur_prompt_any == 0 and cur_comp_any == 0:
        return None

    cur_prompt = _finite(prompt) if prompt not in (None, "") else None
    orig_prompt = _finite(was_prompt) if was_prompt not in (None, "") else None
    if cur_prompt is not None and orig_prompt is not None and cur_prompt < orig_prompt:
        pct = int(round((1.0 - (cur_prompt / orig_prompt)) * 100))
        if pct < 1:
            return None
        return (
            pct,
            str(was_prompt),
            str(was_completion) if was_completion not in (None, "") else "",
        )

    cur_comp = _finite(completion) if completion not in (None, "") else None
    orig_comp = _finite(was_completion) if was_completion not in (None, "") else None
    if cur_comp is not None and orig_comp is not None and cur_comp < orig_comp:
        pct = int(round((1.0 - (cur_comp / orig_comp)) * 100))
        if pct < 1:
            return None
        return (
            pct,
            str(was_prompt) if was_prompt not in (None, "") else "",
            str(was_completion),
        )

    return None
