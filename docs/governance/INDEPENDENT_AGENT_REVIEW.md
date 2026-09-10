# Independent Agent Review — Mandatory Review Gate

**Status:** ACTIVE · **Effective:** 2026-09-10 · **Owner decision (permanent).**

This document is the authoritative governance record for the code-review gate of
the Youtab Agent Runtime repository. It **replaces Greptile** as the review gate.

---

## 0. Greptile retirement (historical record preserved)

- **2026-09-10:** By permanent Owner decision, **Greptile is no longer used** and is
  **no longer a required merge or deployment gate.** No process may wait for
  Greptile, require a Greptile confidence score, require Greptile comments/approval,
  block a PR because Greptile did not run, re-trigger Greptile, or cite Greptile as
  current approval evidence.
- **Historical Greptile reports and comments are preserved** as historical evidence
  (e.g. the existing Greptile review comments on PR #41 and its check-runs). They are
  **inactive and superseded** by this policy and carry **no authority** over merge,
  release, or deployment from 2026-09-10 onward. Do not delete or rewrite them.
- Greptile was an advisory GitHub-App check; it was **never** part of the
  `main-required-gates` ruleset (see §9). Its removal does not weaken any other
  security, testing, CI, or Owner-authorization gate — all of those remain in force.

---

## 1. The gate

The required status check is named exactly:

```
Independent Agent Review
```

Every PR MUST pass this gate before it may: leave Draft · receive merge
authorization · be merged · become a deployment candidate · be deployed to staging
or production.

A green `Independent Agent Review` gate **does not** by itself authorize merge or
deployment — a separate **exact-SHA Owner authorization** is always additionally
required (see §8, §10).

## 2. Reviewer independence

The review MUST be performed by at least one independent Review Agent that:

- did **not** implement the reviewed changes;
- operates **read-only** during the review (does not modify code while reviewing);
- reviews the **complete PR diff**;
- verifies the **exact PR head SHA and base SHA**;
- examines code, tests, security boundaries, and relevant documentation;
- reports findings **without assuming that green CI proves correctness**.

The implementation agent may coordinate the process but **cannot approve its own
work**. If the reviewer finds a defect, the review **fails**: an implementation agent
fixes it, tests are rerun, a **new** independent review is performed on the new SHA.

## 3. Exact-SHA binding

The review result is valid **only for the exact reviewed head SHA**. Any new commit,
rebase, conflict resolution, merge-from-base, or history rewrite **invalidates** the
previous review; the gate returns to **pending** until an independent Review Agent
reviews the new exact SHA. **A review of an earlier SHA never satisfies the gate for
a later SHA.**

## 4. Verdict vocabulary

The reviewer uses only:

```
PROVEN | FAILED | NOT PROVEN | BLOCKED
```

The `Independent Agent Review` check may pass **only** when:

- all required review areas are `PROVEN`;
- no Critical, High, or Medium finding remains unresolved;
- relevant tests are green;
- no security control was weakened;
- no secret is exposed;
- the diff matches the declared scope;
- the reviewed SHA **equals** the current remote PR head SHA.

`NOT PROVEN`, `FAILED`, or `BLOCKED` **fail closed** and prevent merge and deployment.

## 5. Minimum review scope

The Review Agent inspects, as applicable, citing file/function anchors and the
evidence for each conclusion:

correctness & regression risk · authentication & authorization · tenant/workspace
isolation · secret handling & redaction · persistence & transaction boundaries ·
retry/pause/resume/cancel/recovery · engine/model binding & provenance ·
worker/process lifecycle · Linux & Windows compatibility · API/UI contract
compatibility · benchmark & evidence integrity · dependency & license changes ·
migrations & rollback safety · production configuration changes · test adequacy
(including whether the tests would fail on the defective implementation).

## 6. Evidence recorded (per review)

```
repository | PR number | base branch | base SHA | head branch | head SHA
review agent identity | review timestamp (UTC) | review scope
commands and tests examined | findings by severity | unresolved findings
final verdict
```

## 7. GitHub evidence (durable, SHA-bound, non-self-referential)

The review result is published in GitHub tied to the exact SHA, via a **commit
status / check** and a structured PR review report — **not** by committing the
report into the same SHA it reviews.

- **Machine-readable gate:** a commit **status** with context `Independent Agent
  Review` and `state=success|failure|pending`, posted against the exact head SHA
  (see `scripts/youtab/independent_agent_review.py`).
- **Human-readable report:** a structured PR review/comment carrying the §6 evidence
  and the §4 verdict matrix, and a durable evidence record under
  `docs/governance/reviews/` (recorded out-of-band; see that directory's README).
- **Never overwrite or delete earlier review reports.** Superseded reports remain
  part of the historical record.

## 8. Pre-deployment verification

Before staging or production deployment, verify:

- the deployment candidate corresponds **exactly** to the independently reviewed code;
- required CI is green;
- `Independent Agent Review` is green **for the exact deployed SHA**;
- the artifact digest and SBOM correspond to the reviewed source;
- no code or configuration mutation occurred after review;
- rollback and smoke-test procedures are ready;
- the Owner has authorized the **exact** deployment SHA or immutable artifact digest.

If the merge strategy creates a different commit SHA, prove its **tree equivalence**
to the reviewed PR head; if equivalence cannot be proven, a **fresh** independent
review is required. **No deployment from an unreviewed artifact.**

## 9. Branch protection / required-check configuration

The merge gate for `main` is the ruleset **`main-required-gates`**
(`repos/eimanghazaei/Youtab-Agent-Runtime/rulesets/20931081`). As of 2026-09-10 its
required status contexts are:

```
python-security, javascript, wake-word-backends (ubuntu-latest),
wake-word-backends (windows-latest), wake-word-backends (macos-latest),
windows-runtime-cli, windows-tools, benchmark-deterministic
```

- **Greptile context to remove:** *none* — Greptile was never in this ruleset (it was
  an advisory GitHub-App check only). No enforced Greptile gate exists to remove.
- **New required context to add:** `Independent Agent Review`.

Adding a required-check context is a repository-settings/admin operation and is **not
applied by this change** (it affects every PR targeting `main` and must be an Owner/
admin decision). **Required Owner/admin action** (exact command):

```bash
# 1. Read the current ruleset
gh api repos/eimanghazaei/Youtab-Agent-Runtime/rulesets/20931081 > ruleset.json
# 2. Add {"context":"Independent Agent Review"} to the
#    rules[].parameters.required_status_checks[] array (type == required_status_checks),
#    keeping all existing contexts, then:
gh api -X PUT repos/eimanghazaei/Youtab-Agent-Runtime/rulesets/20931081 \
  --input ruleset.json
```

Do **not** temporarily remove all review protection. Until the Owner/admin adds the
context, `Independent Agent Review` is enforced **procedurally** (this policy) and via
the posted commit status; the automated required-context enforcement becomes active
once the ruleset is updated.

## 10. Application to a PR (the flow)

1. Implementation lands on the PR branch; CI runs on the exact remote head SHA.
2. After required CI finishes, an **independent read-only Review Agent** reviews the
   complete diff bound to that exact remote head SHA (§2–§6).
3. The review report is published in GitHub (§7), and the `Independent Agent Review`
   commit status is set to `success` **only if** every §4 condition holds; otherwise
   `failure`/`pending` (fail closed).
4. The PR stays **Draft** with **auto-merge disabled** until the Owner authorizes.
5. Any new corrective commit **invalidates** the review; repeat from step 1 on the new
   SHA.
6. Merge/deploy require a **separate exact-SHA Owner authorization** in addition to a
   green gate.

## 11. Status vocabulary for rollout

Track this governance change with the distinct states — do not conflate them:

```
DECISION RECORDED          — this policy is written and committed
WORKFLOW IMPLEMENTED       — gate mechanism (status/script/docs/templates) in place
BRANCH PROTECTION VERIFIED — the ruleset requires `Independent Agent Review`
CURRENT PR REVIEWED        — the current remote head SHA has a PROVEN gate result
```
