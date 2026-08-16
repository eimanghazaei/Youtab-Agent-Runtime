#!/usr/bin/env python3
"""Render the origin nginx artifact from its generator, or prove it has not drifted.

``youtab_runtime.origin_protection`` is the authority for what the origin
serves; ``infrastructure/nginx/agent.youtab.io.conf`` is only its output,
committed so the block is reviewable in a diff and can be copied to the host.
There was no way to regenerate it, which meant the only way to change the
committed file was to edit it by hand -- and a hand-edited "generated" file is
worse than an ungenerated one, because it still carries the header telling the
next reader that the generator is authoritative.

That is not hypothetical here. The ``http2 on;`` spelling was corrected
directly on the host once already, and the whole reason this generator exists
is that the correction did not come back.

    python scripts/youtab/render_origin_nginx.py            # write the file
    python scripts/youtab/render_origin_nginx.py --check    # fail on any drift

``--check`` compares bytes, not parsed directives: a config that differs only
in whitespace is still not the file that was reviewed.

Written with an explicit ``\\n`` newline because the deployment target and the
CI container are Linux, and a CRLF checkout on a Windows workstation would
otherwise round-trip into the repository. ``.gitattributes`` pins the artifact
to LF for the same reason, so the byte comparison means the same thing on
every machine that runs it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from youtab_runtime.origin_protection import (  # noqa: E402
    AGENT_ORIGIN_CONF_PATH,
    render_agent_origin_conf,
)


def _rendered_bytes() -> bytes:
    return render_agent_origin_conf().encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write. Exit non-zero if the committed file is not exactly "
        "what the generator produces.",
    )
    args = parser.parse_args(argv)

    target = REPO_ROOT / AGENT_ORIGIN_CONF_PATH
    expected = _rendered_bytes()

    if args.check:
        if not target.exists():
            print(f"FAIL: {AGENT_ORIGIN_CONF_PATH} is missing", file=sys.stderr)
            return 1
        actual = target.read_bytes()
        if actual != expected:
            print(
                f"FAIL: {AGENT_ORIGIN_CONF_PATH} has drifted from its generator.\n"
                f"  committed: {len(actual)} bytes\n"
                f"  generated: {len(expected)} bytes\n"
                "Regenerate with: python scripts/youtab/render_origin_nginx.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {AGENT_ORIGIN_CONF_PATH} matches the generator ({len(actual)} bytes)")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(expected)
    print(f"wrote {AGENT_ORIGIN_CONF_PATH} ({len(expected)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
