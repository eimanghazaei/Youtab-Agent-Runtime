# Durable Execution — Claim Ledger

**Stream:** Youtab Agent Runtime — Durable Execution / Timeout / Recovery
**Status:** Phase 0 + Phase 1 complete (source audit). Phase 2 reproductions pending.
**Author/owner:** Eiman (Youtab B.V.)

> Every statement in the two supplied reports was treated as a **hypothesis to verify**,
> not as truth. Verdicts below are grounded in the exact current fork by `path:line`.

---

## 0. Exact truth (Phase 0)

| Fact | Value |
|---|---|
| Remote | `git@github.com:eimanghazaei/Youtab-Agent-Runtime.git` |
| Remote default | `refs/remotes/origin/HEAD` → `origin/main` |
| Base commit SHA | `c7650a1b920224283ba3a59ca054d6625e07a5f3` |
| Base tree SHA | `ebc5aaaf4332152b75f3fa9811fe3a9e899cec3d` |
| Base commit | "Add files via upload" — Eiman, 2026-09-11 21:15:36 +0200 |
| Worktree | `F:/Youtab_AI_COS_Platform/_wt/rt-durable-execution` |
| Branch | `feat/runtime-durable-execution-v1` (from base; clean) |
| Runtime version | **`0.19.1`** (`pyproject.toml:5`, `name = "youtab-agent-runtime"`) |
| Desktop shell version | `0.17.0` (`apps/desktop/package.json`, productName "Youtab") — **separate** from runtime |
| Upstream provenance | Derived from Hermes Agent @ `cc4cab2f592e60a197e796506de9168f74baf3ea`, MIT (Nous Research 2025) — `THIRD_PARTY_NOTICES.md:10-13` |
| Product license | MIT, Youtab B.V. 2026 (`LICENSE`) |
| Python | 3.14.5 default; 3.12/3.11 via `py`. `requires-python = ">=3.11,<3.14"` (⚠ default 3.14 is OUT of supported range) |
| Node / npm | v24.15.0 / 11.12.1 |
| Electron | 40.10.2 (`apps/desktop/package.json`) |
| Docker | 29.8.0 present (disposable PG/Redis available) |
| uv / psql / redis-cli | not on PATH (uv absent; PG/Redis only via Docker) |
| Disk F: | 14 GB free / 87% used ⚠ |
| Lockfile hashes | `uv.lock`=`134538d3…`, `package-lock.json`=`32d3fbf0…`, `pyproject.toml`=`dbd12a94…`, `apps/desktop/package.json`=`f6b620c3…` |
| Test entry | `scripts/run_tests.sh`, `scripts/youtab/run_all_gates.sh` |

**Governance model (AGENTS.md):** One Brain is sole Cognitive Authority; managed tasks require a
signed, tenant-scoped command envelope; agent trees share ONE reasoning envelope (budgets not
multiplied per child); task-scoped agents must "hand off evidence and terminate, deactivate or
checkpoint explicitly"; managed runtime has no independent public execution ingress.

---

## 1. Claim ledger

Legend — **Result**: CONFIRMED (reproduced-in-source) · PARTIAL · DISPROVED · FIXED (already fixed in fork) · NOT_APPLICABLE (component absent / other product) · DEFERRED (other stream) · PENDING (needs Phase 2/env).
**Severity** uses the Owner's P0/P1/P2 priority. **Reproduction** = static source audit done at Phase 1; dynamic reproduction is Phase 2.

| claim_id | report claim | exact source path:line | current value / behavior | reproduction | result | affected surface | severity | disposition | owner |
|---|---|---|---|---|---|---|---|---|---|
| C-VER-1 | "current Runtime is Hermes v0.17" | `pyproject.toml:5`; `apps/desktop/package.json` | Runtime = `0.19.1`; the `0.17.0` is the desktop **shell** version only | source | **DISPROVED** | all | — | none; correct the premise everywhere | Eiman |
| C-LIC-1 | base is MIT (Nous), must keep notice | `THIRD_PARTY_NOTICES.md:10-13`; `LICENSE` | MIT preserved; upstream commit pinned `cc4cab2f`; product MIT Youtab B.V. | source | **CONFIRMED (compliant)** | legal | P2 | keep notices as-is | Eiman |
| C-LIC-2 | Chinese "CN Desktop" under PolyForm Noncommercial is present | tree-wide `git grep polyform\|wanderminds\|qingdao\|noncommercial` → 0 hits; `apps/desktop` sweep clean | No PolyForm/CN desktop code in tracked tree or desktop app | source | **DISPROVED** | legal/desktop | P2 | verify again on packaged Electron artifacts at Phase 6 | Eiman |
| C-2.1a | delegate_task tells model to use background for long work | `tools/delegate_tool.py:3726-3731` | Description says the OPPOSITE: use `cronjob`/`terminal(background=True)`; "Background delegations are NOT durable" | source | **DISPROVED** | delegation | P0 | none | Eiman |
| C-2.1b | hard-kills child at 600s, loses work | `tools/delegate_tool.py:730` (`DEFAULT_CHILD_TIMEOUT=None`), `:551-590`; `youtab_agent_cli/config_defaults.py:1593` (`child_timeout_seconds: 0`) | No 600s cap anywhere; default = no wall-clock cap; `result(timeout=None)` | Phase 2 #1 (healthy >600s) | **DISPROVED / FIXED** | delegation | P0 | prove with soak test; no code change | Eiman |
| C-2.1c | on timeout returns no summary → work lost | `tools/delegate_tool.py:2278-2295` (`summary:None`); `:2296-2299` (`shutdown(wait=False)`) | Only when operator opts into `child_timeout_seconds>0`: soft `interrupt()` (no kill), `summary:None`, abandoned thread unwinds | Phase 2 #7,#8 | **PARTIAL** | delegation | P0 | on configured timeout, return latest checkpoint/last-progress + child id, not `summary:None` | Eiman |
| C-2.1d | sync-return child is non-durable | `tools/delegate_tool.py:2151-2192` (sync RAM future) vs `tools/async_delegation.py:200-368` (persist `delegation_id`+spec to `state.db` before submit; `recover_abandoned_delegations`, `restore_undelivered_completions`) | Async path IS durable (state.db); sync path is RAM-only | Phase 2 #4,#5 | **PARTIAL** | delegation | P0 | give sync path a durable task row; parent detach ≠ child erase | Eiman |
| C-2.2a | Kanban `<<ccr:...>>` opaque ref → recovery loop | `grep -rniw ccr` → 0 hits; `tools/tool_result_storage.py:39,144,203`; worker ctx `youtab_agent_cli/kanban_db.py:9055,9117-9127` | No `ccr` marker. Worker gets **real inline text** (body capped 8 KB); big tool outputs = `<persisted-output>` + one `read_file` | source | **DISPROVED** | kanban | P0 | none | Eiman |
| C-2.2b | heartbeat looks healthy but no progress | `run_agent.py:3530` `_touch_activity`; `kanban_db.py:6989` `heartbeat_worker`, backstop `:4355-4358` (1h) | Heartbeat = pure liveness bridged from token traffic; NOT tied to checkpoint/progress; chatty stuck worker keeps lease green until 1h stale backstop (which its own traffic resets) | Phase 2 #3 | **CONFIRMED** | kanban | P0 | add progress-stall watchdog: heartbeat must carry progress marker/checkpoint digest; stall = no progress, not idle wall-clock | Eiman |
| C-2.2c | crash before first heartbeat → infinite respawn | `kanban_db.py:7371` `detect_crashed_workers` (PID-liveness, 30s grace), `:7706` `consecutive_failures+1`, `:6595` `DEFAULT_FAILURE_LIMIT=2`, `:7848-7851` counter NOT reset on spawn | PID-death detection independent of heartbeat; breaker at 2 → `blocked`/`gave_up` dead-letter | Phase 2 #11 | **DISPROVED / FIXED** | kanban | P0 | prove with crash-injection; no change | Eiman |
| C-2.2d | effect-then-die before terminal commit | `kanban_db.py:4689` `complete_task` (atomic), but `:7442-7450` "clean exit still running" protocol-violation → retried, no effect idempotency | Terminal commit atomic, but worker can do external effect then exit rc=0 before `kanban_complete`; retried without idempotency | Phase 2 #6,#12 | **PARTIAL** | kanban | P0 | per-step effect journaling + idempotency; RECONCILIATION_REQUIRED state | Eiman |
| C-2.3 | agent unaware of hard command blocks; promises impossible work | `tools/approval.py:334-436,503,572-585,3106-3108`; `agent/system_prompt.py` grep hardline/blocklist → NOT_FOUND | Typed refusal returned at execution ("BLOCKED (hardline)…"), but blocklist not advertised to model up front | Phase 2 (denial visibility) | **PARTIAL (CONFIRMED)** | approval/tools | P1 | surface capability/command denial as typed pre-execution refusal | Eiman |
| C-2.4 | resume ignores SESSION_HANDOFF.md, relies on stale session_search summary, may act destructively | `SESSION_HANDOFF` → 0 hits repo-wide; resume `cli.py:14622`, `youtab_state.py:6243,6524` (replays real stored messages); `tools/session_search_tool.py:19-27` ("Zero LLM cost", raw windows) | Premise inaccurate: no `SESSION_HANDOFF.md` exists; resume replays actual persisted messages, not a summary | source | **DISPROVED (premise)** | resume | P0 | real gap ≠ report: resume has no per-step effect-replay guard → re-running crashed `task_run` can repeat effects (see C-2.2d) | Eiman |
| C-2.5 | `memory` tool 2200-char cap | not audited (Enterprise Memory stream owns) | — | — | **DEFERRED** | memory | P2 | coordinate via typed interface; do not duplicate | Enterprise Memory |
| C-3.1 | `/new` freezes session, needs kill | `cli.py:7932-8129` `new_session`; `:7887-7930,8110-8128` (LLM memory extraction deferred to serialized bg worker) | Best-effort reset; slow work off the `/new` path; no lock/join/synchronous drain | Phase 2 #14 | **DISPROVED** | cli/session | P1 | prove; no change | Eiman |
| C-3.2a | MCP stdio zero-timeout CPU spin on child death, GIL starvation | `tools/mcp_tool.py:2283-2287` (timed `asyncio.wait`, 5s floor), `:3230-3436` (every reconnect gated by jittered backoff sleep), `:3384-3424` (300s park after 5 retries) | No busy-spin path; historical spawn-storm (#62212) already mitigated | Phase 2 #10 | **DISPROVED / FIXED** | mcp | P0 | prove no-orphan/no-spin; no change | Eiman |
| C-3.2b | MCP subprocess cleanup / zombies | `tools/mcp_tool.py:6682-6702` (SIGTERM→2s→SIGKILL, `killpg`), `tools/mcp_stdio_watchdog.py` (parent-death, POSIX-only) | Cleanup present but `killpg`/watchdog are **POSIX-only** → weaker on Windows (this host) | Phase 2 #10 (Windows) | **PARTIAL** | mcp/windows | P0 | Windows process-tree kill + parent-death reaping | Eiman |
| C-3.2c | one slow MCP server freezes unrelated work | `tools/mcp_tool.py:76-86,4131` (one shared `_mcp_loop`), per-server locks `:1885` | Per-server locks; async hang doesn't block loop, but a truly CPU-blocking server could starve the shared loop (no such path found) | Phase 2 (soak) | **PARTIAL (theoretical)** | mcp | P1 | consider isolation / bounded executor per server | Eiman |
| C-3.3 | Telegram final result never delivered | `gateway/platforms/base.py:6016-6072` (send after `record_obligation`+`mark_attempting`); `gateway/delivery_ledger.py:20-27,242-301`; `gateway/stream_consumer.py:864-1008` | Final persisted to delivery ledger BEFORE send; crash-redelivery on next boot; no interim-only-drop path | Phase 2 #15 | **FIXED** | telegram/gateway | P1 | prove; no change | Eiman |
| C-3.4 | hook multiplex: secondary-profile security hooks silently inert | `gateway/run.py:10488-10505` (register once from default profile), `agent/shell_hooks.py:246-278` (process-global `_hooks`); `gateway/hooks.py:81-96` (single global registry) | **Real live bug**: only default profile's `hooks:` are wired; secondary profiles' security hooks never fire | Phase 2 | **CONFIRMED** | gateway/security | P1 | per-profile hook registration keyed to a profile-scoped registry | Eiman |
| C-4.1 | A2A hardcoded 300s orphan-kill ignores `A2A_REPLY_TIMEOUT`; late replies dropped | `acp_adapter/` is ACP (editor protocol), NOT A2A; `grep A2A_REPLY_TIMEOUT\|A2A_TIMEOUT` → 0 hits; the `300`s are unrelated (`gateway/memory_monitor.py:48`, `signal.py:322`) | No A2A JSON-RPC agent-mesh adapter exists | source | **NOT_APPLICABLE** | a2a | P0→n/a | none; component absent | Eiman |
| C-4.2 | A2A interim returned as final; PR conditions on `metadata['notify']` | only `metadata["notify"]` = `gateway/platforms/base.py:99-103` (`_mark_notify_metadata`, Slack/Feishu bot-chat gating) | No A2A task-event stream; `notify` is unrelated platform-chat metadata | source | **NOT_APPLICABLE** | a2a | P0→n/a | none | Eiman |
| C-5.1 | Skills system has no "metabolism" (no prune/deprecate) | not audited (structural/long-term) | — | — | **DEFERRED** | skills | P2 | out of durable-exec scope unless it breaks task continuity | Eiman |
| C-6.1 | Claude Code SWE-bench 88.6%; beats Hermes | external third-party benchmark, not this fork | — | — | **NOT_APPLICABLE** | — | — | do not repeat unverified benchmark | Eiman |
| C-6.3 | Claude Code has a 600s idle watchdog that killed healthy subagents | claim about a different product; undocumented internals | — | — | **NOT_APPLICABLE / UNVERIFIED** | — | — | do not claim undocumented internals | Eiman |
| IC-1 | inherent slowness: 1 subagent = 9 min / 205k tokens / no output (serial, context bloat, destructive compaction) | serial tool calls + in-loop compaction `agent/conversation_loop.py:1206,1826,1950-1960`; stall monitors `tools/async_delegation.py:109-112` | Structural design point; serial execution + in-place compaction are real; the specific field number is external, not reproduced here | Phase 2 #1,#5 (soak) | **PARTIAL** | delegation/perf | P1 | design-reference; stall watchdog + honest progress; not a "bigger timeout" | Eiman |
| IC-2 | agent "blind" to operational consequences (deletes record, wrong invoice…) | approval gate `tools/approval.py`; effect contracts `youtab_runtime/contracts.py:113` `EffectProposal` | Governance is a layer around the agent; approval + signed effect proposals already exist (partial) | source | **PARTIAL (by design)** | governance | P1 | integrate durable-exec effects with EXISTING approval/effect ledger; do NOT build a second governance layer | Eiman |
| IC-3 | no SOC 2 / ISO / third-party pentest | — | compliance program, not a code defect | — | **DEFERRED (P2)** | compliance | P2 | separate handoff; out of this stream | Owner |

---

## 2. Durable substrate that already exists (reuse, do not duplicate)

| Primitive | Location | What it gives us |
|---|---|---|
| Task/run registry + append-only journal | `youtab_agent_cli/kanban_db.py:1136` `tasks`, `:1256` `task_runs`, `:1240` `task_events` (AUTOINCREMENT seq), `:2820` create, `:4079` claim, `:4276` heartbeat, `:3807` `_append_event` | Persistent task id (before work), attempt history, append-only events, claim leases w/ fencing (`claim_lock`=host:uuid + run epoch), idempotency_key, tenant col; **reserved** `workflow_template_id`/`current_step_key`/`step_key` for "v2 workflow" |
| Durable delivery ledger | `gateway/delivery_ledger.py:97-111` (`delivery_obligations`), liveness `:146` `_owner_alive`, reconcile `:19-35` | Effect ledger pattern (owner-pid + start-time liveness, at-least-once with `RECOVERED_MARKER`), idempotency via `compute_obligation_id` sha256[:24] |
| Cron execution audit ledger | `cron/executions.py:1-6,40-53` | States claimed/running/completed/failed/**unknown**; interrupted→unknown only after owner proven gone; immutable terminals |
| Signed effect/command contracts | `youtab_runtime/contracts.py:37` `BrainCommandEnvelope`, `:113` `EffectProposal`, `:131` `CompletionReport`, `:154` `completion_status` (incl. `completed_with_uncertainty`, `checkpointed_for_resume`, `terminated_by_*`); `policy.py:42` `AuthorityBoundary` | Typed state/effect vocabulary + Ed25519 signing + nonce replay-protection — but **stateless** (in-RAM nonce set, no persistence) |
| Approval / hard blocklist | `tools/approval.py` (HARDLINE `:334-436`, enforce `:3106-3113`, `fail_closed_when_no_human`) | Single outer-layer enforcement point for sensitive effects |
| Workspace/tenant isolation | `youtab_runtime/security.py:24-30` `resolve_within`; state schema composite `PRIMARY KEY (scope, session_key)` | Filesystem containment + per-scope row isolation |
| FS rollback checkpoints | `tools/checkpoint_manager.py` (git-shadow snapshot of working dir) | For `/rollback` of file mutations — **NOT** execution state |
| Async delegation durability | `tools/async_delegation.py:142` `async_delegations` table (states running/finalizing/unknown/delivered), `:293` recover, `:344` restore | Background child identity + result persisted; crash recovery |

**Missing for durable execution** (the real work): unified task-state set (`WAITING_APPROVAL`, `RECONCILIATION_REQUIRED`, `UNKNOWN`, `CANCELLING` absent; task uses `done` not `SUCCEEDED`); per-step effect journaling + idempotency so resume replay is safe; an execution checkpoint capturing plan/step/pending-approval/child-ref/effect-ref (git checkpointer captures none); version/digest check on resume; tenant/principal binding on checkpoints; continue-as-new handoff; progress-stall watchdog tied to progress (not liveness); Windows process-tree kill; per-profile hook registration; durable `/v1/runs` (currently in-memory) and reconnect-from-last-sequence.

---

## 3. Verdict of the two reports (headline)

- Both reports assume **upstream Hermes v0.17**. The fork is **0.19.1** and many named defects are **already fixed** here (600s kill, MCP spin, Telegram final, /new freeze, infinite kanban respawn, reload false-success).
- Several named components **do not exist** in this fork (A2A adapter; `<<ccr:...>>`; `SESSION_HANDOFF.md`).
- The **genuine, reproduce-worthy** defects on THIS fork: kanban progress-stall (liveness≠progress) [C-2.2b], effect-then-die without idempotency [C-2.2d], configured-timeout loses partial [C-2.1c], sync-delegation non-durable [C-2.1d], hook multiplex inert security hooks [C-3.4], Windows MCP orphan reaping [C-3.2b], `/v1/runs` in-memory + no reconnect-from-sequence [Web/Electron], app-close kills backend on Win/Linux [Web/Electron], denial not advertised pre-exec [C-2.3].
- Structural items [IC-1, IC-2, IC-3] are governance/compliance layers — integrate with existing controls; do not rebuild.

---

## Revision 2 — Owner corrections (additive; supersedes Section 1 verdict cells where noted)

**Verdict labels:** `PHASE 0/1 AUDIT COMPLETE_NOT_REVIEWED` · `PHASE 2 REPRODUCTION IN PROGRESS` · `DURABLE EXECUTION PRODUCT NO-GO`.
No defect is called "fixed" until a failing deterministic reproduction turns green on the exact candidate SHA.

### R2.1 Mechanical claim-ID enumeration and reconciliation
Prior checkpoint said "27" and category totals summed to 25 — both were wrong (a regex undercount: lowercase suffixes `a/b/c/d` and `VER/LIC` rows were missed). Full manual enumeration of **28 report-derived claim IDs** + **2 audit-derived IDs** (`C-WEB-1`, `C-WEB-2`) = **30 total**:

`C-VER-1, C-LIC-1, C-LIC-2, C-2.1a, C-2.1b, C-2.1c, C-2.1d, C-2.2a, C-2.2b, C-2.2c, C-2.2d, C-2.3, C-2.4, C-2.5, C-3.1, C-3.2a, C-3.2b, C-3.2c, C-3.3, C-3.4, C-4.1, C-4.2, C-5.1, C-6.1, C-6.3, IC-1, IC-2, IC-3` (28) + `C-WEB-1, C-WEB-2` (2).

### R2.2 Required classification (8 categories) — exact counts

| Category | Count | Claim IDs |
|---|---|---|
| REPRODUCED_CONFIRMED | 0 | (none — dynamic reproductions pending Phase 2) |
| PRESENT_NOT_YET_REPRODUCED | 11 | C-2.1c, C-2.1d, C-2.2b, C-2.2d, C-2.3, C-3.2b, C-3.2c, C-3.4, C-WEB-1, C-WEB-2, IC-1 |
| ALREADY_FIXED_ON_BASE | 6 | C-2.1b, C-2.2c, C-3.1, C-3.2a, C-3.3, C-LIC-1 |
| REPORT_CLAIM_DISPROVED | 4 | C-VER-1, C-2.1a, C-2.2a, C-2.4 |
| UPSTREAM_ONLY | 0 | (none cleanly isolated on this base) |
| NOT_APPLICABLE_TO_RUNTIME_REPO | 4 | C-4.1, C-4.2, C-6.1, C-6.3 |
| INCONCLUSIVE_ENV_UNAVAILABLE | 1 | C-LIC-2 (packaged-artifact portion) |
| DEFERRED_TO_NAMED_OWNER | 4 | C-2.5 (Enterprise Memory), C-5.1 (Owner/P2 skills), IC-2 (Lane 1/2 governance), IC-3 (Owner compliance) |
| **TOTAL** | **30** | |

### R2.3 Scope corrections
- **C-4.1 / C-4.2 (A2A):** reclassified `NOT_APPLICABLE_TO_RUNTIME_REPO — Gateway/AI-OS requires separate audit`. Proven only that the report's A2A implementation is **absent from this Runtime repository** (`acp_adapter/` = ACP editor protocol). NOT a claim about the whole Youtab platform.
- **C-LIC-2 (CN Desktop / PolyForm):** reclassified `INCONCLUSIVE_ENV_UNAVAILABLE`. Established on the selected base: **0 tracked source/license references** (`polyform|wanderminds|qingdao|noncommercial`); desktop `extraResources` bundles only `install-stamp.json` + `icon.ico`; no SBOM in tree; **no built installer/app.asar exists in the worktree to inspect.** Still to inspect before a final verdict: Electron dependency tree, `apps/desktop/package-lock.json` (not present on base), built `app.asar` inventory, `extraResources`/`extraFiles` of a real build, SBOM, installed artifact. Scope = component provenance + license inventory only, not a broad legal review.
- **Version — three DISTINCT identities (do not collapse):**
  1. remote-default Runtime **source base** = `0.19.1` (`pyproject.toml:5`, origin/main `c7650a1b`);
  2. Desktop **package** version = `0.17.0` (`apps/desktop/package.json`);
  3. a recently produced **local packaged sidecar** reportedly exposed `0.20.0` — belongs to a different **unintegrated delivery line**, NOT evidence about origin/main.

### R2.4 Async delegation wording correction
`tools/async_delegation.py` provides **DURABLE IDENTITY / RECOVERABLE STATUS** only, NOT full durable execution. Proven: durable delegation id/spec/state row (`:200-226`), abandonment detection (`:293`), undelivered-completion restoration (`:344`). NOT yet proven (requires executable restart/resume tests): continuation from last execution step; child restart from a checkpoint; effect-safe resume; model/context continuation; exactly-once final-result delivery across all crash windows. C-2.1d stays `PRESENT_NOT_YET_REPRODUCED`.

### R2.5 Ownership boundaries (no duplicate ledgers)
Lane frozen SHAs (all reachable read-only in this repo): Lane 1 `4edbbe78e46337018cd4aa92bee3437a13f4b413`, Lane 2 `1ed19f0914cce6d074aae6cf378d81f05bc6d283`, Lane 3 `474eac31f8c83c9939d1d2d6f5450260dcf43dec`. Lane 1/2 own the canonical **effect ledger, approvals, worker lease, UNKNOWN/reconciliation, receipts, enterprise governed execution**. **Durable Execution (this stream) owns:** durable task/run identity, task event sequence, execution checkpoints, restart/resume, parent/child lifecycle, progress watchdog, cancellation, reconnect + late-result delivery. Where canonical Lane APIs are absent on the selected base, emit a **typed interface request** (Master Integrator combines later); do NOT create a second effect ledger / approval system / reconciliation state machine on origin/main.

### R2.6 Authoritative test environment (established)
- CI-supported pin (from `.github/workflows/youtab-ci.yml:95`): `python -m pip install 'uv==0.8.17'`; Python `3.12` via `actions/setup-python@v7.0.0` (`:33,:189`).
- This stream: bootstrapped `uv 0.8.17` (via pip in an isolated scratch venv), then `uv sync --frozen --extra dev` with `UV_PYTHON=python3.12`, creating `.venv` **inside the worktree** (Python 3.12.10, `youtab-agent-runtime==0.19.1` editable, 250 locked pkgs; heavy deps limited to numpy/scipy/scikit-learn/onnxruntime — no torch/CUDA). Root worktree venv NOT mutated; no PYTHONPATH borrowing for authoritative runs; `UV_CACHE_DIR` on C: to spare F:.
- Disk: before 13 GB free (85 GB used) → after 13 GB free (86 GB used); delta ≈ 1 GB.
- Validation: `tests/tools/test_async_delegation.py` → **19 passed** (incl. the real-subprocess test that failed under the diagnostic borrow-harness), confirming subprocesses inherit the environment. This env is qualification-grade.

### R2.7 Progress-vs-heartbeat (design note, pre-implementation)
Do not add a heartbeat field yet. First determine whether existing `task_events` (monotonic AUTOINCREMENT id) + a checkpoint digest can serve as the monotonic progress marker. Final design must SEPARATE: worker liveness heartbeat · lease renewal · progress sequence · checkpoint digest · current step · no-progress duration. A live heartbeat without progress must produce a visible STALLED/BLOCKED recovery state — never an immediate destructive kill.
