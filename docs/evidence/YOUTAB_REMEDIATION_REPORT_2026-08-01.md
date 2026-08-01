# Youtab Agent Runtime — branding and provenance remediation

**Date:** 2026-08-01
**Branch:** `fix/youtab-runtime-completion-and-qualification`
**Base:** `ee79f9359cd38c99e1ee9f1df6d0178b5359a8aa` (`agent/youtab-runtime-sovereignty`)
**Authority:** Owner execution prompt, 2026-08-01. No merge, deploy, release,
signing, VPS or Production authorization is claimed or exercised here.

---

## 1. What the previous pass actually did

The Owner reported that files and URLs still pointed at the former upstream.
That is true, and the audit found the opposite failure mode running alongside
it. The earlier rename was a blind string substitution, so it produced two
distinct defect classes:

**Under-replaced — old identity still present.**

- The English `README.md` had its banner and badge row deleted and never
  replaced. The Spanish, Urdu and Chinese READMEs kept theirs, so the primary
  README was the only one with no visual identity at all.
- The upstream author's handle and home directory were shipped inside test
  fixtures, example commands and docs: `/home/teknium/...`, `@teknium1`,
  `USER=teknium`, `assignee="teknium"`.
- 17 shipped images still render the old brand. The branding gate never
  looked, because OCR was opt-in behind a `tesseract` binary that is not
  installed.

**Over-replaced — third-party facts rewritten into fiction.**

This is the more damaging class, because it broke running code.

| Rewritten to | Actually is | Consequence |
| --- | --- | --- |
| `YoutabBV/Youtab-3-Llama-3.1-70B` | `NousResearch/Hermes-3-Llama-3.1-70B` | unresolvable Hugging Face id |
| `openrouter/youtab/youtab-3-llama-3.1-405b` | `openrouter/nousresearch/hermes-3-llama-3.1-405b` | unroutable OpenRouter slug |
| `youtab-3-405b`, `youtab-3-70b` | Hermes fallbacks in a provider profile | fallback chain pointed at nothing |
| `YoutabBV/youtab-example-plugins` | `NousResearch/hermes-example-plugins` | clone instructions 404 |
| `cocktailpeanut/youtab-mod` | `cocktailpeanut/hermes-mod` | dead link and broken doc image |
| `YoutabBV/gateway-gateway`, `/pokemon-agent`, `/kanban-video-pipeline`, `/nous-account-service` | `NousResearch/...` | dead references |
| `discord.gg/YoutabBV` | `discord.gg/NousResearch` vanity | 14 dead support links |
| `.mailmap`: `teknium@youtab.io`, `emozilla@youtab.io`, `ben@youtab.io`, `jonny@youtab.io` | `@nousresearch.com` | four real people given invented addresses, two relisted as "Youtab B.V. team" |

Two of these disabled live runtime behaviour outright:

- `youtab_agent_cli/model_switch.py` warned when the selected model was a
  non-agentic chat model. Its regex was rewritten from `hermes[-_ ]?[34]` to
  `youtab[-_ ]?[34]`. No such model exists, so the warning could never fire
  for the Hermes 3/4 models the Youtab Portal actually resells.
- `agent/coding_context.py` steers edit format from a needle list of model
  families. `hermes` had been replaced by `youtab`, so real Hermes models lost
  their `mode='replace'` steering and silently fell back to neutral wording.

Both are fixed and covered by tests, including negative cases the upstream
suite never had.

---

## 2. Policy applied

Youtab identity is used for everything Youtab owns. Third-party identifiers are
restored to their true values and classified as provenance, never as product
identity. Concretely:

- A real model id, external repository or vendor name is a **fact about a
  dependency**. Rewriting it does not rebrand anything; it breaks the reference
  and asserts something untrue.
- Contributor attribution is **provenance**. `.mailmap`, skill `author:`
  metadata and dated design-rationale comments keep the real names.
- A developer's home directory and handle inside a shipped fixture is
  **neither** — it is personal data riding along in a product artifact, and it
  is now generic.

The Youtab Portal keeps its own provider slot (`youtab`, `inference-api.youtab.io`,
`YOUTAB_API_KEY`). What changed is that it no longer advertises models that do
not exist, and the docs no longer claim Nous Research's Hermes 4 as "Youtab
B.V.'s own Youtab 4 family".

---

## 3. Gate matrix

Text gates run against a clean `git archive` export of the branch head.

| Gate | Command | Result |
| --- | --- | --- |
| Brand/URL classification | `scripts/youtab/brand_url_inventory.py` | **PASS** — 300 occurrences: 116 `LEGAL_PROVENANCE_ALLOWED`, 184 `THIRD_PARTY_DEPENDENCY_ALLOWED`, **0** in forbidden classes 4/5/6 |
| Branding gate (text + paths) | `scripts/youtab/branding_gate.py` | **PASS** — 0 blocking |
| Branding gate (OCR) | `scripts/youtab/branding_gate.py --ocr` | **FAIL** — 107 images read, 0 unreadable, **16 carry old brand** |
| Secret scan | `scripts/youtab/secret_gate.py` | **PASS** — 8,565 files, 0 findings, 34 fixture dispositions |
| SAST | `scripts/youtab/sast_gate.py` | **PASS** — 0 blocking, 12 reviewed inline dispositions, 20 targets |
| Dependency integrity | `scripts/youtab/dependency_gate.py` | **PASS** — 3,573 npm integrity, 2,053 python artifacts, 3 pinned actions, 0 errors |
| npm vulnerabilities | `npm audit` | **PARTIAL** — 9 → **2** (critical `tar` resolved; 2 high remain) |
| Python vulnerabilities | `pip-audit` | **FAIL** — 26 advisories across 4 packages |
| Python regression | `pytest tests` | see §4 |
| DNS / endpoint reachability | `nslookup` | **BLOCKED** — all DNS egress times out in this environment, including control hosts. No claim made about whether `youtab.io` endpoints resolve. |

Evidence files live beside this report in `docs/evidence/`.

### OCR blocker — the 16 images

OCR now runs via a pip-installable engine, so this is a real result rather than
a skipped check. Every one of these is referenced by live user documentation:

```
optional-skills/security/unbroker/assets/unbroker.png
plugins/youtab-achievements/docs/assets/achievements-dashboard-hd.png
website/static/img/dashboard/admin-channels.png
website/static/img/dashboard/admin-sessions.png
website/static/img/dashboard/admin-system-top.png          (contains "teknium")
website/static/img/docs/dashboard-models/auxiliary-expanded.png
website/static/img/docs/dashboard-models/overview.png
website/static/img/docs/dashboard-models/picker-dialog.png
website/static/img/docs/dashboard-models/use-as-dropdown.png
website/static/img/kanban-tutorial/03-drawer-schema-task.png
website/static/img/kanban-tutorial/04b-drawer-retry-history-scrolled.png
website/static/img/kanban-tutorial/06-drawer-crash-recovery.png
website/static/img/kanban-tutorial/08-pipeline-auth.png
website/static/img/kanban-tutorial/09-drawer-pipeline-review.png
website/static/img/kanban-tutorial/10-drawer-in-flight.png
website/static/img/kanban-tutorial/11-drawer-gave-up.png
```

`website/static/img/docs/dashboard-models/overview.png` shows what the earlier
pass attempted: a flat blue rectangle painted over the wordmark, leaving
"AGENT" beneath it and "Update Hermes", "HERMES TEAL" and "NOUS RESEARCH"
untouched elsewhere in the frame. That is the hiding the Owner explicitly ruled
out.

These cannot be fixed honestly from here. They are screenshots of the running
dashboard in specific application states — crash recovery, pipeline auth,
retry history — and reproducing them means running the Youtab-branded UI and
re-capturing, not editing pixels. Editing them would manufacture exactly the
kind of fake artifact the operating doctrine forbids. **This gate stays red.**

One further image, `achievements-tier-showcase-hd.png`, was orphaned (no
reference anywhere in the tree) and is deleted.

### Dependency advisories still open

| Ecosystem | Package | Advisories | Note |
| --- | --- | --- | --- |
| npm | `react-router`, `react-router-dom` | 2 high | fix only via `npm audit fix --force`, which downgrades to 7.11.0 — a breaking routing change needing UI verification |
| Python | `pillow` 12.2.0 | 20 | largest single contributor |
| Python | `mcp` 1.26.0 | 3 | |
| Python | `setuptools` 81.0.0 | 2 | |
| Python | `pytest` 9.0.2 | 1 | dev-only |

Python dependencies are exact-pinned by deliberate supply-chain policy
documented in `pyproject.toml`, so each bump requires a `uv lock` regeneration
and a full re-run. Not attempted here; recorded rather than hidden.

---

## 4. Python regression

Run on Windows 11 with CPython 3.12.10, inside the project's supported
`>=3.11,<3.14` range.

Two modules cannot be collected on Windows and are explicitly **not
qualified** on this platform rather than skipped silently:

- `tests/youtab_agent_cli/test_gateway.py` — imports `pty`, which needs the
  POSIX-only `termios`.
- `tests/test_run_tests_parallel.py` — creates `/tmp/youtab-isolation-probe`
  at import time, an absolute POSIX path.

Both need a Linux or macOS runner. Neither is affected by the changes here.

Pre-existing failures confirmed at the base SHA `ee79f935` in a separate
worktree, i.e. not introduced by this branch:

- `tests/agent/test_coding_context.py::TestWorkspaceBlock::test_empty_outside_repo`
  — walks up from `tmp_path` and finds a `package.json` in this machine's user
  profile.
- `tests/youtab_agent_cli/test_kanban_boards.py::TestBoardCRUD::test_remove_clears_init_cache_for_recreated_db`
  (both parametrisations) — Windows file locking.
- `tests/tools/test_local_env_windows_msys.py::TestGitBashCoreutilsOnPath::test_derives_dirs_from_portablegit_layout`
  — path separator assumption.

---

## 5. Known systemic issue not fixed here

`git grep -nE '\.read_text\(\s*\)'` finds roughly **505** encoding-free reads
across ~188 files. On a Windows host with a non-UTF-8 code page these raise
`UnicodeDecodeError`. One was already failing the suite and is fixed; the rest
are left for a scoped sweep rather than widened into a branding PR.

---

## 6. Change safety

- Full mirror taken **before** any edit:
  `_backups/agent-runtime-20260801/youtab-agent-runtime-mirror.git`
  — 60 refs (47 heads, 13 pull refs, 0 tags), `git fsck` clean.
- All-refs bundle: `youtab-agent-runtime-ALLREFS-20260801.bundle`,
  611,314,841 bytes,
  SHA-256 `3c2f2838b557e056a7c443cc3ca02894339fecaee95453ebcf4282da720673d0`.
  `git bundle verify` reports a complete history, exit 0.
- Ref manifest: `REFS_MANIFEST_20260801.tsv`.
- No branch, tag or commit deleted. No history rewritten. No force-push.
- `main` is untouched at `cc4cab2f592e60a197e796506de9168f74baf3ea`.
- The two historical refs were already published and match the handoff
  exactly: `product/youtab-agent-runtime-baseline` at `24dd1766`,
  `agent/youtab-runtime-sovereignty` at `ee79f935`. Nothing was republished.
- PR #6 remains closed and unmerged.
