# P5 durable-execution — release / build-to-source provenance manifest

Scope: the running-service durability proof (RUNNING_SERVICE_PROOF.md). This
records exactly what was built, from what source, and the ONE provenance gap in
the current image plus its fix. Product remains NO-GO; nothing pushed/deployed.

## Source provenance (git)
| Field | Value |
|---|---|
| Source commit (P5 code) | `cc5c4f7d2` (durable-execution P5 closure) |
| Doc HEAD at manifest time | `1f68a381e2c30d4f349e3021c9d466d4380e1c72` |
| HEAD tree | `35ece42a272cbc82067c625c0462ed42887db4dd` |
| Branch | `feat/runtime-durable-execution-v1` (worktree `rt-durable-execution`) |
| Base | origin/main `c7650a1b` (runtime 0.19.1) |
| `uv.lock` sha256 | `a28248d41dac34e609672bf59a09094fd50ab30fe6d12453ada1bb71589b4f28` |
| postgres extra | `psycopg[binary]==3.3.6` (pyproject.toml:292) |

## Image provenance (docker)
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

## PROVENANCE GAP (open) — no embedded source SHA in the P5 image
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

## What the P5 image proved (as built, empty-SHA notwithstanding)
Live `gateway run` service on the real image: /v1/runs admission over HTTP,
durable status/result in PostgreSQL, a SUCCEEDED run with nonempty result
(`CUSTOMER_TASK_OK_42`) via a completing mock provider, restart recovery of that
SUCCEEDED run from PG, and PG-down fail-closed (listener never binds). See
RUNNING_SERVICE_PROOF.md and RUNNING_SERVICE_SUCCEEDED_RAW.txt.

## Coordinate with the master release candidate (OPEN)
The final RC image + shared deployment contract are Master-owned. This manifest is
the artifact to hand to Master: it pins source↔image, names the embedded-SHA fix,
and the additive override `docker-compose.postgres.yml` (PG backend + API ingress +
/health probe) is the proposed production config, pending Master adoption. No
push/merge/deploy performed here.
