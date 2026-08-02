# Re-capture manifest: the 16 images that still render the retired brand

Produced by `scripts/youtab/branding_gate.py --ocr` (engine: rapidocr-onnxruntime
1.4.4) against `fix/youtab-runtime-completion-and-qualification`. Of 61 tracked
images, 61 were read, 0 were unreadable, and 16 carry the retired brand.

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
