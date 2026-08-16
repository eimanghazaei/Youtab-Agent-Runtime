#!/usr/bin/env python3
"""Fail closed when the router and the authorization policy disagree.

The policy is only a control while it describes the router the application
actually builds. A route added without a rule, a rule left behind by a deleted
route, a method nobody classified -- each of those is the gate's whole subject,
and each of them is invisible to a test that asserts on a hand-written list.

So this walks the real router (via :mod:`scripts.youtab.route_inventory`) and
compares it against :mod:`youtab_agent_cli.authz`. It decides nothing itself:
producing the inventory, deciding each route's authority, and checking the two
agree are three separate jobs, and this is only the third.

Nine conditions fail it, and each fails *closed* -- an inconclusive check is a
failure, never a pass:

    1. missing classification  a router route no rule resolves
    2. stale entry             a rule matching nothing on the router
    3. duplicate entry         one route+method decided twice, conflictingly
    4. unknown method          a rule naming a verb the router cannot serve
    5. unknown route object    the collector could not classify a route object
    6. ambiguous parameters    two rules that differ only by parameter naming
    7. missing WebSocket       a socket that does not enforce a scope
    8. missing mount           a mount with no rule covering it
    9. unjustified public      a public/loopback route with no written reason

    python3 scripts/youtab/router_policy_gate.py
    python3 scripts/youtab/router_policy_gate.py --json report.json
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

#: Sockets are enforced inside their handler, so the gate reads the handler
#: rather than a table: the property is "this function consults the scope gate
#: before it accepts", which only the source can answer.
_SCOPE_GATE = "_ws_scope_ok"

#: Routes that exist only in some build states. The gate must accept their
#: absence, or a runner that skipped the frontend build fails for a reason that
#: has nothing to do with authorization.
CONDITIONAL_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("/assets/{}.css", "GET"),
    ("/assets", "MOUNT"),
})


class GateFailure(list):
    """Findings, grouped by condition. Truthy when the gate must fail."""

    def add(self, condition: str, detail: str) -> None:
        self.append((condition, detail))


def _probe(path: str) -> str:
    """A concrete request path for a normalised template."""
    return "/" if path == "/" else path.replace("{}", "x")


def _load():
    from scripts.youtab.route_inventory import collect
    from youtab_agent_cli.web_server import app

    return app, collect(app)


def _check_http(inventory, authz, findings: GateFailure) -> None:
    for pair in inventory["http"]:
        path, method = tuple(pair)
        if (path, method) in CONDITIONAL_ROUTES:
            continue
        if authz.required_scope(_probe(path), method) is None:
            findings.add("missing-classification", f"{method} {path}")


def _check_stale(inventory, authz, findings: GateFailure) -> None:
    """Every literal rule must still match something the router serves.

    Prefix rules are checked by prefix, exact rules by equality, so a rule for
    a deleted route is reported rather than quietly protecting nothing.
    """
    live = {p for p, _ in (tuple(x) for x in inventory["http"])}
    live |= set(inventory["websocket"]) | set(inventory["mounts"])
    live_probes = {_probe(p) for p in live}
    # The SPA catch-all is declared `/{full_path:path}` and normalises to
    # `/{}`, whose probe is `/x`. The root itself is served by that same route,
    # so a rule for `/` is live whenever the catch-all is.
    if "/{}" in live:
        live_probes.add("/")
    conditional_probes = {_probe(p) for p, _ in CONDITIONAL_ROUTES}

    for path in authz.EXACT_ROUTE_SCOPES:
        if path not in live_probes and path not in conditional_probes:
            findings.add("stale-entry", f"exact rule matches no route: {path}")

    for prefix, _ in authz.ROUTE_SCOPES:
        if not any(p.startswith(prefix) for p in live_probes):
            findings.add("stale-entry", f"prefix rule matches no route: {prefix}")

    for template, _ in authz.PATTERN_ROUTE_SCOPES:
        pattern = re.compile("^" + "[^/]+".join(
            re.escape(part) for part in template.split("{}")) + "$")
        if template in {p for p, _ in CONDITIONAL_ROUTES}:
            continue  # absent in this build state by design
        if not any(pattern.match(p) for p in live_probes):
            findings.add("stale-entry", f"pattern rule matches no route: {template}")


def _check_duplicates(authz, findings: GateFailure) -> None:
    seen: dict[str, str] = {}
    for prefix, scope in authz.ROUTE_SCOPES:
        if prefix in seen and seen[prefix] != scope:
            findings.add("duplicate-entry",
                         f"{prefix} decided twice: {seen[prefix]} and {scope}")
        seen[prefix] = scope
    for path in authz.EXACT_ROUTE_SCOPES:
        # An exact rule that duplicates a prefix rule is fine -- that is how a
        # narrower decision is expressed. Two *exact* rules cannot collide,
        # because the table is a dict. Patterns can, so check those.
        pass
    templates = [t for t, _ in authz.PATTERN_ROUTE_SCOPES]
    if len(templates) != len(set(templates)):
        findings.add("duplicate-entry", "two pattern rules share a template")


def _check_methods(inventory, authz, findings: GateFailure) -> None:
    served = {m for _, m in (tuple(x) for x in inventory["http"])}
    known = served | {"WEBSOCKET", "MOUNT"}
    for scope_name in ("WRITE_METHODS",):
        for method in getattr(authz, scope_name):
            if method not in known:
                findings.add("unknown-method",
                             f"{scope_name} names {method}, which the router never serves")


def _check_unrecognised(inventory, findings: GateFailure) -> None:
    for entry in inventory["unrecognised"]:
        findings.add("unknown-route-object", entry)


def _check_ambiguous_parameters(inventory, findings: GateFailure) -> None:
    """Two routes whose normalised forms collide are one decision, not two.

    The inventory normalises ``{id}`` and ``{session_id}`` to the same key on
    purpose. If two *distinct* declared paths collapse onto one key with
    different methods that is fine; if they collapse and the router serves both
    as separate endpoints, a single rule silently governs both.
    """
    seen: dict[tuple[str, str], int] = {}
    for pair in inventory["http"]:
        key = (tuple(pair)[0], tuple(pair)[1])
        seen[key] = seen.get(key, 0) + 1
    for key, count in seen.items():
        if count > 1:
            findings.add("ambiguous-parameter",
                         f"{key[1]} {key[0]} collapses {count} declared routes")


def _check_websockets(app, inventory, findings: GateFailure) -> None:
    import inspect

    sources: dict[str, str] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        if path and not getattr(route, "methods", None) and getattr(route, "endpoint", None):
            try:
                sources[path] = inspect.getsource(inspect.unwrap(route.endpoint))
            except (OSError, TypeError):
                # Cannot read it, so cannot prove it enforces. Fail closed.
                findings.add("missing-websocket",
                             f"{path}: handler source unavailable, cannot verify")
    for path in inventory["websocket"]:
        source = sources.get(path)
        if source is None:
            findings.add("missing-websocket", f"{path}: no handler resolved")
            continue
        if _SCOPE_GATE in source:
            continue
        # Delegating handlers are allowed, but the delegate has to be named in
        # the body and has to enforce -- checked one level deep, not assumed.
        delegated = re.findall(r"(\w*_ws_upgrade_authorized|\w*_authorized)\(", source)
        if delegated and _delegate_enforces(app, delegated[0]):
            continue
        findings.add("missing-websocket", f"{path}: accepts without {_SCOPE_GATE}")


def _delegate_enforces(app, name: str) -> bool:
    import inspect

    for module in list(sys.modules.values()):
        fn = getattr(module, name, None)
        if fn is None or not callable(fn):
            continue
        try:
            if _SCOPE_GATE in inspect.getsource(fn):
                return True
        except (OSError, TypeError):
            continue
    return False


def _check_mounts(inventory, authz, findings: GateFailure) -> None:
    for mount in inventory["mounts"]:
        if (mount, "MOUNT") in CONDITIONAL_ROUTES:
            continue
        if authz.required_scope(_probe(mount), "GET") is None:
            findings.add("missing-mount", f"{mount}: no rule covers this mount")


def _check_public_justified(findings: GateFailure) -> None:
    """Every open route must carry an individual written reason.

    The registry is where those reasons live. A route the policy calls public
    with no entry there is an unreviewed decision, which is the condition this
    check exists for.
    """
    from youtab_agent_cli import authz
    from youtab_agent_cli.route_authz_registry import INDEX

    justified = {
        entry.path for entry in INDEX.values()
        if entry.justification.strip()
    }
    # The catch-all has two spellings: the policy table keys it `/` (an exact
    # match on the bare root), the router and the registry key it `/{}`. One
    # decision, so one justification satisfies both.
    if "/{}" in justified:
        justified.add("/")
    for path, scope in authz.EXACT_ROUTE_SCOPES.items():
        if scope != authz.PUBLIC:
            continue
        if path not in justified:
            findings.add("unjustified-public",
                         f"{path} is public with no written justification "
                         "in route_authz_registry")


def run() -> GateFailure:
    findings = GateFailure()
    try:
        app, inventory = _load()
    except Exception as exc:  # noqa: BLE001 -- inconclusive is a failure
        findings.add("load-failure", f"router could not be built: {exc!r}")
        return findings

    from youtab_agent_cli import authz

    _check_http(inventory, authz, findings)
    _check_stale(inventory, authz, findings)
    _check_duplicates(authz, findings)
    _check_methods(inventory, authz, findings)
    _check_unrecognised(inventory, findings)
    _check_ambiguous_parameters(inventory, findings)
    _check_websockets(app, inventory, findings)
    _check_mounts(inventory, authz, findings)
    _check_public_justified(findings)
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="router-policy-gate")
    ap.add_argument("--json", dest="json_out")
    args = ap.parse_args(argv)

    findings = run()
    grouped: dict[str, list[str]] = {}
    for condition, detail in findings:
        grouped.setdefault(condition, []).append(detail)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(grouped, indent=2, sort_keys=True), encoding="utf-8")

    if not findings:
        print("PASS: the router and the authorization policy agree")
        return 0

    print(f"FAIL: {len(findings)} finding(s) across "
          f"{len(grouped)} condition(s)")
    for condition in sorted(grouped):
        print(f"\n  {condition} ({len(grouped[condition])}):")
        for detail in sorted(grouped[condition])[:20]:
            print(f"    - {detail}")
        if len(grouped[condition]) > 20:
            print(f"    ... and {len(grouped[condition]) - 20} more")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
