# P5 durable-execution — release / build-to-source provenance manifest

Scope: the running-service durability proof (RUNNING_SERVICE_PROOF.md). This
records exactly what was built, from what source, and the ONE provenance gap in
the current image plus its fix. Product remains NO-GO; nothing pushed/deployed.

## Source provenance (git)
| Field | Value |
|---|---|
| RC source commit | `5b371ccb2d7888b0b429b907f47d91b666ec31e8` (embedded in RC image) |
| Prior P5 code | `cc5c4f7d2` (original image, provenance-gapped) |
| Branch | `feat/runtime-durable-execution-v1` (worktree `rt-durable-execution`) |
| Base | origin/main `c7650a1b` (runtime 0.19.1) |
| `uv.lock` sha256 | `a28248d41dac34e609672bf59a09094fd50ab30fe6d12453ada1bb71589b4f28` |
| postgres extra | `psycopg[binary]==3.3.6` (pyproject.toml:292) |

## RC image provenance (docker) — SELF-DESCRIBING (embedded SHA), digest-pinnable
| Field | Value |
|---|---|
| Tag | `youtab-agent-runtime:p5-rc-5b371ccb2` |
| Image Id (config digest) | `sha256:4389e5f44e657e264d66dd6e9d9828a46c88d9e967d4f5598aebca6f001076d3` |
| Distribution tarball sha256 (`docker save`) | `844de14ce53ff0334b346942b5eb0d7b17542d786618d6c6ca87e7b7dd05a0bb` |
| Registry repo digest | `<PUBLISHED_REPO_DIGEST>` — filled by Master at publish; customers pin `<repo>@sha256:<that>` |
| Embedded source SHA | `5b371ccb2d7888b0b429b907f47d91b666ec31e8` (== HEAD; PROVENANCE MATCH ✅) |
| Built with | `--build-arg YOUTAB_AGENT_GIT_SHA=<HEAD>` |
| Build log | `docs/evidence/p5_deploy/p5_rc_image_build.log` (build exit 0) |

Customer handoff pins the image by digest (registry repo digest, or the tarball
sha256 for offline `docker load`) — see ADMIN_INSTALL_PROCEDURE.md. Superseded RC
`p5-rc-cad5ec26d` was removed locally after the terminal-durability fix.

### Prior (superseded) P5 image — provenance gap, kept for the record
| Field | Value |
|---|---|
| Tag | `youtab-agent-runtime:p5-cc5c4f7d` |
| Image Id | `sha256:abee28872c4aff7c8dd92b36a82f160517d4db98bd77fa24c93daf613edbde08` |
| Created | 2026-09-23T09:17:46Z |
| Size | 4,494,293,974 B (4.49 GB) |
| Runtime base | `debian:13.4` @ `sha256:e2d08da6f42ef4b09b165d55528a12727aeed8240dc9edf888e3ec07e10ef9da` |
| uv builder | `ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie` @ `sha256:b3c543b6c4f23a5f2df22866bd7857e5d304b67a564f4feab6ac22044dde719b` |
| node builder | `node:22-bookworm-slim` @ `sha256:7af03b14a13c8cdd38e45058fd957bf00a72bbe17feac43b1c15a689c029c732` |
| Build log | `docs/evidence/p5_deploy/p5_image_build.log` (build exit 0) |

## PROVENANCE GAP — CLOSED for the RC image
The RC image `p5-rc-cad5ec26d` was built with
`--build-arg YOUTAB_AGENT_GIT_SHA=$(git rev-parse HEAD)`, so
`/opt/youtab/.youtab_agent_build_sha` == `cad5ec26d…` (verified). The RC image is
self-describing. The section below documents the gap in the ORIGINAL P5 image
(`p5-cc5c4f7d`) and the fix, which is now applied.

## PROVENANCE GAP (original p5-cc5c4f7d image) — no embedded source SHA
The Dockerfile embeds the source commit at `/opt/youtab/.youtab_agent_build_sha`
ONLY when built with `--build-arg YOUTAB_AGENT_GIT_SHA=<sha>` (Dockerfile:334-336).
The P5 image was built WITHOUT that build-arg, so:
- build log shows an empty embedded SHA, and
- OBSERVED in the running image: `/opt/youtab/.youtab_agent_build_sha` is **absent**
  (`docker run --rm --entrypoint sh … cat …` → "No such file", exit 1).

So the current image carries **no self-describing build-to-source link**; the
cc5c4f7d↔image binding in this manifest is external (this document), not embedded.

### Fix (for the release-candidate rebuild — reproducible)
Rebuild passing the exact commit as the build-arg so the SHA is embedded and the
image is self-describing:
```
GIT_SHA=$(git rev-parse HEAD)
docker build \
  --build-arg YOUTAB_AGENT_GIT_SHA="$GIT_SHA" \
  -t youtab-agent-runtime:<tag> .
# verify:
docker run --rm --entrypoint sh youtab-agent-runtime:<tag> \
  -c 'cat /opt/youtab/.youtab_agent_build_sha'   # -> $GIT_SHA
```
Recommended additionally (not blocking): OCI labels
`org.opencontainers.image.revision=$GIT_SHA` and `...image.source=<repo url>` —
the image currently has `Config.Labels == null`.

## What the RC image proved (live `gateway run` service)
- /v1/runs admission over HTTP; durable status/result in PostgreSQL; a SUCCEEDED
  run with nonempty result (`CUSTOMER_TASK_OK_42`) via a completing mock provider;
  restart recovery of the SUCCEEDED run from PG.
- Secure external route: api_server NOT host-published (only a TLS Caddy edge is),
  bearer enforced end-to-end, SUCCEEDED run through the edge over verified TLS1.3.
- PostgreSQL loss fail-closed at startup (listener never binds) AND mid-flight
  (`/health/ready` → 503, `POST /v1/runs` → 503 `durable_store_unavailable`, no
  in-memory-only 202); recovery after PG restart. Store-aware `/health/ready`
  distinct from static `/health` liveness.
- No false-durable terminal completion: PG lost after admission but before the
  terminal write → client sees reconciliation_required (terminal=false), store
  stays RUNNING, nothing vanishes after restart.
- Secure external TLS route qualified for LOCAL admin (loopback); cross-host
  exposure is OPEN (unsandboxed backend — TLS+Bearer are not a sandbox).
See RUNNING_SERVICE_PROOF.md, RUNNING_SERVICE_SUCCEEDED_RAW.txt,
SECURE_INGRESS_AND_PGLOSS_RAW.txt, TERMINAL_DURABILITY_RAW.txt,
ADMIN_INSTALL_PROCEDURE.md.

## Coordinate with the master release candidate (OPEN)
The final RC image + shared deployment contract are Master-owned. This manifest is
the artifact to hand to Master: it pins source↔image, names the embedded-SHA fix,
and the additive override `docker-compose.postgres.yml` (PG backend + API ingress +
/health probe) is the proposed production config, pending Master adoption. No
push/merge/deploy performed here.
