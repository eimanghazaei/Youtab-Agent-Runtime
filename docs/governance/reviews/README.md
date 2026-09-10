# Independent Agent Review — Evidence Ledger

Durable, append-only ledger of independent code-review results. See the policy in
[`../INDEPENDENT_AGENT_REVIEW.md`](../INDEPENDENT_AGENT_REVIEW.md).

## What lives here

One machine-readable evidence record per reviewed SHA, named `<head_sha>.json`,
carrying the §6 fields and the §4 verdict matrix:

```
repository, pr_number, base_branch, base_sha, head_branch, head_sha,
review_agent_identity, review_timestamp_utc, review_scope,
commands_and_tests_examined, findings_by_severity, unresolved_findings, verdict
```

The companion **commit status** (context `Independent Agent Review`) is the
machine-enforced gate (posted against the exact head SHA); this file is the durable
human/audit record. Use `scripts/youtab/independent_agent_review.py` to write a record
and post the status.

## Rules

- **Append-only.** Never overwrite or delete an earlier review record. A new SHA gets
  a new file; a superseded review remains part of the historical record.
- **Exact-SHA binding.** A record is valid only for its `head_sha`. Any new commit,
  rebase, or merge-from-base invalidates it; the gate returns to pending for the new
  SHA. A record for an earlier SHA never satisfies a later SHA.
- **Independence.** The `review_agent_identity` must not be the implementing agent.

## Greptile history (superseded)

Greptile ceased to be an active review gate on **2026-09-10** (Owner decision).
Historical Greptile check-runs and PR comments are preserved as historical evidence
and are **inactive** — they carry no merge/release/deployment authority. Do not delete
or rewrite them.
