# Windows support (runtime / CLI)

Status of native-Windows support for the Youtab Agent Runtime and CLI, and the
line between what is supported cross-platform and what is intentionally
POSIX/Linux-only. This documents *implementation reality*, not aspiration — see
the paired tests referenced below.

## Supported

- **OS**: Windows 10 / 11 (developed and tested on Windows 11, build 26200).
- **Python**: 3.12 in CI (project `requires-python = ">=3.11,<3.14"`).
- **Filesystem**: NTFS.
- **Default home**: `%LOCALAPPDATA%\youtab` (POSIX uses `~/.youtab-agent-runtime`),
  resolved by `youtab_constants._get_platform_default_youtab_home`.

The following runtime/CLI behaviours are supported and covered on Windows:

- **Atomic persistence** — `utils.atomic_json_write` / `atomic_write_text` /
  `atomic_replace`: crash-safe temp-file → fsync → atomic `os.replace`, with a
  bounded retry that rides out the transient Windows `MoveFileEx` sharing
  violation (WinError 5/32) when the destination is momentarily open. Concurrent
  thread and cross-process (`spawn`) writers never corrupt the target; a reader
  never observes a torn document. (`tests/youtab_agent_cli/test_windows_compat.py`,
  `test_atomic_json_write.py`.)
- **Board removal / archive** — `kanban_db.remove_board` closes DB handles
  before rename/rmtree and tolerates the post-close handle-release lag on
  Windows (`_windows_resilient_fs_op`). Callers must use `connect_closing`, not
  `with connect(...)` (which leaves the fd open). (`test_kanban_boards.py`.)
- **Paths & encoding** — spaces, Unicode filenames, non-ASCII content, and
  drive-letter absolute paths; all text I/O is explicit UTF-8 (never the cp1252
  locale default). Local image references in task bodies accept native Windows
  paths (`agent/image_routing.py`). (`test_windows_compat.py`.)
- **Subprocess construction** — argv-list invocation (no shell interpolation),
  paths with spaces, and predictable `FileNotFoundError` for a missing command.
- **Dashboard-auth lifecycle & serving** — the WAVE-19…22 invariants
  (VERIFIED-only serving, provider→route binding, fail-closed ownership, drain
  token-only) run and pass unchanged on Windows (`tests/youtab_runtime/`).
- **Service manager** — `detect_service_manager()` selects the Windows
  Scheduled-Task backend on Windows (never the s6/systemd backends).
- **Web UI build** — builds on Windows; the cross-process build lock is
  flock-based (POSIX) and degrades to an unserialized build on Windows.

## Intentionally POSIX / Linux-only (fails closed on Windows)

These are host-management / deployment integration paths that are meaningless or
unavailable on native Windows. Production degrades safely (selection guard,
early return, or `ImportError`/`AttributeError` fallthrough) — it never invokes
`sudo`, never partially mutates the host, and never reports a false success. The
tests for these paths skip on Windows via **narrow capability guards** in
`tests/_wincompat.py` (never a blanket `sys.platform` skip), and each is paired
with a Windows fail-closed / safe-selection test.

| Capability | Why POSIX-only | Windows behaviour | Guard |
| --- | --- | --- | --- |
| systemd / s6 service supervision | Linux init/`s6-overlay`; needs `os.mkfifo`, `os.chown` | `detect_service_manager` selects the Windows backend | `requires_os_attr("mkfifo")` + selection test |
| Docker UID/GID chown | `os.chown`, uid/gid mapping | chown helper swallows the missing-attr no-op | `requires_os_attr("chown")` |
| `sudo` privileged install / `SUDO_USER` → home | `sudo`, `pwd.getpwnam`, `os.geteuid` | `_apply_profile_override` early-returns (no `os.geteuid`) | `requires_module("pwd")` + no-op test |
| FHS symlinks in `/usr/local/bin`, `~/.local/bin` | symlink creation privilege | n/a on Windows shells | `requires_symlink` (privilege probe) |
| POSIX file-mode bits (`0o600`…) | ACL model on Windows; `st_mode` synthesised | chmod best-effort | `requires_posix_permissions` |
| `curses` session browser | no `_curses` on Windows Python | lazy import, feature unavailable | `requires_module("curses")` |
| pty/termios gateway paths | `pty`/`termios` POSIX-only | Windows uses the ConPTY bridge | `requires_module("termios")` |
| `/proc` process introspection | Linux-only | `ps`/tasklist fallbacks | `requires_proc` |
| flock cross-process web-build serialization | `fcntl` POSIX-only | unserialized build (safe) | `requires_module("fcntl")` + no-flock test |

### Known limitations (not yet implemented on Windows)

- **Worker-exit classification** (`kanban_db._classify_worker_exit`) parses
  POSIX wait-status via `os.WIFEXITED`/`os.WEXITSTATUS`; `reap_worker_zombies`
  is already a documented Windows no-op. Rate-limit-vs-crash requeue
  distinction is therefore POSIX-only today. Tracked as a limitation, not a
  security defect.
- **`shell_hooks`** splits hook command strings with `shlex.split` in POSIX
  mode; a bare native Windows path in a hook command (e.g. `C:\tools\hook.py`)
  needs quoting. Interpreter-prefixed / quoted hook commands work.

## Running the Windows suite

```bash
# From the repo root, with a venv that has .[dev,web] installed:
YOUTAB_AGENT_PYTHON=<python> TZ=UTC PYTHONUTF8=1 \
  bash scripts/run_tests.sh tests/youtab_runtime tests/youtab_agent_cli \
    -j 3 --file-retries 0 -q
```

CI runs this on `windows-latest` as the `windows-runtime-cli` job in
`.github/workflows/youtab-ci.yml`. Genuinely POSIX-only tests report as skipped
(`s`) — that is the intended state, not a gap.
