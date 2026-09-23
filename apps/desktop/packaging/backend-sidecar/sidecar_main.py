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

LOOPBACK-ONLY ENFORCEMENT (desktop sidecar security): a bundled desktop sidecar
must never open a listener on a non-loopback interface. This launcher inspects
the requested ``--host`` BEFORE the CLI runs and refuses (exits non-zero, before
any socket is bound and before readiness is announced) when the host is not a
loopback address. Authentication remains mandatory in addition to this — the two
are layered, not alternatives. Only ``127.0.0.0/8``, ``::1`` and ``localhost``
(which resolves to loopback on standard systems) are accepted; ``0.0.0.0``,
``::`` and any routable IPv4/IPv6 address are rejected up front.
"""

import ipaddress
import sys

# Exit code used when a non-loopback bind is refused (distinct from other
# failures so callers/tests can assert the reason).
NON_LOOPBACK_REFUSED_EXIT = 78  # EX_CONFIG


def _host_is_loopback(host: str) -> bool:
    """True iff ``host`` is a loopback bind target.

    Accepts 127.0.0.0/8, ::1 and the name ``localhost``. A bracketed IPv6
    literal (``[::1]``) is unwrapped. Any other name is rejected (it could
    resolve off-host), as is any routable address or ``0.0.0.0`` / ``::``.
    """
    h = (host or "").strip().lower()
    if h in ("localhost", "localhost.localdomain"):
        return True
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _requested_host(argv):
    """Extract an explicit ``--host <value>`` / ``--host=<value>`` from argv.

    Returns the host string if one was passed, else None (no explicit host —
    the CLI's own loopback default applies and there is nothing to refuse here).
    """
    for i, tok in enumerate(argv):
        if tok == "--host" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--host="):
            return tok.split("=", 1)[1]
    return None


def _enforce_loopback_only(argv) -> None:
    host = _requested_host(argv)
    if host is not None and not _host_is_loopback(host):
        sys.stderr.write(
            "youtab-backend: refusing to start — the desktop sidecar binds "
            f"loopback only, but --host {host!r} is not a loopback address. "
            "No listener was opened.\n"
        )
        sys.stderr.flush()
        raise SystemExit(NON_LOOPBACK_REFUSED_EXIT)


class _Lifecycle:
    """Append-only process lifecycle record for the packaged backend.

    A desktop sidecar that disappears must leave evidence of HOW: a Python exit
    (``SystemExit``/``os._exit``/uncaught exception, with its stack), a native
    fault (``faulthandler``), or nothing at all, which means it was terminated
    from outside. Records carry pid/ppid and never argv values beyond the
    command name, so no secret reaches the file.
    """

    def __init__(self, path):
        self.path = path
        self._fault_file = None

    def write(self, event, detail=""):
        import os
        import time

        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} "
            f"pid={os.getpid()} ppid={os.getppid()} {event}"
        )
        if detail:
            line += "\n" + detail.rstrip()
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    def install(self, argv):
        import atexit
        import faulthandler
        import os
        import threading
        import traceback

        command = next((a for a in argv if not a.startswith("-")), "")
        self.write("start", f"command={command!r} frozen={bool(getattr(sys, 'frozen', False))}")
        try:
            self._fault_file = open(self.path, "a", encoding="utf-8")
            faulthandler.enable(file=self._fault_file, all_threads=True)
        except (OSError, RuntimeError, ValueError):
            self._fault_file = None

        atexit.register(lambda: self.write("atexit"))

        real_exit = os._exit

        def _recorded_exit(code):
            self.write(f"os._exit code={code}", "".join(traceback.format_stack()))
            real_exit(code)

        os._exit = _recorded_exit

        previous_hook = threading.excepthook

        def _thread_hook(args):
            self.write(
                f"thread-exception thread={getattr(args.thread, 'name', '?')}",
                "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)),
            )
            previous_hook(args)

        threading.excepthook = _thread_hook

    def run(self, main):
        import traceback

        try:
            main()
        except SystemExit as exc:
            self.write(f"SystemExit code={exc.code!r}", "".join(traceback.format_exc()))
            raise
        except BaseException:
            self.write("uncaught-exception", traceback.format_exc())
            raise
        self.write("main-returned")


def _lifecycle_log_path():
    import os

    try:
        from youtab_constants import get_youtab_home

        logs = get_youtab_home() / "logs"
        os.makedirs(logs, exist_ok=True)
        return str(logs / "backend-lifecycle.log")
    except Exception:
        return None


if __name__ == "__main__":
    # Refuse a non-loopback bind before importing/booting the CLI, so no socket
    # is ever created and no readiness line is emitted on a rejected host.
    _enforce_loopback_only(sys.argv[1:])

    _log_path = _lifecycle_log_path()
    _lifecycle = _Lifecycle(_log_path) if _log_path else None
    if _lifecycle is not None:
        _lifecycle.install(sys.argv[1:])

    from youtab_agent_cli.main import main

    if _lifecycle is not None:
        _lifecycle.run(main)
    else:
        main()
