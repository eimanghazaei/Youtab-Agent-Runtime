# Token-auth provider→route binding (dashboard-auth token plugins)

Security change to the non-interactive (service-to-service) token-auth seam in
`youtab_agent_cli.dashboard_auth.token_auth`. Applies to any plugin that
registers a token-authable route — i.e. any `dashboard_auth` provider that
implements `supports_token` / `verify_token` and opts a route into the seam.

## Changelog

- **Changed (BREAKING for external token plugins):** `register_token_route(path)`
  and `register_token_route_prefix(prefix)` now **require** a keyword argument
  `provider=` (the owning provider's `name`) and accept an optional
  `capability=` (the scope the route requires). Each token route/prefix is now
  owned by **exactly one** provider; the seam authenticates a request **only**
  with that owner instead of trying every registered token provider.
- **Added:** `TokenRouteOwner`, `TokenRouteOwnershipError` (conflicting owner,
  fail-closed), `TokenRouteRegistrationError` (new registration after freeze),
  `route_owner(path)`, `freeze_token_routes()`, `is_frozen()`.
- **Hardened:** prefix membership is decided on path **segments**, not raw
  `str.startswith`, on the same `request.url.path` the ASGI framework routes on.
- **Lifecycle:** the registry is **immutable after startup** (Contract A). The
  dashboard freezes it in lifespan startup, before serving traffic.
- **Fixed:** an accepted service bearer no longer 500s the interactive
  `_authorization_gate` (it previously called `.has()` on a `TokenPrincipal`),
  and a token minted for one surface can no longer authenticate another
  (e.g. a runtime-service token can no longer reach `/api/gateway/drain`).

## Migration for external dashboard-auth / token plugins

If your plugin's `register(ctx)` opts a route into the token seam, add the owner
and capability. Ownership is by the provider's `name`; the capability is the
scope your `verify_token` puts on the returned `TokenPrincipal`.

**Before:**

```python
from youtab_agent_cli.dashboard_auth.token_auth import register_token_route_prefix

def register(ctx):
    provider = MyServiceProvider(secret=..., scope="my-scope")
    ctx.register_dashboard_auth_provider(provider)
    register_token_route_prefix("/api/my-surface/v1")          # OLD
```

**After:**

```python
from youtab_agent_cli.dashboard_auth.token_auth import register_token_route_prefix

def register(ctx):
    provider = MyServiceProvider(secret=..., scope="my-scope")
    ctx.register_dashboard_auth_provider(provider)
    register_token_route_prefix(
        "/api/my-surface/v1",
        provider=provider.name,          # the single owner of this surface
        capability="my-scope",           # scope the caller's TokenPrincipal must hold
    )
```

Exact-path routes are identical via `register_token_route(path, provider=..., capability=...)`.

### Fail-closed behaviour you can rely on

- **An ownerless legacy call fails closed.** A plugin that still calls the old
  signature (`register_token_route(path)` with no `provider=`) raises
  `TypeError` inside its `register(ctx)`; the plugin loader records the error and
  the route is simply **not registered**. It stays behind the interactive cookie
  gate — it never becomes a token-authable route without an owner. There is no
  compatibility default and no inferred owner (either would be unsafe).
- **A conflicting owner fails closed** (`TokenRouteOwnershipError`): two
  providers cannot claim the same route/prefix.
- **A new registration after freeze fails closed** (`TokenRouteRegistrationError`):
  nothing can add a token route once the server is serving. A byte-identical
  idempotent re-registration (e.g. a `discover_plugins(force=True)` re-run of the
  same plugins) remains a no-op.

### Provider→route ownership and capability binding

- **Ownership:** the seam resolves the single owner of a request path
  (exact route > longest segment-anchored prefix; ties between different owners
  fail closed) and consults **only** that provider. A bearer that another
  provider would accept is rejected on a route it does not own.
- **Capability:** the required capability comes from the **route registration**
  (`capability=`), not from arbitrary principal-supplied scopes. The seam admits
  the request only if the owner's `verify_token` returns a principal whose
  `provider` matches the owner **and** whose scopes include the registered
  capability. `_authorization_gate` re-checks the same facts as a defensive belt.

## Registry lifecycle (Contract A — one linearizable auth registry)

The whole dashboard-auth authorization state — providers AND provider-bound token
routes — lives in ONE object, `lifecycle.AuthRegistry`, behind a single
coordinator lock:

    BUILDING  ──lifecycle.freeze_dashboard_auth()──▶  FROZEN

* **BUILDING** (startup): plugins register providers and their owned routes.
* **FROZEN** (serving): the dashboard freezes the registry in lifespan startup,
  before accepting traffic. After freeze, EVERY mutator refuses:
  `register_provider`, `clear_providers`, `register_token_route[_prefix]` (new
  route/owner/capability), `clear_token_routes`. There is **no** provider
  removal/replacement API, and a duplicate provider name always raises — so a
  provider can never be swapped under an owner's name. Only a byte-identical
  idempotent route re-registration (e.g. `discover_plugins(force=True)` of the
  same plugins) stays a no-op.

**Linearizable.** Every mutation and `freeze()` hold the SAME coordinator lock
continuously across *state-validation → conflict/idempotency check → commit*. So a
mutation that observed BUILDING cannot commit after `freeze()` returns: it either
committed before `freeze` took the lock, or it takes the lock afterward, re-reads
`_frozen` under the same hold, sees FROZEN, and is refused. There is no
check-then-act window. The lock is a leaf (no other lock is taken while it is
held), so there is no lock-order cycle.

**Consistent snapshot, lock released before `verify_token`.**
`authenticate_token` resolves the `(owner, provider)` snapshot under the
coordinator lock in one hold, **releases the lock**, and only then calls the owner
provider's `verify_token` — never with the lock held. The `_authorization_gate`
re-check reads the same frozen generation, so owner/provider/capability cannot
change between seam authentication and the gate: no cross-generation / split-lock
TOCTOU.

**No runtime reset.** `clear_*` are refused after freeze, and there is **no
reset/unfreeze callable in any shipped module** — a frozen registry cannot be
reopened. Tests get isolation by INJECTING a fresh instance: an autouse fixture
rebinds the module default (`lifecycle._default`) to a new `AuthRegistry()` per
test; the delegating module functions resolve the default at call time, so the
injection is total and needs no production reset path.

## Versioning

Repository version is pre-1.0 (`0.19.1`). Under semantic versioning a breaking
change to a public plugin API before 1.0.0 is a **minor** bump: **0.19.1 →
0.20.0** (pre-1.0 breaking-version notation). The bump itself is intentionally
**not** made in this change set because `0.19.1` is also asserted by unrelated
release/wake-word fixtures and pinned in `uv.lock`; it should be applied
together with the release that lands this change.

### Release gate (stacked-PR ledger)

This change ships on `fix/wave12-authz-gate-token-provider-binding`, whose Draft
PR targets `integration/engine-runtime-v5` (itself the head line of a separate
Draft PR to `main`). The following gate is mandatory and must not be skipped:

- **The engine PR that carries this change to `main` MUST NOT become Ready or
  merge to `main` until, on the same exact head:** the version is bumped
  `0.19.1 → 0.20.0` in `pyproject.toml` and `youtab_agent_cli/__init__.py`,
  `uv.lock` is regenerated, the *legitimate* version references are updated
  (`scripts/wakeword/qualify.py` and the wake-word fixtures that assert
  `runtime_version`/`--runtime-version`), and ALL CI reruns green on the
  resulting exact head.
- **Do not** modify fixtures merely to silence a version assertion — update them
  only because the runtime version legitimately changed to `0.20.0`.
- Each stacked merge (this branch → `integration/engine-runtime-v5` → `main`)
  requires its own exact-head Owner authorization and a full CI rerun on the new
  head. Only the final merged `main` SHA may become the Engine pin; a Draft head
  is never a final pin.
