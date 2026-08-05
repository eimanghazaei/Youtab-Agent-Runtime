# ADR-0001 — Authentication and the trust boundary

**Status:** Accepted · **Scope:** protected pre-production Runtime
**Supersedes:** nothing · **Related:** `YOUTAB_AGENT_RUNTIME_BOUNDARY.md`

## Context

Moving the control plane onto a private Docker bridge changed which identity
control is load-bearing, and the change is not obvious.

Under `network_mode: host` the dashboard bound `127.0.0.1`, and
`should_require_auth` returns `False` for a loopback bind — so the dashboard
served **unauthenticated**, and what protected it was that only the host could
address it. Nginx sat in front, Cloudflare Access enforced at the edge, and the
origin trusted the network.

On a bridge, Docker publishes to a container's *interface* address and never to
its loopback, so a loopback-bound dashboard is unreachable through a published
port. The container must bind `0.0.0.0` inside its own namespace. That is not a
public bind — the namespace has exactly one way in, a port published on host
loopback — but `should_require_auth` correctly treats any non-loopback bind as
gated, and the gate fails closed with no provider registered.

So the bridge migration cannot ship without an answer to: *which provider?*

The edge already answers a related question. `youtab_agent_cli/access_jwt.py`
verifies the `Cf-Access-Jwt-Assertion` Cloudflare signs for every request it
forwards: RS256 pinned rather than read from the header, issuer and audience
from configuration rather than from the token, JWKS with a bounded cache, `exp`
and `nbf` checked, and only an exactly-configured address accepted. What it
does *not* do is create a session. It authorises a request; the dashboard gate
wants an identity.

## Decision

**Register the existing Access verifier as a real dashboard-auth provider.**
Cloudflare Access is the identity boundary for protected pre-production. The
bundled password provider is not enabled and no shared password is introduced.

Specifically:

1. The provider **reuses `access_jwt.verify_access_token`**. It does not
   re-implement verification and does not relax it.
2. A verified assertion is mapped to a **governed internal Youtab principal**
   through the authz roster — Cloudflare says *who the human is*; the roster
   says *what they may do*. The provider never reads a role from a token
   claim, because the identity provider is not the authority on Youtab
   authority.
3. **No header is trusted without the verified JWT.** `Cf-Access-Authenticated-User-Email`
   is set by Cloudflare and is trivially forgeable by anything that reaches the
   origin directly, so the email comes from verified claims only.
4. Sessions are minted server-side, carried in `Secure` + `HttpOnly` cookies
   with the existing `__Host-` prefix, and rotated by the existing refresh
   path. `verify_session` re-verifies against Access rather than trusting a
   cookie forever.
5. WebSocket and SSE authenticate the **same principal** through the existing
   ticket seam. A transport that skipped this would be an unauthenticated hole
   beside an authenticated door.

### Why not the alternatives

**Bundled password provider.** A shared password on a pre-production host with
Owner-level authority is a credential that gets pasted into chat, survives
staff changes, and cannot be attributed. Access already gives per-person
identity with an audit trail at the edge.

**Trust `Cf-Access-Authenticated-User-Email`.** This is the tempting shortcut
and it is the reason this ADR is explicit. That header is only meaningful if
the request provably came through the edge. The origin's own address is public
— a sibling hostname on the zone resolves straight to it — so anything that can
open a socket can send that header. Verifying the signed assertion is what
turns "a header says so" into a credential.

**Wait for Youtab OAuth/OIDC.** That is the canonical customer-facing identity
and remains so. It is not built, and pre-production needs a boundary now.

## Consequences

- The dashboard is **more strictly** authenticated than under host networking,
  where it was not authenticated at all.
- Deployment gains a hard prerequisite: `YOUTAB_ACCESS_TEAM_DOMAIN`,
  `YOUTAB_ACCESS_AUD` and `YOUTAB_ACCESS_ALLOWED_EMAILS` must be configured, or
  the gate fails closed and the container is unreachable. This is deliberate —
  failing closed is the correct direction — and it is a pre-deploy checklist
  item, not a runtime default.
- Loopback health stays available for the container's own healthcheck through
  its narrow internal contract. Exempting the health *path* publicly would hand
  out an unauthenticated liveness and version oracle.
- Direct-origin rejection, AOP, Host validation and WebSocket Origin
  validation are unchanged. This ADR adds an identity layer; it removes no
  network control.

## Future: customer-facing Runtime

Canonical application identity is **Youtab OAuth/OIDC, Authorization Code with
PKCE**. Cloudflare may remain an edge and origin security layer, but it must
not become Youtab's tenant identity, user lifecycle or RBAC. Normal customer
access is **not** enabled by this ADR.

## Acceptance criteria (executable)

- `auth_required` reports the true authenticated state on a gated bind.
- A request with no assertion is refused.
- A forged signature, wrong issuer, wrong audience, expired, or not-yet-valid
  assertion is refused.
- An assertion for an address outside the allowlist is refused.
- An Owner/Admin address is accepted and maps to the Owner principal.
- A raw email header without a verified assertion grants nothing.
- No token, claim, email or cookie appears in a URL or a log line.
- Mutation controls: removing signature, issuer, audience or allowlist
  verification each turns the suite red.
