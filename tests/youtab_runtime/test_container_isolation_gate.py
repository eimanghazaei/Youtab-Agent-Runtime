"""The isolation gate refuses each forbidden control, and admits a good one.

Every case below is a mutation: take the hardened Compose this repository
ships, break exactly one control, and require the gate to go red naming that
control. A gate that only ever sees a passing file proves nothing -- and a gate
that rejects everything proves nothing either, so the positive controls are
here for the same reason.

The gate reads parsed structure rather than text on purpose. `network_mode:
host` appears in this repository's own comments explaining why it was removed,
and a grep-based gate that failed on a comment is a gate people learn to route
around.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.youtab.container_isolation_gate import scan  # noqa: E402

COMPOSE = REPO / "docker-compose.yml"


def _controls(result: dict) -> set[str]:
    return {f["control"] for f in result["findings"]}


@pytest.fixture
def repo_copy(tmp_path):
    """A scratch root holding the real Compose and Dockerfile.

    The Dockerfile is copied rather than synthesised so the privilege-drop
    check runs against the image this product actually builds.
    """
    (tmp_path / "docker-compose.yml").write_text(
        COMPOSE.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "Dockerfile").write_text(
        (REPO / "Dockerfile").read_text(encoding="utf-8", errors="replace"),
        encoding="utf-8")
    return tmp_path


def mutate(root: Path, fn) -> dict:
    """Apply one change to the parsed Compose, write it back, rescan."""
    doc = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    service = next(iter(doc["services"].values()))
    fn(doc, service)
    (root / "docker-compose.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return scan(root)


# --- positive controls ------------------------------------------------------


class TestPositiveControls:
    """Without these, "reject everything" would score full marks."""

    def test_the_shipped_configuration_passes(self):
        result = scan(REPO)
        assert result["passed"] is True, result["findings"]

    def test_the_shipped_configuration_checked_real_controls(self):
        """A pass with nothing checked is not a pass."""
        result = scan(REPO)
        assert result["controls_checked"] >= 7
        assert result["blocking_count"] == 0

    def test_an_untouched_copy_still_passes(self, repo_copy):
        assert scan(repo_copy)["passed"] is True


# --- §19 mutation matrix ----------------------------------------------------


class TestMutationsTurnItRed:
    def test_host_networking(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.update(network_mode="host"))
        assert not r["passed"] and "no-host-network" in _controls(r)

    def test_privileged_mode(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.update(privileged=True))
        assert not r["passed"] and "no-privileged" in _controls(r)

    def test_host_pid(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.update(pid="host"))
        assert not r["passed"] and "no-host-pid" in _controls(r)

    def test_host_ipc(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.update(ipc="host"))
        assert not r["passed"] and "no-host-ipc" in _controls(r)

    @pytest.mark.parametrize("mount", [
        "/var/run/docker.sock:/var/run/docker.sock",
        "/run/containerd/containerd.sock:/run/containerd/containerd.sock",
    ])
    def test_container_runtime_socket(self, repo_copy, mount):
        r = mutate(repo_copy, lambda d, s: s["volumes"].append(mount))
        assert not r["passed"] and "no-host-mount" in _controls(r)

    @pytest.mark.parametrize("mount", [
        "/:/host", "/root:/root", "/home:/home", "/etc:/etc",
        "/proc:/hostproc", "/sys:/hostsys", "~/.ssh:/keys",
    ])
    def test_broad_host_mount(self, repo_copy, mount):
        r = mutate(repo_copy, lambda d, s: s["volumes"].append(mount))
        assert not r["passed"] and "no-host-mount" in _controls(r)

    def test_a_provider_key_in_the_environment(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s["environment"].append(
            "EXAMPLE_UPSTREAM_API_KEY=sk-" + "z" * 32))
        assert not r["passed"] and "no-secret-in-env" in _controls(r)

    def test_a_provider_key_passed_by_reference_is_still_refused(self, repo_copy):
        """`${VAR}` still puts the value in the container's environ."""
        r = mutate(repo_copy, lambda d, s: s["environment"].append(
            "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"))
        assert not r["passed"] and "no-secret-in-env" in _controls(r)

    @pytest.mark.parametrize("port", ["8081:8081", "0.0.0.0:8081:8081", "8081"])
    def test_public_dashboard_binding(self, repo_copy, port):
        r = mutate(repo_copy, lambda d, s: s.update(ports=[port]))
        assert not r["passed"] and "loopback-publish" in _controls(r)

    def test_an_unresolvable_port_fails_closed(self, repo_copy):
        """Ambiguity is refused, not assumed benign."""
        r = mutate(repo_copy, lambda d, s: s.update(ports=["${BIND_ADDR}:8081:8081"]))
        assert not r["passed"] and "loopback-publish" in _controls(r)

    def test_missing_read_only_root(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.pop("read_only", None))
        assert not r["passed"] and "read-only-root" in _controls(r)

    def test_missing_capability_drop(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.pop("cap_drop", None))
        assert not r["passed"] and "cap-drop-all" in _controls(r)

    def test_a_capability_added_back(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.update(cap_add=["SYS_ADMIN"]))
        assert not r["passed"] and "no-cap-add" in _controls(r)

    def test_missing_no_new_privileges(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.pop("security_opt", None))
        assert not r["passed"] and "no-new-privileges" in _controls(r)

    def test_missing_cpu_limit(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.pop("cpus", None))
        assert not r["passed"] and "cpu-limit" in _controls(r)

    def test_missing_memory_limit(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.pop("mem_limit", None))
        assert not r["passed"] and "memory-limit" in _controls(r)

    def test_missing_pid_limit(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.pop("pids_limit", None))
        assert not r["passed"] and "pid-limit" in _controls(r)

    def test_missing_restart_policy(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.pop("restart", None))
        assert not r["passed"] and "restart-policy" in _controls(r)

    def test_missing_healthcheck(self, repo_copy):
        r = mutate(repo_copy, lambda d, s: s.pop("healthcheck", None))
        assert not r["passed"] and "healthcheck" in _controls(r)


# --- fails closed on bad input ---------------------------------------------


class TestFailsClosed:
    def test_a_missing_compose_file_is_a_failure(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM x\ns6-setuidgid\n", encoding="utf-8")
        r = scan(tmp_path)
        assert not r["passed"] and "compose-present" in _controls(r)

    def test_malformed_compose_is_a_failure(self, repo_copy):
        (repo_copy / "docker-compose.yml").write_text(
            "services: [unclosed\n", encoding="utf-8")
        r = scan(repo_copy)
        assert not r["passed"] and "compose-parses" in _controls(r)

    def test_compose_with_no_services_is_a_failure(self, repo_copy):
        (repo_copy / "docker-compose.yml").write_text(
            "services: {}\n", encoding="utf-8")
        r = scan(repo_copy)
        assert not r["passed"] and "compose-services" in _controls(r)

    def test_a_missing_dockerfile_is_a_failure(self, repo_copy):
        (repo_copy / "Dockerfile").unlink()
        r = scan(repo_copy)
        assert not r["passed"] and "dockerfile-present" in _controls(r)

    def test_an_image_that_never_drops_privilege_is_a_failure(self, repo_copy):
        """s6 may start as root; the long-running services may not stay root."""
        (repo_copy / "Dockerfile").write_text(
            "FROM scratch\nUSER root\nENTRYPOINT [\"/init\"]\n", encoding="utf-8")
        r = scan(repo_copy)
        assert not r["passed"] and "privilege-drop" in _controls(r)


# --- the gate reads structure, not text -------------------------------------


def test_a_comment_mentioning_host_networking_does_not_fail_the_gate(repo_copy):
    """The repository's own comments explain why host networking was removed.

    A gate that failed on those would be one people work around rather than
    with, so it parses the document instead of scanning it.
    """
    path = repo_copy / "docker-compose.yml"
    path.write_text(
        "# network_mode: host was removed here on purpose\n"
        + path.read_text(encoding="utf-8"),
        encoding="utf-8")
    assert scan(repo_copy)["passed"] is True
