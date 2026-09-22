"""Frozen entrypoint for the self-contained Youtab backend sidecar.

This is the SAME backend Electron launches in development:
``python -m youtab_agent_cli.main`` — so the frozen executable accepts the
identical ``serve --host 127.0.0.1 --port 0`` invocation, announces
``YOUTAB_AGENT_BACKEND_READY port=<n>`` on stdout, and serves the HTTP loopback
gateway (``/api/health`` etc.). Freezing the real CLI entry (rather than the
``tui_gateway`` stdio gateway) keeps the existing Electron HTTP backend
lifecycle — port announcement, health/readiness, connection-state machine,
shutdown — working unchanged; packaged mode only swaps the *command* from a
system Python to this bundled executable.

A thin launcher (rather than freezing ``main.py`` as a script) preserves the
``youtab_agent_cli`` package context so its absolute imports resolve when
frozen. Importing ``youtab_agent_cli.main`` runs ``youtab_bootstrap``'s import
hardening as a side effect, exactly as ``-m youtab_agent_cli.main`` does.
"""

from youtab_agent_cli.main import main

if __name__ == "__main__":
    main()
