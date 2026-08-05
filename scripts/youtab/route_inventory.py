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
    """Walk the router. Returns HTTP pairs, WebSocket routes and mounts.

    Duck-typed rather than isinstance-driven. FastAPI wraps routes in its own
    types depending on version -- a runner with a different FastAPI put 158 of
    this application's routes into `_IncludedRoute`, which an isinstance check
    against `starlette.routing.Route` does not match, and the inventory
    silently reported 136 instead of 294. An inventory that under-reports is
    worse than none: every route it misses is a route the registry never has to
    classify.

    So the shape decides, wrappers are unwrapped, and anything still
    unrecognised is reported rather than dropped.
    """
    from starlette.routing import Mount

    http: set[tuple[str, str]] = set()
    sockets: set[str] = set()
    mounts: list[str] = []
    unknown: list[str] = []
    seen: set[int] = set()

    def walk(routes, prefix: str = "") -> None:
        for route in routes or ():
            if id(route) in seen:
                continue
            seen.add(id(route))
            raw = prefix + str(getattr(route, "path", "") or "")

            if isinstance(route, Mount):
                mounts.append(normalise(raw))
                walk(getattr(route, "routes", None), raw)
                continue

            # FastAPI's `include_router` wrapper. Newer versions keep the
            # included router and its prefix in a context object rather than
            # flattening the routes into the parent, so the parent's
            # `app.routes` contains `_IncludedRouter(original_router=...,
            # include_context=_RouterIncludeContext(..., prefix=...))` and the
            # real routes are one level down. Missing this is what reported 136
            # of 294 route+methods on the CI runner while reporting all 294
            # locally, purely because the two had different FastAPI versions.
            #
            # The prefix has to come from the context: the wrapper carries no
            # `path`, so walking the inner router without it would record every
            # included route at the wrong path.
            context = getattr(route, "include_context", None)
            included = (
                getattr(route, "original_router", None)
                or getattr(context, "included_router", None)
            )
            if included is not None and getattr(included, "routes", None) is not None:
                walk(included.routes,
                     prefix + str(getattr(context, "prefix", "") or ""))
                continue

            # Other wrapper shapes, by attribute rather than by type.
            for attr in ("route", "_route", "__wrapped__"):
                inner = getattr(route, attr, None)
                if inner is not None and inner is not route and (
                    hasattr(inner, "path") or hasattr(inner, "routes")
                ):
                    walk([inner], prefix)
                    break
            else:
                inner = None
            if inner is not None:
                continue

            methods = getattr(route, "methods", None)
            path = getattr(route, "path", None)

            if path and methods:
                for method in sorted(methods):
                    if method not in _DERIVED_METHODS:
                        http.add((normalise(raw), method))
                continue

            # No methods but a path: a WebSocket route, or a wrapper around
            # one. `endpoint` distinguishes a real route from a container.
            if path and getattr(route, "endpoint", None) is not None:
                sockets.add(normalise(raw))
                continue

            nested = getattr(route, "routes", None)
            if nested:
                if path:
                    mounts.append(normalise(raw))
                walk(nested, raw if path else prefix)
                continue

            if path:
                sockets.add(normalise(raw))
                continue

            unknown.append(repr(route)[:200])

    walk(app.routes)
    return {
        "schema_version": 1,
        "http": sorted([list(pair) for pair in http]),
        "websocket": sorted(sockets),
        "mounts": sorted(set(mounts)),
        "unrecognised": unknown,
        "counts": {
            "http_route_methods": len(http),
            "http_paths": len({p for p, _ in http}),
            "websocket": len(sockets),
            "mounts": len(set(mounts)),
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
