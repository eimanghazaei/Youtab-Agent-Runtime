# Account sync and shared Setup delivery

Status: implementation in progress; not a production readiness claim.

Owner scope: one Windows x64 Setup, Pilot on the development laptop, Stable
on the qualification PC and customer devices; authenticated cross-device
chat/settings sync. Preserve existing local data and immutable releases.

## Identity and data

Reuse the Gateway's database-fresh native principal. Bind every sync record to
`tenant_id`, `user_id`, and `workspace_id`; email and device/IP are not keys.
Private account data is owner-scoped even within an organization. Shared
organizational data requires a separate membership/role contract.

Sync is opt-in. The Owner selected ALL existing and future chats, using original
conversation/message dates, including archived and compressed conversations.
Discovery keys a conversation by its compression root and continues in durable
batches; migration never deletes local originals. Start with user/assistant text transcripts and an explicit
preference allowlist. Credentials, provider keys, machine paths, tool traces,
files, plugins' secrets and local databases are not uploaded. Preserve local
originals. Account switching must stop in-flight work and quarantine the old
account's cursor/cache. Encrypt remote payloads at rest with context binding.

Use optimistic revisions and durable deletion tombstones. A stale device must
not restore deleted conversations or silently overwrite concurrent edits.
Offline operation queues changes; conflict resolution must preserve both
versions until the user chooses. Consent must be visible before first upload.

## Releases

Retain `<base>/latest.json` for existing installations. Add independent
`<base>/channels/pilot/latest.json` and `channels/stable/latest.json` pointers.
All point to the same immutable SHA manifest and identical Runtime/Setup bytes.
Default customer channel is Stable; channel selection is an explicit local
setting, independent of account sync, and never inferred from machine identity.

Build once; qualify Pilot; promote its exact manifest and byte hashes to Stable
without rebuilding. Promotion checks an expected current Stable pointer and a
qualification record bound to Runtime hash, Setup hash and source SHA.
Keep separate per-channel sequence floors; prohibit downgrade/reused sequence.

A manifest must describe a versioned Setup URL, SHA-256, source SHA, size and
updater protocol. About verifies immutable metadata and Setup bytes before
launch. Never overwrite a running executable. Launch a verified sibling helper;
the existing Runtime+Setup transaction owns promotion, rollback and recovery.
The legacy bridge is available from the UI, preserves the complete old Git
installation, and never rewrites its marker to simulate a successful migration.

## Delivery gates

Unit and integration coverage: authorization, cross-account/workspace denial,
revoked sessions, deletion after offline replay, concurrent writes, token expiry,
partial downloads, tampered Setup, release/channel mismatch and rollback.
Local Semgrep before any push; mandatory exact-head review, OWASP and smoke.
No publication/deployment until these integrations and real two-device acceptance
are complete. Missing server/schema/key configuration is a failure, not PASS.

## Local implementation checkpoint

- `scripts/release_channels.py`: offline byte-verifying Pilot/Stable promotion;
  legacy `latest.json` is preserved. Caller must hold the publication lock.
- `electron/release-delivery.ts`: channel URLs, immutable metadata/Setup binding,
  size/hash/x64 PE checks, verified sibling staging without replacing a running
  executable. About now stages the verified helper before stopping the backend;
  the native transaction installs the matching Runtime and Setup together.
- Native token parser retains the dedicated sync credential inside the existing
  encrypted main-process token store; no provider bearer reuse is introduced.

Implemented locally: common Setup metadata in the artifact manifest; persisted
channel selection (including same-version changes); marker-bound Desktop
channel preferences; About handoff; preserved legacy GUI upgrade; byte-identical
Pilot-to-Stable promotion with qualification and pointer CAS/rollback.
`scripts/publish_release_channel.py` owns the publication lock, verifies public
immutable hashes and mutable pointer cache behavior, and bridges Stable to the
legacy pointer only after qualification. It has not been executed on the VPS.

The main process owns a scoped encrypted journal, durable outbox/cursor,
account/session cancellation, all-history discovery, background sync and
inert continuation. Long histories use bounded immutable text parts; roots
publish after parts, and superseded parts are reclaimed only after checking
references locally and on the server. Conflict roots keep both histories.
Language/appearance preferences are explicit account actions; machine keys,
installation channel and local execution authority remain local.

Bounds are explicit: 100,000 messages and 16 MiB JSON per logical transcript,
262,144 characters per message; 1 MiB/2,000 messages per wire part; 32 MiB
encrypted account storage and 100,000 records on Gateway; durable Desktop
store 64 MiB plain/128 MiB encrypted envelope. Capacity/export failures must
show incomplete migration and preserve originals, never silently truncate.
These are not an unlimited-storage promise. Production capacity policy and
performance qualification for large accounts remain required.

Remaining release work: clean exact-source common Setup build and independent
review; required gates on supported platforms; controlled shared-Setup
migration on the laptop; actual two-channel/two-device qualification; server
schema/key rollout under separate Owner approval. Existing live sequence 3 is
unchanged. The Windows whole-repository run currently has failures, so focused
passing tests do not establish a green PR or customer readiness.

Infrastructure qualification must cover all three pointer URLs: legacy,
`channels/pilot/latest.json` and `channels/stable/latest.json`. Each needs origin
`no-store` and Cloudflare cache bypass. The previous rule matching only
`/releases/latest.json` does not qualify the new channel indexes. Immutable SHA
artifacts retain their existing immutable cache policy.
