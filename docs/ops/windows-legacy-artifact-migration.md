# Windows legacy install to shared artifact channel

## Contract

Use one exact-pinned Windows x64 Setup and one immutable Runtime package for
every supported Windows x64 machine. Do not build per-device executables or
publish device-specific release pointers. User homes, credentials, profiles,
custom skills and chats remain local to their existing home.

A standalone Git installation predating artifact metadata requires a one-time
explicit transition: launch the shared customer Setup with `--migrate-legacy`.
This is the install flow, not `--update`, `--repair` or `--reinstall`. Close the
installed Desktop, CLI and other Setup windows before proceeding. Do not
uninstall the old application, rewrite its marker, reset Git or delete its
user home to make the transition pass.

The Setup must be built from the reviewed merged SHA with the approved release
base and exact embedded install script. The target immutable Runtime must be
published and verified before launch. Development, non-Windows and unpinned
source installations are not alternative customer migration paths.

## Validation and transaction

- The current root must be a directory containing a standalone `.git`
  directory and a regular canonical bootstrap marker with an exact source SHA.
- Git's current HEAD must be valid; the old marker's SHA must be an ancestor.
  Carried commits are allowed and retained. No fetch, reset or checkout runs.
- Any non-null artifact channel, sequence or artifact hash rejects this path,
  even if that metadata is incomplete or damaged. Normal artifact update
  checks continue to reject downgrades, reused sequences and foreign channels.
- An existing preserved legacy archive rejects a second migration. It is never
  overwritten or automatically removed.
- The normal verified artifact staging, install lock, executable lock probes,
  Runtime/Setup promotion, provisioning and health check still apply.
- Migration journals use schema 3 and bind the old Git HEAD. Ordinary
  transactions remain schema 2; old schema-2 journals remain recoverable.
- Before verified commit, failure or cancellation restores the previous
  Runtime/Setup pair. User state outside the Runtime is not rewritten.
- After verified commit, the entire old Runtime backup is renamed to the
  sibling `youtab-agent-runtime.legacy-preserved-v1`. This retains Git objects,
  carried commits, untracked files and the old environment for recovery.
- Interrupted cleanup verifies the retained Git identity before completing;
  ambiguous archives or changed identities fail closed and retain the journal
  and backups for review.

Do not remove the legacy archive as part of ordinary updates. After migration,
the installed marker comes from the verified artifact transaction and includes
the common release base, SHA, artifact hash and positive release sequence.
Subsequent About updates use the same channel as fresh installations.

## Acceptance

Isolated installer tests cover retained local commits and untracked files,
unchanged external user config, rollback of both Runtime and Setup, interrupted
archive promotion, archive collisions, changed source identity, invalid journals
and refusal to bypass artifact identity checks. They do not prove acceptance on
the operator's laptop.

Before declaring the transition successful on a real machine, verify the new
installed SHA/sequence and retained archive, startup, native login, real model
response, existing chats/custom skills and subsequent one-click update. Never
print credential contents or the private release prefix in that evidence.
