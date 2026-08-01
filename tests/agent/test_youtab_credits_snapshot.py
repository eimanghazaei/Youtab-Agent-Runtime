"""Tests for build_youtab_credits_snapshot (L6-A, magnitudes-only)."""

from __future__ import annotations

from agent.account_usage import build_youtab_credits_snapshot
from youtab_agent_cli.youtab_account import (
    YoutabPaidServiceAccessInfo,
    YoutabPortalAccountInfo,
    YoutabPortalSubscriptionInfo,
)


def _account(**kwargs) -> YoutabPortalAccountInfo:
    kwargs.setdefault("logged_in", True)
    kwargs.setdefault("source", "account_api")
    kwargs.setdefault("fresh", True)
    return YoutabPortalAccountInfo(**kwargs)


def _all_lines(snapshot) -> list[str]:
    return list(snapshot.details)


def test_healthy():
    info = _account(
        paid_service_access=True,
        paid_service_access_info=YoutabPaidServiceAccessInfo(
            subscription_credits_remaining=18.0,
            purchased_credits_remaining=12.34,
            total_usable_credits=30.34,
        ),
        subscription=YoutabPortalSubscriptionInfo(
            plan="Pro",
            current_period_end="2026-07-01",
        ),
    )
    snap = build_youtab_credits_snapshot(info)
    assert snap is not None
    assert snap.available is True
    assert snap.plan == "Pro"
    assert snap.provider == "youtab"
    assert snap.title == "Youtab credits"
    blob = "\n".join(_all_lines(snap))
    assert "$18.00" in blob
    assert "$12.34" in blob
    assert "$30.34" in blob
    assert "Renews: 2026-07-01" in blob
    assert "/billing" in blob
    # money-rule: magnitudes-only, never a percentage
    assert "%" not in blob








def test_logged_out():
    info = _account(
        logged_in=False,
        paid_service_access=True,
        paid_service_access_info=YoutabPaidServiceAccessInfo(
            total_usable_credits=10.0,
        ),
    )
    assert build_youtab_credits_snapshot(info) is None


def test_none():
    assert build_youtab_credits_snapshot(None) is None






