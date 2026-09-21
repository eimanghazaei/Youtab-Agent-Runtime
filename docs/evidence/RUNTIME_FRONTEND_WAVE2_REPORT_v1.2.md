# Runtime Frontend — Wave 2.2 Report v1.2 (supersedes v1.1)

SESSION 2/4 · Frontend Master Integrator · OWNER Requirements 1–12.
Final exact-SHA evidence correction. Delivery branch: `delivery/runtime-frontend-owner-1-12`.

## 1. SHAs (full 40-character)

- **Base SHA:** `20d69ce4245a51e105c381f811fb8cdb8490401c`
- **Pre-v1.2 head (this report's parent):** `db2ba509a15e061bf543870e577fbcfce75d5bb9`
- **Final SHA (including this v1.2 report commit):** the delivery branch HEAD produced by committing this file. A document cannot contain its own commit hash; the 40-character value is recorded in the session hand-off / `git rev-parse HEAD` on `delivery/runtime-frontend-owner-1-12`. Every prior commit SHA is listed in full below, and this is their additive descendant.

## 2. Commit series (base → HEAD), author+committer = Eiman Ghazaei, 0 attribution

| # | Full SHA | Commit | Changed files |
|---|---|---|---|
| 1 | `2d2b554c558f62555be4dad1c8073545ca146dc0` | docs: checklist + canonical contracts | docs/roadmap/…CHECKLIST_v1.0.md, docs/evidence/…CONTRACTS_v1.0.md |
| 2 | `293c468c588dd26a41702d35e28af8dbdb94c7f8` | feat: file/folder UI slice | lib/folder-grants.ts(+test), lib/file-ingress.ts(+test), app/files/local-files-panel.tsx(+test), store/composer.ts, global.d.ts, app/chat/composer/attachments.tsx(+test) |
| 3 | `48dd1ee13f14063ba5ea6bceab1fea15fddc99ad` | i18n: local-files/scan-state/model-receipt keys | i18n/types.ts, en.ts, zh.ts |
| 4 | `540aa406367d8ba448ae25eadaaef933b4ac5b8b` | feat: ME-05 UI mounted on Model surface | app/model-receipt/{inference-receipt.tsx,inference-receipt.test.tsx,use-inference-receipt.ts}, app/settings/model-settings.tsx, model-settings.test.tsx |
| 5 | `2c1123977b3864b12ea082f4feed5aa8953dcfc0` | test: SE-02 branding lock | components/__tests__/branding-integrity.test.ts |
| 6 | `1c2dd12872f983eccc1ae4c7db9c3e6fccdf1543` | test: API/MCP integration (REFERENCE_EXTERNAL_SYSTEM) | app/settings/__tests__/api-connection.integration.test.ts, app/skills/__tests__/mcp-consumers.integration.test.ts |
| 7 | `6b5f54bc30783d8760fe2db2f1530584812cae91` | docs: Wave 2 report v1.0 | docs/evidence/…WAVE2_REPORT_v1.0.md |
| 8 | `c05f481469a7e064552d3643b691a844a1b6b0cc` | docs: Wave 2.1 report v1.1 + totals correction | docs/evidence/…WAVE2_REPORT_v1.1.md, docs/roadmap/…CHECKLIST_v1.0.md |
| 9 | `db2ba509a15e061bf543870e577fbcfce75d5bb9` | fix: rename visible ME-05 UI → "Inference summary" | app/model-receipt/inference-receipt.tsx(+test), app/settings/model-settings.test.tsx, i18n/en.ts, i18n/zh.ts, docs/roadmap/…CHECKLIST_v1.0.md |
| 10 | (this commit) | docs: Wave 2.2 report v1.2 | docs/evidence/…WAVE2_REPORT_v1.2.md |

**Attribution scan:** `git log 20d69ce4245a51e105c381f811fb8cdb8490401c..HEAD --format=%B | grep -iE "co-authored|claude|anthropic|openai|codex|generated with"` → **empty (0)**. All authors and committers = `Eiman Ghazaei <eiman.ghazaei@gmail.com>`. The historical `feat/runtime-frontend-owner-1-12` (which carries the prohibited trailers) is unchanged and NOT part of this delivery.

## 3. Inference receipt/summary contract decision (OWNER item 1)

**Backend contract inspected:** there is **NO canonical durable inference receipt**. The frontend types (`app/types.ts`, `types/youtab.ts`) and the gateway event stream (`use-message-stream/gateway-event.ts`) carry **no** `receipt_id`, `effect_id`, or `inference_receipt`; `prompt.submit`'s result is not consumed and `message.complete` carries usage but **no server request-id / receipt-id / latency**. The only `request_id` in the stream belongs to interactive `clarify`/`sudo.respond` prompts, not the inference round-trip.

**Decision:** the visible UI is renamed from **"Inference receipt" → "Inference summary"** (en: `Inference summary`; zh: `推理摘要`). It presents live session/model/usage state as a truthful summary and is **not described as a durable receipt**. Absent fields (server request-id, custom endpoint, latency) render **"not reported by backend"** — never fabricated (no request ID / endpoint / latency / effect ID / receipt ID invented). The **durable-receipt requirement stays BLOCKED** until the Backend supplies a canonical receipt contract (ME-05).

## 4. Direct SHA qualification (OWNER item 2 — NOT tree-equivalence)

All commands run in `apps/desktop` of the clean worktree at head `db2ba509a` (node_modules installed there, exit 0):

| Command | Exit | Result |
|---|---|---|
| `vitest run --project ui <9 targeted files: file/folder + mounted-UI + API/MCP + branding + a11y-in-panel>` | 0 | **90 passed / 0 failed** (9 files) |
| `npm run typecheck` (tsc ×3) | 0 | clean |
| `npx eslint src/` | 0 | 0 errors (75 pre-existing warnings) |
| `npx prettier --check 'src/**/*.{ts,tsx}'` | 0 | clean |
| `git diff --check` | 0 | clean |
| attribution scan (base..HEAD) | — | 0 forbidden |
| official Gitleaks (below) | 0 | 0 findings |
| **`npm run test:ui` (one complete serial run, raw output)** | 1 | **6 failed / 3191 passed (3197 collected)** — failing = ONLY the 3 PX files, **zero non-PX failures** |

Accessibility coverage lives in `local-files-panel.test.tsx` (aria labels + keyboard) and the mounted-UI test; no separate a11y file exists.

## 5. Official Gitleaks (SE-03)

- **Scanner:** Gitleaks **v8.21.2** — `ghcr.io/gitleaks/gitleaks:v8.21.2`, digest `sha256:0e99e8821643ea5b235718642b93bb32486af9c8162c8b8731f7cbdc951a7f46` (Docker, pinned, ephemeral).
- **Command:** `gitleaks detect --no-git --source /scan -v --report-format json` over `git log -p 20d69ce4…..HEAD` (every commit diff in the delivery range) + `docs/evidence` + `docs/roadmap`.
- **Exit:** 0 · **Findings:** **0 ("no leaks found").**

## 6. Artifact / log paths and SHA-256

| Artifact | SHA-256 |
|---|---|
| `scratchpad/w22_battery.txt` (targeted+gates log) | `43cf750f896cab0dfe7d6a2776a679d9200b8b4f7e2d33bbd35f44e54e68f14b` |
| `scratchpad/w22_serial.txt` (serial test:ui raw log) | `5f80a7c24c498f47c521841c431d68cbba573280e2803a1391347f0cadeb3cf3` |
| `scratchpad/gitleaks_w22/report.json` (Gitleaks report) | `37517e5f3dc66819f61f5a7bb8ace1921282415f10551d2defa5c3eb0985b570` |
| `scratchpad/gitleaks_w22/delivery_range.patch` (base..HEAD) | `be087c72a24182af532e7b732840d6918574df6e411d103934ec39e8002145ea` |
| `docs/evidence/RUNTIME_FRONTEND_WAVE2_REPORT_v1.1.md` | `781c034c3bd1dccad5bdc9e508443c081d5a7db4ce95b6eaf8b03fede997ca06` |
| `docs/roadmap/RUNTIME_FRONTEND_EXECUTION_CHECKLIST_v1.0.md` (pre-v1.2) | `c111cda3b5128268ae697283ba22ecfe2802e8cf793daf5f9de96f4b3383a07c` |

(Logs live under the session scratchpad; hashes bind them to this candidate.)

## 7. All 36 checklist statuses

**Totals: DONE 0 · VERIFIED_NOT_REVIEWED 16 · IMPLEMENTED_NOT_VERIFIED 4 · BLOCKED 16 · NOT_STARTED 0.**

- **VERIFIED_NOT_REVIEWED (16):** ME-03, API-01, API-03, MCP-01, MCP-02, MCP-04, FR-01, FR-02, FR-03, FR-07, FR-08, AX-01, AX-02, SE-01, SE-02, SE-03.
- **IMPLEMENTED_NOT_VERIFIED (4):** ME-01, ME-02, ME-04 (static REAL-BACKED), API-02 (static).
- **BLOCKED (16):** ME-05 (durable receipt + live inference — summary UI mounted+tested, durable receipt absent in backend), MCP-03, FR-04/05/06, FW-01/02, AP-01, SC-01/02/03/04, E2E-01/02/03/04.
- **DONE (0):** none. No row marked DONE this pass.

## 8. The six remaining PX nodes (Runtime-owned; frontend must NOT touch)

1. PX-01 `src/app/skills/index.test.tsx` — toolset mgmt: renders a switch for each toolset and toggles it off
2. PX-02 `src/app/skills/index.test.tsx` — toolset mgmt: renders the provider config panel inline
3. PX-03 `src/app/skills/index.test.tsx` — toolset mgmt: renders toolset titles without leading emoji
4. PX-04 `src/app/skills/index.test.tsx` — vision explainer deep-links to Settings → Models
5. PX-05 `src/app/settings/gateway-settings.test.tsx` — labels local mode as default inheritance for a named profile
6. PX-06 `src/app/messaging/index.test.tsx` — hides setup-guide button for a plugin platform with no docs URL

Accepted Runtime handoff: `NO_CODE_CHANGE_VERIFIED` (two official quiescent 0/0 runs) OR a real correction SHA. Until then the `0 failed / exit 0` full-suite gate stays BLOCKED. These are the ONLY failing nodes; zero new failures were introduced.

## 9. Sister dependency status (none integrated)

- **Runtime/Electron folder-grant SHA** — not received (FR-04/05/06, FW-01/02, AP-01, E2E-03).
- **Corrected Gateway file-ingress SHA** — not received; current Gateway candidate is NO-GO and MUST NOT be integrated (SC-01..04, E2E-04).
- **Runtime PX handoff** — not received.
- **Backend durable inference-receipt contract** — does not exist; ME-05 durable receipt BLOCKED.
- **OWNER** — live provider creds (ME-05/E2E-01), reachable MCP server (MCP-03/E2E-02), push/PR authorization.
- **Codex** — exact-SHA APPROVED (condition 10 of every DONE).

## 10. Verdict

**FRONTEND NO-GO.** Full-suite failure is exactly the six known PX nodes with zero new failures. No row is DONE. No push, PR, merge, deployment, or Gateway-candidate integration performed. Final worktree clean. Stop for Codex review of the exact candidate.
