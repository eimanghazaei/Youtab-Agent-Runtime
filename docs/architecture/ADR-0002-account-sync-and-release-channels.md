# ADR 0002: Account-scoped history and shared release channels

Status: proposed; local implementation under review, no production rollout.

## Decision

Reuse Gateway's stable user/tenant/workspace principal and native session
authority. Email is a sign-in attribute, never a storage or authorization key.
Device sessions remain independent and revocable. Owner opted for all existing
and new chat text to sync after consent, with original dates and preserved local
history. Files, tools, machine credentials and Brain authority do not sync.

Use encrypted scoped payloads, a dedicated sync token audience, durable offline
revisions, explicit deletion tombstones and conflict copies. Large histories
travel as bounded text parts followed by a root, reconstructed as one chat.
Part reclamation must preserve every live root's references. Capacity failures
remain visible and cannot authorize deleting originals.

One Windows x64 Setup serves Pilot and Stable. Channel is an explicit local
delivery preference. Build the candidate once, test Pilot, then promote the
same manifest/Runtime/Setup hashes to Stable. About verifies and stages matching
Setup before handoff; existing installer transaction owns promotion/rollback.
Old Git installations require a preserved GUI migration once.

## Consequences and acceptance

Gateway schema and independent encryption secret require a controlled rollout.
Private organizational history does not imply team sharing or SSO provisioning.
Production capacity/performance, least privilege, key rotation and both real
device acceptance remain required. A passing focused test is not a green
publication gate. No feature deployment or publication has occurred.

Details and current limitations: `docs/ops/account-sync-and-release-channels.md`.
