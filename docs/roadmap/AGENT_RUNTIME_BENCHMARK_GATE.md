# Agent Runtime — Benchmark & Security Gate (pre-exposure)

Agent Runtime must **not** be exposed to ordinary users (public or production)
before this gate passes. A later Closed Beta requires **separate Owner approval**
after benchmark evidence exists. This document is the candidate checklist; each
row is a *candidate* until it has real, reproducible evidence attached.

Status vocabulary: `NOT_STARTED` · `HARNESS_READY` · `RUN_LOCALLY` ·
`RUN_IN_CI` · `EVIDENCE_ATTACHED` · `WAITING_FOR_VPS_EXECUTION`.

## Four separate benchmark suites

The suites are scored independently; a green product benchmark does not excuse a
red security benchmark.

### A. ENGINE BENCHMARK (model + tool loop, no product surface)
| Candidate | Metric | Status |
|---|---|---|
| Task completion | % tasks reaching correct terminal state | NOT_STARTED |
| Multi-step planning | plan depth / correctness on multi-step tasks | NOT_STARTED |
| Tool correctness | right tool, right args, right order | NOT_STARTED |
| Skill correctness | skill selection + execution vs golden | NOT_STARTED |
| Persistence & reopen | run result identical after reopen | NOT_STARTED |
| Cancellation | cancel stops dispatch; state consistent | NOT_STARTED |
| Retry & idempotency | retried request does not double-dispatch | NOT_STARTED |
| ECO quality | ECO (Ollama) output quality vs reference | NOT_STARTED |
| Amour fallback correctness | fallback only on real ECO failure; one hop | NOT_STARTED |
| Latency / throughput / cost | p50/p95, req/s, tokens+$ per task | NOT_STARTED |
| Hallucination / truthfulness | grounded-answer rate; refusal calibration | NOT_STARTED |

### B. FULL WEB OS END-TO-END BENCHMARK (product path)
| Candidate | Metric | Status |
|---|---|---|
| Create → run → terminal → reopen through the UI | pass rate | NOT_STARTED |
| Agent Builder CRUD round-trip (create/edit/delete/list refresh) | pass rate | NOT_STARTED |
| Browser network sanitation (no provider/model/URL/IP on the wire) | 0 leaks | NOT_STARTED |
| Sessions / Projects / Skills / Memory / Multi-Agent UI wired | no dead controls | NOT_STARTED |
| Failure recovery surfaced correctly in UI | pass rate | NOT_STARTED |

### C. SECURITY / ISOLATION BENCHMARK (hard gate — must be 100%)
| Candidate | Metric | Status |
|---|---|---|
| Tenant isolation | no cross-tenant read/write/run | NOT_STARTED |
| Prompt injection resistance | injected instructions not executed | NOT_STARTED |
| Secret leakage | no secret in env/argv/inspect/logs/UI/wire | HARNESS_READY (gateway *_FILE sentinel tests; Amour resolver proof) |
| Cross-tenant attacks | foreign-id run/session/file denied 404 | HARNESS_READY (Agent CRUD cross-tenant 404 tests) |
| Wrong-run evidence | one run cannot consume another's dispatch | NOT_STARTED (needs server correlation) |
| Correlation integrity | per-run correlation stamped + verified | NOT_STARTED (server correlation not yet implemented) |
| Observer fail-closed | observer failure aborts, never green | HARNESS_READY (eco-verify closure + selftests) |

### D. LOAD / RECOVERY BENCHMARK
| Candidate | Metric | Status |
|---|---|---|
| Sustained load | throughput + error rate under N concurrent runs | NOT_STARTED |
| Recovery | ECO down→up, engine restart, DB blip → correct recovery | NOT_STARTED |
| Backpressure | queue saturation degrades safely, no data loss | NOT_STARTED |

## Exposure rule
- No public or production exposure until suites A, B, C, D have attached evidence
  AND suite C (security/isolation) is 100%.
- Closed Beta is a **separate** Owner decision made against that evidence — it is
  not implied by a green benchmark.

## Current honest status
Security-benchmark harness pieces exist (gateway `*_FILE` sentinel tests, the
Amour resolver/real-client proof, the eco-verify fail-closed closure + self-tests,
Agent CRUD cross-tenant 404 tests). No end-to-end benchmark run has been scored,
and the two correlation-dependent security rows are blocked on server-side
correlation. Agent Runtime is therefore **NOT benchmark-ready and NOT user-ready**.
