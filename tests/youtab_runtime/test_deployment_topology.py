"""The deployment topology the dashboard's lifecycle controls actually require.

`service_manager` locates supervised slots under `S6_DYNAMIC_SCANDIR`
(`/run/service`), which is container-local tmpfs. That single fact decides the
topology: a `gateway-default` slot registered inside a gateway container is
invisible to a dashboard running in a *different* container, so the dashboard's
Restart Gateway control fails with `no such gateway 'default'` and the UI
reports "Gateway restart failed with exit 1 / Gateway Status: Off".

That is exactly what shipped. `docker-compose.yml` declared two services, and
nothing in the repository bridges them -- no lifecycle API, no control socket,
no shared supervisor, no authenticated IPC. Mounting the Docker socket would
have made it "work" and would have handed the web application root control of
the host.

These tests pin the contract so the split cannot come back by being tidier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _services(compose: dict) -> dict:
    return compose.get("services") or {}


def test_supervised_slots_are_container_local():
    """The premise everything else rests on.

    If this ever became a path on a shared volume, two containers could share
    lifecycle control and the single-container requirement would relax. Until
    then it does not.
    """
    from youtab_agent_cli.service_manager import S6_DYNAMIC_SCANDIR

    assert str(S6_DYNAMIC_SCANDIR).startswith("/run/"), (
        f"supervised slots now live at {S6_DYNAMIC_SCANDIR}; if that is shared "
        "between containers, revisit the single-container topology"
    )


def test_exactly_one_runtime_service_is_declared(compose):
    """A second service is how the defect returns."""
    services = _services(compose)
    assert len(services) == 1, (
        "docker-compose declares more than one service. The dashboard and the "
        f"gateway must share one container: {sorted(services)}"
    )


def test_no_dashboard_only_container(compose):
    """A `dashboard` command selects the role that skips gateway registration.

    That skip is correct -- it avoids lock contention on
    `logs/gateways/<profile>/lock` -- which is precisely why a dashboard-only
    container must not be the thing serving the UI that offers lifecycle
    controls.
    """
    for name, service in _services(compose).items():
        command = service.get("command") or []
        assert command and command[0] != "dashboard", (
            f"service {name!r} runs the dashboard-only role, which never "
            "registers a gateway slot"
        )


def test_the_service_runs_the_gateway_role(compose):
    """`gateway run` is what makes the boot reconciler register the slot."""
    service = next(iter(_services(compose).values()))
    assert service.get("command")[:2] == ["gateway", "run"], (
        "the container must run the gateway role, or "
        "container_boot._is_gateway_container is false and no gateway-default "
        "slot is created"
    )


def test_the_supervised_dashboard_is_enabled_and_pinned_to_loopback(compose):
    """Host and port must both be explicit.

    The run script defaults to `0.0.0.0:9119`. Neither default is safe here: a
    non-loopback bind engages the auth gate, which fails closed with no
    provider registered, and the reverse proxy expects a specific port.
    """
    service = next(iter(_services(compose).values()))
    env = {}
    for entry in service.get("environment") or []:
        key, _, value = str(entry).partition("=")
        env[key] = value

    assert env.get("YOUTAB_AGENT_DASHBOARD") == "1", "the supervised dashboard is not enabled"
    assert "127.0.0.1" in env.get("YOUTAB_AGENT_DASHBOARD_HOST", ""), (
        "the dashboard must be pinned to loopback; the run script default is 0.0.0.0"
    )
    assert env.get("YOUTAB_AGENT_DASHBOARD_PORT"), (
        "the dashboard port must be explicit; the run script default is 9119"
    )


def test_the_docker_socket_is_never_mounted(compose):
    """The repair that must never be made.

    A dashboard with the Docker socket can control every container on the
    host, which is strictly more authority than the control it needs.
    """
    for name, service in _services(compose).items():
        for volume in service.get("volumes") or []:
            assert "docker.sock" not in str(volume), (
                f"service {name!r} mounts the Docker socket"
            )


def test_state_is_persistent(compose):
    service = next(iter(_services(compose).values()))
    mounts = [str(v) for v in (service.get("volumes") or [])]
    assert any(":/opt/data" in m for m in mounts), "no persistent /opt/data mount"


def test_a_health_check_exists(compose):
    """Truthful status needs something asking the process, not the UI cache."""
    service = next(iter(_services(compose).values()))
    assert service.get("healthcheck"), "no healthcheck declared"


def test_the_dashboard_only_role_still_refuses_lifecycle_control():
    """The skip is deliberate and must stay.

    Removing it to "fix" a dashboard-only container would reintroduce the
    restart storm the comment in container_boot describes.
    """
    from youtab_agent_cli.container_boot import _is_dashboard_container

    assert _is_dashboard_container(["dashboard", "--host", "127.0.0.1"])
    assert not _is_dashboard_container(["gateway", "run"])
