#!/usr/bin/env python3
"""Enumerate every effective route from the running router.

The authorization registry has to be checked against reality, and reality is
the router the application actually builds -- not a hand-maintained list, which
drifts the moment someone adds an endpoint and is exactly the drift the gate
exists to catch.

So this walks ``app.routes`` and reports what is really there: every HTTP
path/method pair, every WebSocket route, every mount. It normalises dynamic
segments (``/api/sessions/{id}`` stays parameterised rather than collapsing to
a literal) so a registry entry can name a route once and keep matching when the
parameter is renamed.

It classifies nothing. Producing the inventory and deciding each route's
authority are separate jobs on purpose: this one is mechanical and must stay
that way, or the thing being audited would be generating its own audit.

    python3 scripts/youtab/route_inventory.py --json inventory.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: ``HEAD`` is synthesised by Starlette for every ``GET`` and carries no
#: separate authority. ``OPTIONS`` is the CORS preflight, which returns no
#: application data. Both are excluded so the registry describes decisions
#: somebody actually has to make.
_DERIVED_METHODS = frozenset({"HEAD", "OPTIONS"})

_PARAM_RE = re.compile(r"\{[^}/]+\}")


def normalise(path: str) -> str:
    """A stable key for a route.

    Dynamic segments become ``{}`` so renaming a path parameter does not read
    as adding a route and removing another. A trailing slash is stripped except
    on the root, because Starlette exposes both forms for the same endpoint and
    two entries for one decision is how a registry grows a contradiction.
    """
    collapsed = _PARAM_RE.sub("{}", path)
    if len(collapsed) > 1 and collapsed.endswith("/"):
        collapsed = collapsed.rstrip("/")
    return collapsed or "/"


def collect(app) -> dict:
    """Walk the router. Returns HTTP pairs, WebSocket routes and mounts."""
    from starlette.routing import Mount, Route, WebSocketRoute

    http: set[tuple[str, str]] = set()
    sockets: set[str] = set()
    mounts: list[str] = []
    unknown: list[str] = []

    def walk(routes, prefix: str = "") -> None:
        for route in routes:
            raw = prefix + str(getattr(route, "path", "") or "")
            if isinstance(route, Mount):
                mounts.append(normalise(raw))
                # A mount serves an arbitrary sub-application. Its children are
                # walked when it exposes them; when it does not, the mount is
                # the unit of policy and is recorded as such.
                child = getattr(route, "routes", None)
                if child:
                    walk(child, raw)
                continue
            if isinstance(route, WebSocketRoute):
                sockets.add(normalise(raw))
                continue
            if isinstance(route, Route):
                for method in sorted(route.methods or set()):
                    if method not in _DERIVED_METHODS:
                        http.add((normalise(raw), method))
                continue
            # Anything the framework grows later must be visible, not dropped.
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path and methods:
                for method in sorted(methods):
                    if method not in _DERIVED_METHODS:
                        http.add((normalise(prefix + path), method))
            elif path:
                sockets.add(normalise(prefix + path))
            else:
                unknown.append(repr(route))

    walk(app.routes)
    return {
        "schema_version": 1,
        "http": sorted([list(pair) for pair in http]),
        "websocket": sorted(sockets),
        "mounts": sorted(mounts),
        "unrecognised": unknown,
        "counts": {
            "http_route_methods": len(http),
            "http_paths": len({p for p, _ in http}),
            "websocket": len(sockets),
            "mounts": len(mounts),
        },
    }


def load_app():
    from youtab_agent_cli.web_server import app

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="route-inventory")
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--by-prefix", action="store_true",
                    help="summarise counts per top-level prefix")
    args = ap.parse_args(argv)

    inventory = collect(load_app())
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(inventory, indent=2, sort_keys=True), encoding="utf-8")

    counts = inventory["counts"]
    print(f"HTTP route+method pairs : {counts['http_route_methods']}")
    print(f"distinct HTTP paths     : {counts['http_paths']}")
    print(f"WebSocket routes        : {counts['websocket']}")
    print(f"mounts                  : {counts['mounts']}")
    if inventory["unrecognised"]:
        print(f"UNRECOGNISED route objects: {len(inventory['unrecognised'])}")
        return 1

    if args.by_prefix:
        import collections

        per = collections.Counter()
        for path, method in inventory["http"]:
            parts = [p for p in path.split("/") if p and p != "{}"]
            per["/" + "/".join(parts[:2])] += 1
        print("\nper prefix:")
        for prefix, n in per.most_common():
            print(f"  {n:>4}  {prefix}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
