"""A normal user cannot reach the credential, catalogue or raw-engine surface.

Four refusals are required of the product, and each one gets a test here:

1. a normal user cannot discover an upstream catalogue;
2. a normal user cannot submit an upstream credential;
3. a normal user cannot select a raw engine identifier;
4. a normal user cannot retrieve raw upstream metadata.

The distinction these tests exist to hold
-----------------------------------------
A nav entry that is not rendered is a statement of intent. A request that
reaches a handler unable to honour it is a guarantee. Only the second survives
curl, a cached bundle, or someone running a fork of the web app against a real
deployment — and all three reach the same handler the hidden nav entry would
have reached.

So every request below carries a **valid session token**. That is the whole
point: these are not tests that an anonymous stranger is turned away, which
the auth gate already covers and which would pass just as well if the
entitlement gate were deleted. They assert that a fully authenticated, ordinary
user of this deployment is refused anyway, because the question the gate
answers is not who is asking but whether the deployment offers the surface at
all.

No upstream is named anywhere in this file, including in these comments.
Writing one down in order to forbid it would put the identity back on a
surface the policy exists to keep it off, and the reachability matrix reads
source text — a test that names what it forbids is itself a finding. The
placeholder credential names below are synthetic for that reason. Nothing is
lost by it: the gate refuses by path, before any payload is examined, so what
the body says cannot change the outcome.
"""

from fastapi.testclient import TestClient

import pytest

from youtab_agent_cli.credential_entitlement import (
    REFUSAL_DETAIL,
    REFUSED_PATH_PREFIXES,
)
from youtab_agent_cli.web_server import _SESSION_TOKEN, app

client = TestClient(app, base_url="http://127.0.0.1")

#: A real, valid session. See the module docstring: refusing an anonymous
#: caller would prove nothing about this gate.
HEADERS = {"X-Youtab-Session-Token": _SESSION_TOKEN}

#: Synthetic. Never a real upstream key name.
PLACEHOLDER_CREDENTIAL = "EXAMPLE_UPSTREAM_API_KEY"


@pytest.fixture(autouse=True)
def _no_entitlement(monkeypatch):
    """Pin the default state: no entitlement, however the run was launched.

    Without this the suite would pass or fail depending on whether the
    developer happened to have the variable exported, which is exactly the
    kind of ambient dependency a security assertion must not have.
    """
    from youtab_agent_cli.credential_entitlement import ENTITLEMENT_ENV

    monkeypatch.delenv(ENTITLEMENT_ENV, raising=False)


def _assert_refused(response):
    """A refusal, and specifically this gate's refusal.

    Asserting the status alone would let an unrelated 403 — a CSRF check, a
    future authorization rule, a handler that happens to reject the synthetic
    payload — stand in for the guarantee and quietly pass. Matching the body
    ties the assertion to this decision.
    """
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == REFUSAL_DETAIL


@pytest.mark.parametrize(
    "path",
    [
        "/api/providers/oauth",
        "/api/providers/custom-endpoints",
        "/api/model/options",
    ],
)
def test_a_normal_user_cannot_discover_an_upstream_catalogue(path):
    _assert_refused(client.get(path, headers=HEADERS))


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("put", "/api/env", {"key": PLACEHOLDER_CREDENTIAL, "value": "x"}),
        ("post", "/api/env/reveal", {"key": PLACEHOLDER_CREDENTIAL}),
        ("post", "/api/providers/custom-endpoints", {"name": "n", "base_url": "http://h"}),
        ("post", "/api/providers/validate", {"provider": "p", "api_key": "x"}),
        ("post", "/api/providers/oauth/p/submit", {"code": "x"}),
    ],
)
def test_a_normal_user_cannot_submit_an_upstream_credential(method, path, payload):
    _assert_refused(getattr(client, method)(path, json=payload, headers=HEADERS))


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/model/set", {"scope": "main", "provider": "p", "model": "m"}),
        ("put", "/api/model/moa", {"enabled": True, "models": ["m"]}),
    ],
)
def test_a_normal_user_cannot_select_a_raw_engine(method, path, payload):
    _assert_refused(getattr(client, method)(path, json=payload, headers=HEADERS))


@pytest.mark.parametrize(
    "path",
    [
        "/api/env",
        "/api/model/info",
        "/api/model/auxiliary",
        "/api/model/recommended-default",
        "/api/model/moa",
    ],
)
def test_a_normal_user_cannot_retrieve_raw_upstream_metadata(path):
    _assert_refused(client.get(path, headers=HEADERS))


def test_every_gated_prefix_is_actually_refused():
    """Structural: the declared surface and the enforced surface are the same.

    The four tests above name the routes that exist today. This one fails if a
    prefix is ever added to the policy and the middleware stops consulting it —
    the failure mode where the list looks right and enforces nothing.
    """
    for prefix in REFUSED_PATH_PREFIXES:
        _assert_refused(client.get(prefix, headers=HEADERS))


def test_the_capability_report_says_the_surface_is_absent():
    """The interface is told the truth, so it does not advertise a 403.

    Reported rather than inferred: without this the dashboard would have to
    guess from a failed request, and a client that guesses wrong renders a nav
    entry leading straight to a refusal.
    """
    response = client.get("/api/dashboard/capabilities", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["credential_surface"] is False


def test_the_capability_report_is_not_itself_gated():
    """Asking whether a surface exists must not require the surface.

    If this endpoint ever moved under one of the gated prefixes, the client
    could not distinguish "absent" from "refused" and would have no way to
    render correctly in either case.
    """
    for prefix in REFUSED_PATH_PREFIXES:
        assert not "/api/dashboard/capabilities".startswith(prefix)


def test_an_ungated_route_is_not_refused():
    """Positive control.

    Without this, a middleware that refused *every* request — or a client
    misconfigured so that nothing reaches the app at all — would satisfy every
    assertion above and read as a correctly locked-down deployment.
    """
    response = client.get("/api/sessions", headers=HEADERS)
    assert response.status_code != 403, response.text
