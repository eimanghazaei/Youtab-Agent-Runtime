#!/usr/bin/env python3
"""Structured isolation gate for the trusted control plane.

Reads the deployment description as *data* -- parsed Compose, parsed
Dockerfile -- and refuses a configuration that would hand the container more
authority than the control plane needs. Not a grep: `network_mode: host`
appears in this repository's own comments explaining why it was removed, and a
text scan that fails on a comment is a gate people learn to route around.

Fails closed, everywhere
------------------------
An input that is missing, unparseable, empty, or ambiguous is a FAIL, never a
skip. That includes the case this gate was most likely to get wrong: a port
binding whose host address comes from an unresolved `${VAR}`. "It probably
resolves to loopback" is not a finding, so it is refused until it is written
down.

Scope
-----
This is the *control plane*: the one s6-supervised container holding the
dashboard and gateway, which must keep sharing `/run/service` for the
lifecycle contract to work. The untrusted execution sandbox is a separate
boundary and a later slice; nothing here should be read as covering it.

    python3 scripts/youtab/container_isolation_gate.py --root . --output x.json

Exit 0 = every control satisfied. Exit 1 = at least one violation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - the gate cannot run without it
    print(json.dumps({"passed": False, "errors": ["PyYAML is not installed"]}))
    sys.exit(1)


SEVERITY_BLOCK = "BLOCK"

#: Host paths that must never be bind-mounted into the control plane. A mount
#: of any of these is either the Docker API, the host's credentials, or the
#: host filesystem wholesale.
FORBIDDEN_MOUNT_SOURCES = (
    "/var/run/docker.sock", "/run/docker.sock", "docker.sock",
    "/run/containerd/containerd.sock", "containerd.sock",
    "/etc", "/proc", "/sys", "/boot", "/dev",
    "/root", "/home", "~/.ssh", "~/.aws", "~/.docker", "~/.kube",
)

#: Environment variable names that must not carry a provider credential into
#: the container. Matched on shape, so a provider added later is still caught.
SECRET_NAME_RE = re.compile(
    r"(API_KEY|_KEY$|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY)",
    re.IGNORECASE,
)
#: Names that look secret-shaped but are non-secret configuration. Each is an
#: explicit, reviewed exception rather than a pattern hole.
SECRET_NAME_ALLOW = frozenset({
    "YOUTAB_ACCESS_AUD",          # public Access audience tag
    "YOUTAB_ACCESS_TEAM_DOMAIN",  # public team hostname
})

#: A value that is obviously a reference rather than a literal secret.
_SECRET_REF_RE = re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$")


class Finding(dict):
    def __init__(self, control: str, detail: str, where: str) -> None:
        super().__init__(control=control, detail=detail, where=where,
                         severity=SEVERITY_BLOCK)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _env_pairs(service: dict) -> list[tuple[str, str]]:
    """Compose accepts both list and mapping forms for `environment`."""
    env = service.get("environment")
    pairs: list[tuple[str, str]] = []
    if isinstance(env, dict):
        pairs = [(str(k), "" if v is None else str(v)) for k, v in env.items()]
    else:
        for entry in _as_list(env):
            key, _, val = str(entry).partition("=")
            pairs.append((key, val))
    return pairs


def check_compose(path: Path) -> tuple[list[Finding], list[str]]:
    """Every control that is expressed in the Compose file."""
    findings: list[Finding] = []
    checked: list[str] = []

    if not path.is_file():
        return [Finding("compose-present", f"{path} not found", str(path))], checked
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [Finding("compose-parses", f"unparseable: {exc}", str(path))], checked
    if not isinstance(doc, dict):
        return [Finding("compose-parses", "not a mapping", str(path))], checked

    services = doc.get("services")
    if not isinstance(services, dict) or not services:
        return [Finding("compose-services", "no services declared", str(path))], checked

    for name, service in services.items():
        where = f"services.{name}"
        if not isinstance(service, dict):
            findings.append(Finding("service-shape", "service is not a mapping", where))
            continue

        # --- network -----------------------------------------------------
        checked.append(f"{where}:network")
        if str(service.get("network_mode", "")).strip().lower() == "host":
            findings.append(Finding(
                "no-host-network",
                "network_mode: host gives the container the host's network stack, "
                "including every loopback-bound service on the box",
                where))

        # --- privilege ---------------------------------------------------
        checked.append(f"{where}:privilege")
        if service.get("privileged") is True:
            findings.append(Finding("no-privileged", "privileged: true", where))
        for key in ("pid", "ipc"):
            if str(service.get(key, "")).strip().lower() == "host":
                findings.append(Finding(f"no-host-{key}", f"{key}: host", where))
        for cap in _as_list(service.get("cap_add")):
            findings.append(Finding("no-cap-add", f"cap_add: {cap}", where))

        # --- mounts ------------------------------------------------------
        checked.append(f"{where}:mounts")
        for volume in _as_list(service.get("volumes")):
            src = (str(volume.get("source", "")) if isinstance(volume, dict)
                   else str(volume).split(":", 1)[0])
            norm = src.strip().rstrip("/") or src.strip()
            for bad in FORBIDDEN_MOUNT_SOURCES:
                if norm == bad or norm.endswith(bad) or norm.startswith(bad + "/"):
                    findings.append(Finding(
                        "no-host-mount", f"mounts host path {src!r}", where))
                    break
            else:
                # A bare `/` or a bind of the whole host tree.
                if norm == "" or norm == "/":
                    findings.append(Finding(
                        "no-host-mount", "mounts the host root", where))

        # --- hardening ---------------------------------------------------
        checked.append(f"{where}:hardening")
        if service.get("read_only") is not True:
            findings.append(Finding(
                "read-only-root",
                "read_only: true is not set; a writable image layer lets a "
                "compromise persist across a restart",
                where))
        cap_drop = {str(c).upper() for c in _as_list(service.get("cap_drop"))}
        if "ALL" not in cap_drop:
            findings.append(Finding("cap-drop-all", "cap_drop does not include ALL", where))
        sec_opt = [str(s) for s in _as_list(service.get("security_opt"))]
        if not any(s.replace(" ", "") == "no-new-privileges:true" for s in sec_opt):
            findings.append(Finding(
                "no-new-privileges", "security_opt lacks no-new-privileges:true", where))

        # --- resource limits ---------------------------------------------
        checked.append(f"{where}:limits")
        deploy_limits = (((service.get("deploy") or {}).get("resources") or {})
                         .get("limits") or {})
        if not (service.get("cpus") or deploy_limits.get("cpus")):
            findings.append(Finding("cpu-limit", "no CPU limit", where))
        if not (service.get("mem_limit") or deploy_limits.get("memory")):
            findings.append(Finding("memory-limit", "no memory limit", where))
        if not service.get("pids_limit"):
            findings.append(Finding("pid-limit", "no pids_limit", where))

        # --- availability -------------------------------------------------
        checked.append(f"{where}:availability")
        if not service.get("restart"):
            findings.append(Finding("restart-policy", "no restart policy", where))
        if not service.get("healthcheck"):
            findings.append(Finding("healthcheck", "no healthcheck", where))

        # --- published ports ----------------------------------------------
        checked.append(f"{where}:ports")
        for port in _as_list(service.get("ports")):
            findings.extend(_check_port(port, where))

        # --- secrets in the environment -----------------------------------
        checked.append(f"{where}:environment")
        for key, value in _env_pairs(service):
            if key in SECRET_NAME_ALLOW or not SECRET_NAME_RE.search(key):
                continue
            if value and not _SECRET_REF_RE.match(value.strip()):
                findings.append(Finding(
                    "no-secret-in-env",
                    f"{key} carries a literal value; provider credentials must "
                    "come from the broker, not the container environment",
                    where))
            else:
                findings.append(Finding(
                    "no-secret-in-env",
                    f"{key} is delivered through the container environment; a "
                    "process can read its own environ",
                    where))

    return findings, checked


def _check_port(port: Any, where: str) -> list[Finding]:
    """A published port must name an explicit loopback host address."""
    if isinstance(port, dict):
        host_ip = str(port.get("host_ip", "")).strip()
        if not host_ip:
            return [Finding("loopback-publish",
                            "published port declares no host_ip", where)]
        if host_ip not in ("127.0.0.1", "::1"):
            return [Finding("loopback-publish",
                            f"published on {host_ip}, not loopback", where)]
        return []

    text = str(port).strip()
    if "$" in text:
        # Unresolved interpolation: the bind address is unknown, so this is
        # ambiguous input and ambiguity fails closed.
        return [Finding("loopback-publish",
                        f"port {text!r} is not statically resolvable", where)]
    parts = text.split(":")
    if len(parts) < 3:
        return [Finding("loopback-publish",
                        f"port {text!r} has no explicit host address, so Docker "
                        "publishes it on every interface", where)]
    host_ip = parts[0]
    if host_ip not in ("127.0.0.1", "::1"):
        return [Finding("loopback-publish",
                        f"published on {host_ip!r}, not loopback", where)]
    return []


def check_dockerfile(path: Path) -> tuple[list[Finding], list[str]]:
    """The image's own privilege posture.

    s6 legitimately starts as root to build the supervision tree, so `USER
    root` is not itself a violation. What must be true is that the long-running
    services drop to a dedicated account, which in this image is
    `s6-setuidgid`. The gate requires the evidence to be present rather than
    assuming it.
    """
    findings: list[Finding] = []
    checked = ["dockerfile:privilege-drop"]
    if not path.is_file():
        return [Finding("dockerfile-present", f"{path} not found", str(path))], checked
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [Finding("dockerfile-readable", str(exc), str(path))], checked

    if "s6-setuidgid" not in text:
        findings.append(Finding(
            "privilege-drop",
            "no s6-setuidgid in the image; long-running services would stay root",
            str(path)))
    return findings, checked


def scan(root: Path) -> dict:
    compose_findings, compose_checked = check_compose(root / "docker-compose.yml")
    docker_findings, docker_checked = check_dockerfile(root / "Dockerfile")
    findings = compose_findings + docker_findings
    checked = compose_checked + docker_checked
    return {
        "schema_version": 1,
        "passed": not findings,
        "controls_checked": len(checked),
        "checked": checked,
        "findings": findings,
        "blocking_count": len(findings),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="container-isolation-gate")
    ap.add_argument("--root", default=".")
    ap.add_argument("--output")
    args = ap.parse_args(argv)

    result = scan(Path(args.root))
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")

    if result["passed"]:
        print(f"PASS: {result['controls_checked']} isolation controls satisfied")
        return 0
    print(f"FAIL: {result['blocking_count']} isolation violation(s)")
    for f in result["findings"]:
        print(f"  {f['severity']}  {f['control']:22} {f['where']}  {f['detail']}")
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
