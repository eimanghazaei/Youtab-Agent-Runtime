# Re-capture manifest: the 16 images that rendered the retired brand

**Status: closed.** All 16 have been re-captured or re-authored. The last
`scripts/youtab/branding_gate.py --ocr` run (engine: rapidocr-onnxruntime 1.4.4)
against `fix/youtab-runtime-completion-and-qualification` reads all 61 tracked
images, finds **0 unreadable** and **0 carrying the retired brand**, and exits 0.

The sections below are kept as written — they are the specification the capture
session was executed against, and the record of why each image needed what it
needed. What actually happened per group is recorded under
[How each group was closed](#how-each-group-was-closed) at the end.

This file exists because the fix is not a code change. It is a capture session
against a running, credentialed product, and this is the specification for it.

## The finding that makes this cheap

**The dashboard source is already fully Youtab-branded.** `web/src` contains
zero occurrences of the retired brand across all 61 tracked images' worth of UI:

```
$ grep -rniE "hermes|teknium|nousresearch" web/src | wc -l
0
$ grep -rn "label:" web/src/themes/presets.ts | head -1
label: "Youtab Teal"
$ grep -rn "org:" web/src/i18n/en.ts
org: "Youtab B.V."
```

Every one of the 16 images predates the rebrand. A capture taken today from the
current build renders `Youtab Teal` where the stale asset shows `HERMES TEAL`,
and `Youtab B.V.` where it shows `NOUS RESEARCH`. Nothing needs to be designed,
edited, or decided — the images simply need to be taken again.

## What must not be done

`website/static/img/docs/dashboard-models/picker-dialog.png` shows the failure
mode to avoid: a flat blue rectangle painted over the wordmark, leaving `AGENT`
legible beneath it, a second rectangle over the `Update ...` button, and
`HERMES TEAL` / `NOUS RESEARCH` untouched in the same frame. Editing pixels
manufactures the exact artifact the branding policy forbids. So does blurring,
cropping the offending region out, or allowlisting the path in the gate.

None of the 16 may be deleted: every one is referenced by live documentation
(column 3 below).

## Prerequisites the capture host needs

Not available in a CI container or an unauthenticated sandbox:

1. **Provider credentials** — the models page enumerates its catalog through
   `api.getModelOptions` / `api.getMoaModels` / `api.getAuxiliaryModels`, which
   resolve against live OpenRouter, Bedrock and Anthropic endpoints. The stale
   captures show `34 models`, `10 models`, `8 models` per provider.
2. **A configured gateway** — `admin-channels.png` shows the 27-channel grid and
   `admin-sessions.png` shows live session rows.
3. **A real dispatcher run** — seven kanban images show states that only exist
   after workers actually run, crash, retry and trip the circuit breaker. They
   cannot be hand-authored into `kanban.db` without asserting product behaviour
   nobody observed.
4. **A design pass** for the two assets in group D, which are not UI captures.

## Group A — model configuration (4)

Surface: `web/src/pages/ModelsPage.tsx`, `web/src/components/ModelPickerDialog.tsx`.
Detected string in all four: `NOUS RESEARCH` — the product's own vendor footer
(`v0.11.0 · NOUS RESEARCH`), not a third-party model vendor in the catalog.

| # | Image | Referenced by | State to reach |
| --- | --- | --- | --- |
| 1 | `website/static/img/docs/dashboard-models/overview.png` | `configuring-models.md`, `kanban-tutorial.md`, zh-Hans | Models page, default view |
| 2 | `website/static/img/docs/dashboard-models/picker-dialog.png` | `configuring-models.md`, zh-Hans | "Set main model" dialog open |
| 3 | `website/static/img/docs/dashboard-models/use-as-dropdown.png` | `configuring-models.md`, zh-Hans | "Use as" dropdown expanded |
| 4 | `website/static/img/docs/dashboard-models/auxiliary-expanded.png` | `configuring-models.md`, zh-Hans | Auxiliary section expanded |

## Group B — admin dashboard (3)

Referenced by `website/docs/user-guide/features/web-dashboard.md`.

| # | Image | Detected string | Why it is product identity |
| --- | --- | --- | --- |
| 5 | `admin-channels.png` | `hermes gateway start`, `/.hermes/.env` | Old product CLI and config root. Current build emits `youtab gateway start` and `~/.youtab-agent-runtime/` |
| 6 | `admin-sessions.png` | `HERMES TEAL` | Theme label; now `Youtab Teal` |
| 7 | `admin-system-top.png` | `teknium-dev` | Upstream developer's hostname in a shipped capture |

## Group C — kanban tutorial (7)

Referenced by `website/docs/user-guide/features/kanban-tutorial.md` and its
zh-Hans translation. Surface: the `plugins/kanban/dashboard` plugin, which ships
as a prebuilt bundle with no source in-tree, so it must be driven through the
running dashboard rather than rendered standalone.

| # | Image | Detected string | Documented state |
| --- | --- | --- | --- |
| 8 | `03-drawer-schema-task.png` | `Update Hermes` | Completed schema task, drawer open |
| 9 | `04b-drawer-retry-history-scrolled.png` | `Update Hermes` | Implementation task with two attempts: blocked then completed, scrolled |
| 10 | `06-drawer-crash-recovery.png` | `Update Hermes`, `HERMES TEAL` | Two-attempt history: 1 crashed + 1 completed |
| 11 | `08-pipeline-auth.png` | `HERMES` | Dashboard filtered by `auth-project` |
| 12 | `09-drawer-pipeline-review.png` | `HERMES TEAL` | Reviewer's drawer on `Review password reset PR` |
| 13 | `10-drawer-in-flight.png` | `HERMES` | Task claimed by `backend-dev`, not yet complete |
| 14 | `11-drawer-gave-up.png` | `HERMES TEAL` | Circuit breaker: 2 `spawn_failed` + 1 `gave_up` |

`Update Hermes` is the product's own update affordance, not a task title about a
third-party model. It is product identity.

## Group D — non-UI assets (2)

These are authored artwork, not screenshots. They need a design pass, not a
capture session.

| # | Image | Referenced by | Detected string |
| --- | --- | --- | --- |
| 15 | `optional-skills/security/unbroker/assets/unbroker.png` | `optional-skills/security/unbroker/README.md` | `GITHUB.COM/NOUSRESEARCH/HERMES-AGENT` — old product repository URL burned into the artwork |
| 16 | `plugins/youtab-achievements/docs/assets/achievements-dashboard-hd.png` | `plugins/youtab-achievements/README.md` | `HERMES NATIVE`, `NOUS RESEARCH` |

## Verifying a re-capture

Replace the file in place, keeping the path, then:

```bash
python -m pip install 'rapidocr-onnxruntime==1.4.4'
python scripts/youtab/branding_gate.py --root . --ocr --output /tmp/branding.json
```

The gate exits 0 only when `images_with_old_brand` reaches 0. It exits 1 while
any remain and 2 if no OCR engine is installed, so a host without the engine
cannot produce a false green.

## What is not in scope here

The text, path, filename, URL and package-metadata halves of the audit are
already clean and are not affected by any of the above:

| Surface | Result |
| --- | --- |
| Text occurrences | 300 total: 116 `LEGAL_PROVENANCE_ALLOWED`, 184 `THIRD_PARTY_DEPENDENCY_ALLOWED`, **0** in the forbidden classes |
| Tracked filenames and paths | 0 carry the retired brand |
| Package metadata | `youtab-agent-runtime`, author `Youtab B.V.`, entry points `youtab` / `youtab-agent-runtime` / `youtab-acp` / `youtab-agent-worker` |
| Installer, update and download paths | 0 point at the former upstream |

The 300 permitted occurrences are attribution and third-party facts, never
active product brand: `.mailmap` and skill `author:` metadata for real
contributors, and real model ids, OpenRouter slugs and external repositories the
product genuinely depends on or documents.

## How each group was closed

Every replacement is a photograph of this branch's own build, taken at the
original file's exact pixel dimensions and colour mode. Nothing was overlaid,
blurred, cropped, deleted, renamed, or allowlisted, and the gate itself was not
touched. Each replacement was OCR'd directly after being written into place.

**Group B (3) and the achievements asset** — re-captured first, from the
dashboard built out of this branch (`cd web && npm run build`) and served with
`youtab dashboard`. These needed no credentials.

**Group D `unbroker.png`** — redrawn from scratch at 879x1100 in the Youtab
palette and rendered headless from source. Not an edit of the old file.

**Group A — model configuration (4).** Taken against a real credentialed
provider: `provider: deepseek`, `model: deepseek-v4-pro`, with the key loaded
normally by the runtime from `~/.youtab-agent-runtime/.env`. The usage analytics
in `overview.png` and `use-as-dropdown.png` are real sessions this host ran
through DeepSeek, not seeded rows — which is why the cards read
`deepseek-v4-pro` (main) and `deepseek-v4-flash` with genuine token, cost and
tool-call counts. `dashboard.show_token_analytics` was switched on because the
page these figures document describes token counts and cost.

DeepSeek is named as DeepSeek throughout. It is a third-party provider, not
Youtab's model; `Youtab-1` stays reserved for Youtab's own.

**Group C — kanban tutorial (7).** Every state was produced by the real
dispatcher driving real workers, through the documented CLI. No row of
`kanban.db` was written or edited by hand:

| Figure | How the state was reached |
| --- | --- |
| `03-drawer-schema-task` | `backend-dev` worker ran the schema task and called `kanban_complete`; one `completed` run, 53s |
| `04b-drawer-retry-history-scrolled` | Run 1: the worker judged the reviewer's two concerns valid and called `kanban_block`. A human answered in a comment and ran `youtab kanban unblock`. Run 2: the dispatcher respawned it and the worker completed |
| `06-drawer-crash-recovery` | The worker was spawned, then SIGKILLed mid-flight. The dispatcher detected the dead pid on its own and reopened the task; the retry read the crash in its prior-attempt context and chose a chunked strategy |
| `08-pipeline-auth` | The live board, tenant-filtered to `auth-project` |
| `09-drawer-pipeline-review` | The reviewer's task, ready, parented on the completed implementation |
| `10-drawer-in-flight` | Captured while the `backend-dev` worker was genuinely running: run history shows one `active` run with no end |
| `11-drawer-gave-up` | Three real dispatcher passes against a task whose worker cannot be spawned: 2 `spawn_failed`, then `gave_up` at the task's `--max-retries 3` |

Two prose fixes went with these, so the tutorial describes what its figures now
show: Story 4's circuit-breaker example is stated as the spawn failure that
actually occurs, and the crash-recovery paragraph quotes the error the
dispatcher really records. Both are in the English and zh-Hans copies.

### One product bug found and fixed on the way

`plugins/kanban/dashboard/dist/` shipped a stylesheet whose 303 selectors were
named `youtab-agent-runtime-kanban-*` while the bundle's `className` strings
emitted `youtab-kanban-*`. The rebrand had renamed the two halves
inconsistently, so **none of the kanban plugin's styles applied** and the board
rendered as an unstyled vertical list. The bundle's own `querySelector` strings
already used the long name, so the 270 class-name tokens plus the one root class
were brought in line with them; the 7 custom event names (`youtab-kanban:drop`
and friends) were deliberately left alone. Without this the board could not be
photographed as the tutorial documents it.
