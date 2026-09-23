# R4 owner acceptance + exact-SHA integration proof (Timeout/durable owner)

Bounded owner integration/review per master handoff
(`_evidence/master/TIMEOUT_R4_OWNER_HANDOFF_2026-09-23.txt`) and release decision
(`_evidence/master/R4_ADR_0002_RELEASE_DECISION_2026-09-23.md`). Worktree
`F:/Youtab_AI_COS_Platform/_wt/rt-durable-execution`. **No push, PR, protected
merge, deploy, customer install, amend, rebase, squash, force, or destructive
worktree change.** Product **NO-GO**.

## ADR-0002 verdict: ACCEPT (durable-execution owner)
I accept ADR-0002 (`docs/architecture/ADR-0002-durable-run-authority-singleton.md`)
as the R4 design direction for the durable owner line, matching master release
acceptance. Source-backed reasons (independently re-verified in this worktree):
- **Store-enforced singleton, not deployment-enforced.** `youtab_runtime/durable_run_authority.py`:
  owner stamp `inst:<uuid>:<epoch>` (uuid instance id — immune to PID reuse /
  container PID-namespace collision, the exact defect ADR-0002 supersedes);
  SQLite exclusive OS file lock (msvcrt/fcntl); fail-stop `mark_lost` listeners.
- **Every durable write is fenced.** `durable_run_store_pg._fence` reads the epoch
  row `FOR SHARE` + checks `pg_locks` shows the advisory lock still held →
  `mark_lost` on mismatch; `durable_run_store._fence` checks epoch under
  `BEGIN IMMEDIATE`. PG acquisition = `pg_try_advisory_lock` on a dedicated
  session (keepalives, `tcp_user_timeout=10s`, `statement_timeout=5s`).
- **Fail-closed end to end.** `api_server` gates readiness, admission, dispatch and
  execution on `authority.held`; `_on_run_authority_lost` interrupts agents and
  `os._exit(75)`. Recovery moves prior nonterminal runs to UNKNOWN (never
  resumed/retried).

## Integration status (additive, no history rewrite)
- Owner integrated SHA: **`042bfbba7a19f786458fecd90564efc9067f3c75`**, tree
  `93b0a037499012275f9fe5db3be7d221d85b652e`, branch
  `feat/runtime-durable-execution-v1`.
- R4 product fix `415a1734a49f04ceced1801f5ddaffd7ff36ee55` and proof
  `a1f9e33428…` are **ANCESTORS** of the owner SHA (brought in by fast-forward;
  history NOT rewritten). `git diff 415a1734a HEAD -- durable_run_authority.py` is
  **empty** — the authority module is byte-identical since R4; the later commits
  only ADD store methods (`open_approval`, `decide_open_approval`) and the R5 reap
  hook. This owner branch IS the durable integration line; no throwaway branch was
  needed.

## Exact-SHA real-PostgreSQL proof (042bfbba7)
Deterministic (real `postgres:16-alpine` on 127.0.0.1:55432):
- `tests/durable_execution/test_r4_run_authority.py` — **7 passed** (3 SQLite,
  4 real-PG): competing-instance refused, **same-PID prior-instance recovery**,
  **lock-loss fail-stop + next-holder recovery**, **HTTP/SSE terminal-follows-commit**.
- Full `tests/durable_execution/` — **123 passed / 2 skipped** on real PG.

Live image on the exact SHA — `youtab-agent-runtime:r4int-042bfbba7`,
Id `sha256:0f42a77f64f9bfe0e4f3b8be1bc12c93346870d72980d20439ff95a9fb457eb6`,
embedded `/opt/youtab/.youtab_agent_build_sha == 042bfbba7…` (provenance match),
booted via s6 `/init` + `gateway run`, real PG, streaming mock, real HTTP:
- **Authority loss / restart:** `pg_terminate_backend` on the advisory-lock
  backend → `CRITICAL … authority LOST (epoch=1 … AdminShutdown)` → fail-stop →
  `effect-descendant reap on fail-stop: contained=True … sweeps=1` (R5 reap hook
  is wired at this SHA) → s6 restart → **epoch=2** → `startup reconcile: 1
  abandoned run moved to UNKNOWN`. HTTP `GET /v1/runs/{id}` and `/result` →
  `status:unknown, output:null, recovered_from_store:true`. SSE stream carried
  **no `run.completed`**. **No late ack** — run stayed `UNKNOWN|NULL` at +10/+20s
  past the 20s mock window. **No blind retry** — 0 `SUCCEEDED` rows lacking a
  committed `status.completed` event.
- **Happy path (commit-before-emit):** `/result` → `completed`,
  `output:CUSTOMER_TASK_OK_42`; PG row `SUCCEEDED|CUSTOMER_TASK_OK_42`; SSE order
  = durable `status.completed` **before** `run.completed`.

Invariants preserved at the exact SHA: **commit-before-ack/emit, no uncommitted
output, no blind retry** (conditions 2 & 3 wording).

## Master conditions — status
1. **Topology (one effectful instance/DB, lock/epoch, direct/session PG, Recreate,
   explicit UNKNOWN):** reflected in ADR-0002 "Supported topology",
   `docker-compose.postgres.yml`, and `ADMIN_INSTALL_PROCEDURE.md`. MET (docs).
2. **One Runtime SHA with R1 worker/server + Desktop, rerun proofs:** durable side
   integrated + re-proven on exact SHA 042bfbba7 (above). **The R1 worker/server +
   Desktop unification into ONE candidate SHA is NOT done here** — that is the
   integration owner's task after rt-lanes is consolidated to one owner; I did not
   edit their worktrees and do **not** imply one integrated candidate. **OPEN.**
3. **10–15s effect window / effect-level safety is separate:** acknowledged. The
   R5 reap hook narrows the in-process descendant window (wired, fired in the live
   probe) but R5 has open review findings (`R5_AC7_REVIEW_2026-09-23.md`, untracked
   peer note), and the Lane-1 effect ledger that closes the effect-level window is
   a **SEPARATE OPEN GATE**. R4 is **not** promoted to effect safety or candidate GO.
4. **Cancellation/authority-loss races vs final worker approval/resume protocol +
   current RunStore:** authority-loss races proven against the current RunStore
   (tests + live). The approval primitives (`open_approval`/`decide_open_approval`,
   single-authority, committed) are in place, but the **final worker approval/
   resume (D1 create-to-worker dispatch) protocol is HELD for owner ratification**,
   so full race verification against the FINAL protocol is **OPEN.**

## Remaining blockers (explicit)
- **B1 — One-candidate unification:** R1 worker/server + Desktop into one Runtime
  SHA (integration owner, after rt-lanes consolidation to a single owner). Not
  implied here.
- **B2 — Effect-level fencing:** Lane-1 effect ledger + R5 reap review findings
  (enumeration/identity/deadline/child-race were addressed in `2cf4cdf47`, but the
  gate stays open pending adversarial + installed-image effect proof). Separate gate.
- **B3 — Restart-safe approval / D1 dispatch seam:** HELD for Eiman; `open_approval`
  wiring gated on it.
- **B4 — Windows full-suite/gates:** not wholly green (POSIX-only failures,
  pre-existing and identical on base) — not R4 regressions.
- **B5 — Shared-worktree hazard:** multiple sessions commit to
  `feat/runtime-durable-execution-v1` in this one worktree; two peer review docs
  are untracked here. Recommend consolidating rt-lanes to one owner.

## Verdict
ADR-0002 **ACCEPTED** as the R4 design direction for the durable owner line; R4 is
integrated additively (no history rewrite) at owner SHA **042bfbba7** and
re-proven on that exact SHA against real PostgreSQL (deterministic 7/7 authority +
123/2 durable) and on the exact-SHA image (live authority-loss/restart + HTTP/SSE,
commit-before-emit, no uncommitted output, no blind retry). **Product NO-GO**;
conditions 2/3/4 have explicit open gates above; effect-safety, D1 approval/resume,
and one-candidate unification are separate and unclosed. Coordinated with R1
Runtime Integration; no worktree edits; no single integrated candidate implied.
