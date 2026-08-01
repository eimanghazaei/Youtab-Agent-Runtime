# Youtab Agent Runtime — execution and qualification report

Date: 2026-08-01  
Decision: **BLOCKED — do not merge or deploy**

## Executive result

The product repository is now an isolated Youtab source snapshot with independent
Git history, Youtab package/CLI/Desktop/runtime identity, a fail-closed One Brain
execution boundary, controlled update-intake documentation, and dedicated
security/branding gates. The previous downstream-history pull request was closed
without merge.

This is not release-ready. The managed Youtab boundary and the dedicated security
controls pass, but the broader Python regression suite and current dependency
advisory scans still fail. The new branch must remain a Draft/blocked review until
those findings are remediated and a new owner approval is issued.

## Safety and source-isolation evidence

| Control | Evidence |
|---|---|
| Former PR | PR #6 closed; `merged=false` |
| Product base branch | `product/youtab-agent-runtime-baseline` |
| Independent root commit | `24dd17665b5d` |
| Implementation commit | `da1d6e28e12e9cdf9330b7b6405b27b3d4ff7318` |
| Pinned source commit used for materialization | `cc4cab2f592e60a197e796506de9168f74baf3ea` |
| Materialized-source SHA-256 | `a9132941261d3aff4d4141354968939b3afcf011ea5d0e3233037bead86b9907` |
| Source transfer method | Local `git archive` materialization into a new directory; no upstream `.git` history copied |
| Product snapshot size | 8,714 tracked files in the implementation commit |
| Main branch | Not changed |
| Force-push | Not performed |
| Merge | Not performed |
| VPS / Production | Not accessed or deployed |

Legal provenance is retained only in `THIRD_PARTY_NOTICES.md` and the license
surface. It is not used as product identity, package identity, documentation
branding, UI branding, runtime paths, or update wiring.

## Implemented product boundary

The managed runtime accepts a strict signed task contract and verifies Ed25519
integrity, task/tenant/nonce binding, expiry, shared reasoning limits and replay
state. It rejects authority escalation and sovereign-memory scopes. External
effects are emitted only as proposals with `runtime_authorized=false`; the Brain
Effect Gate remains the authority. The managed worker uses stdin/stdout and does
not open an independent public listener. Completion reports carry evidence,
uncertainty, unresolved items and deactivation state.

Additional deterministic controls cover private/local SSRF targets, path
traversal, secret-output redaction, prompt-injection non-escalation and shared
resource envelopes.

## Final gate matrix

| Gate | Result | Final evidence |
|---|---:|---|
| Managed Unit / Integration / E2E | PASS | 23/23 tests across contracts, policy, security and worker E2E |
| Deterministic Smoke | PASS | 23/23; zero network calls and zero external effects |
| OWASP managed-runtime controls | PASS | Web/API/LLM disposition plus deterministic boundary tests |
| Branding text/path/OCR | PASS | 8,977 text files + 689 binary files; OCR enabled; zero findings |
| Secret scan | PASS | 8,976 files; zero actionable findings; 34 exact hash-bound test/document fixtures |
| Dependency lock integrity | PASS | 2,053 Python artifacts, 3,573 npm integrity entries, 6 metadata-only entries, 3 SHA-pinned Actions |
| High-confidence SAST | PASS WITH REVIEW DEBT | Zero blocking findings; 12 inline reviewed dispositions; 1,317 non-blocking heuristic findings remain inventoried |
| Python compilation | PASS | Core runtime, CLI, agent, providers, tools, plugins and gateways compile |
| Web tests | PASS | 20 files / 135 tests |
| TUI tests | PASS | 133 files / 1,437 passed / 1 skipped |
| Desktop UI tests | PASS | 354 files / 3,133 tests |
| Electron platform tests | PASS | 74 files passed / 1 skipped; 867 tests passed / 2 skipped |
| Desktop production package | PASS | Renderer + Electron bundles + native `node-pty` + Linux unpacked package |
| Full Python regression qualification | **FAIL** | Final focused rerun: 1,815 passed / 133 failed across 73 previously failing files; 66 files still fail |
| npm advisory audit | **FAIL** | 92 advisories: 1 critical, 83 high, 4 moderate, 4 low |
| Python advisory audit | **FAIL** | 40 advisory records across 11 locked packages |
| Wake-word asset qualification | **NOT QUALIFIED** | No renamed/validated wake-word model was fabricated; feature remains blocked |

### SAST inventory retained for review

The non-blocking inventory is not represented as clean: `S104=13`, `S105=71`,
`S107=2`, `S108=33`, `S310=164`, `S311=42`, `S324=22`, `S404=175`,
`S603=476`, `S604=1`, `S605=1`, `S606=5`, `S607=205`, and `S608=107`.
High-confidence blocking codes `S102`, `S202`, `S307`, `S314`, `S403`, `S405`,
`S506`, and `S602` have no undisposed finding.

### Dependency blockers

The Python advisory scan identified findings in: `httplib2`, `mcp`, `msgpack`,
`pillow`, `pyasn1`, `pydantic-settings`, `pygments`, `pynacl`, `pytest`,
`setuptools`, and `tornado`. The npm report includes direct and transitive build,
lint, Storybook, Vite, Electron and runtime dependency chains. Lock integrity is
therefore green while vulnerability admission is red; these are deliberately
separate claims.

### Python failure classes

Remaining failures include process/socket restrictions in the qualification
runner, LSP lifecycle/install behavior, verification evidence and stop logic,
gateway/process state, update locks, service management, code execution,
profiles, CLI state and the deliberately unqualified wake-word path. Environment
limitations explain some failures but do not convert them into passes. A clean,
supported runner and code fixes are both required before release admission.

## OWASP scope

The runtime disposition is aligned to the current
[OWASP Top 10:2025](https://owasp.org/Top10/2025/en/),
[OWASP API Security Top 10:2023](https://owasp.org/API-Security/editions/2023/en/0x11-t10/),
and [OWASP Top 10 for LLM Applications:2025](https://genai.owasp.org/llm-top-10/).
Controls owned by the wider Youtab system—identity provider, tenant control
plane, public API gateway, sovereign memory, release signing and Production
admission—are documented as system responsibilities and are not falsely marked
as runtime-tested.

## Roadmap status

| Roadmap level | Status |
|---|---|
| Phase 0 — isolation, identity and architecture ground truth | Substantially implemented: independent history, source digest, Youtab identity and One Brain boundary exist; final qualification gate remains open |
| Gate G0 / Phase 1 qualification | **FAIL** because full Python regression and dependency vulnerability admission are red |
| Phase 3 — controlled fork/update discipline | Partially implemented: quarantine/diff/test/security/PR policy and local gates exist; no automatic upstream promotion is allowed |
| Backend/frontend integration | Not implemented as a production adapter; no claim of end-to-end Youtab system integration |
| Phases 4–12 | Not implemented or deployed by this work |

## Required next approval boundary

No merge, force-push to `main`, VPS mutation, Production deployment, signing or
release admission is authorized by this report. Before any of those actions, the
133 remaining Python regressions and dependency advisories must be triaged and
remediated or explicitly risk-accepted, the full matrix must be rerun on a
supported clean runner, and the owner must issue a new exact approval based on
the resulting evidence.
