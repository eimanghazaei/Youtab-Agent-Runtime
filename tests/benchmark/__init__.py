"""WAVE-26 Agent Runtime capability benchmark (contract 7).

A versioned, runnable benchmark package that judges runtime capability **only**
from observable, durable state and side effects — the WAVE-26 run journal, the
effect ledger, the egress audit trail, harness process evidence, and files /
artifacts — and **never** from the agent's own self-report. ``self_reported_success``
is recorded for every run but is never an input to the verdict; the divergence
between what the agent claimed and what the state proves is the headline
``honesty_divergences[]`` aggregate.

This package is intentionally kept **out of default pytest collection** (every
test here is marked ``benchmark``; see ``pyproject.toml``). It is exercised by a
dedicated CI job in deterministic-offline mode and, when an Owner provides a
runtime endpoint + secret file, in isolated-local / real-provider modes.
"""
