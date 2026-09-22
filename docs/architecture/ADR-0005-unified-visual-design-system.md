# ADR-0005 — Unified Youtab visual design system across all surfaces

**Status:** Accepted (partially implemented — see §7)
**Date:** 2026-09-22
**Supersedes/elevates:** the pre-ADR plan `docs/design/design-system-unification-plan.md` (on `feat/electron-visual-web-parity`)
**Scope:** `web/` (dashboard), `apps/desktop/` (Electron), shared packages `@youtab/ui`, `@youtab/ui-desktop`
**Base SHA:** `22ef28e1afd247d4a1a4507cf57523365b5277bd` (branch `delivery/runtime-frontend-owner-1-12-v2`)

## Context

The product is built by multiple parallel sessions across two apps (web dashboard → `@youtab/ui`; desktop → `@youtab/ui-desktop`). Appearance and brand were applied **at the app level**, not in the shared DS: `web/src/index.css` overrode the shared `:root` to the LENS_0 "Youtab Teal" palette while `@youtab/ui`'s own default was **amber** (`#170d02`/`#ffac02`); the desktop palette lived inline in `apps/desktop/src/styles.css` + `presets.ts`, and `@youtab/ui-desktop` shipped dist-only with no source tokens. Consequence: a surface is only "on theme" if its app happens to apply the theme, and only the palette is shared — not the brand marks, fonts, or icons. New features (or hard-coded colors) drift. There is no single source of truth every session inherits.

## Decision

Promote the appearance to the shared DS as the **default**, and require feature code to consume **semantic tokens / DS components only** (never raw colors or one-off icons/marks). Canonical palette = LENS_0 "Youtab Teal": bg `#041C1C`, cream `#FFE6CB`, emerald `#34D399`, destructive `#FB2C36`, success `#4ADE80`, warning `#FFBD38`, border/input `color-mix(in srgb,#FFE6CB 15%,transparent)`, radius `0.5rem`, Ice Blue `#A8E7FF`; system sans/mono stacks. Brand marks (Simorgh orb, Persepolis bull backdrop) and a Tabler icon registry become shared. A **lint guard** fails on raw hex colors and non-registry icon imports inside feature directories — the mechanism that guarantees any option a session adds looks the same. Visual-only: no functional/API/security/permission/persistence change; no option removed.

## Implementation (this branch)

- **`7ccb830131559d51bc4b9c71441ebe44912a7c28`** — `@youtab/ui` `:root` default is now LENS_0 "Youtab Teal" + shadcn `@theme inline --color-*` remaps + radius + `--youtab-ice-blue: #A8E7FF`, replacing amber; the now-redundant `:root` override removed from `web/src/index.css`. **Zero visual change on web** (it already overrode to teal): built dashboard CSS has 0 amber occurrences, teal + ice-blue present; web build + typecheck exit 0; `src/ui/globals.css == dist/ui/globals.css`.
- **`3111d3d9e588214c2389054f9d25c1ee78eadbca`** — design-system lint guard: `web/eslint.config.js` (`src/pages/runtime/**`) and `apps/desktop/eslint.config.mjs` (`src/app/enterprise/**`, `src/app/governance/**`) fail on raw hex colors and direct `lucide-react`/`@tabler/icons-react` imports; shared runtime icon registry `web/src/lib/runtime-icons.ts`. Both feature surfaces are already raw-hex-free and lint clean under the guard; a planted violation fails with 2 errors in each app.

Feature surfaces `apps/desktop/src/app/enterprise`, `apps/desktop/src/app/governance`, and `web/src/pages/runtime` were audited: **0 raw hex colors** — they already consume DS semantic tokens, so they inherit the shared palette automatically.

## §7 Deferred (blocked — needs the sanctioned build env + branch coordination)

Two items are NOT landed here and are flagged for the Owner:

1. **`@youtab/ui-desktop` token extraction + desktop repoint** and **brand marks (SimorghOrb/Loader/icon registry/Backdrop) into `@youtab/ui` dist**: blocked because this is a **pnpm workspace** but the work happens in an **npm worktree** — the package `unbuild` fails pre-existingly (`badge.stories.tsx` TS4023, unrelated) and emits `.mjs` while the committed dist is `.js`, so a matching, verifiable package `dist` cannot be regenerated locally. Shipping hand-fabricated dist would be unverifiable; deferred rather than faked.
2. **Desktop app-level theming collision:** repointing `apps/desktop/src/styles.css`/`presets.ts` to a shared package collides at merge with the in-flight `feat/electron-visual-web-parity` branch, which owns the desktop teal redesign at the app level (its own `styles.css` ~2.5k lines + `presets.ts`). This must be sequenced with that branch's owner.

**Recommendation:** land the shared-package token/brand extraction and the desktop repoint in a pnpm environment (so `@youtab/ui`/`@youtab/ui-desktop` dist regenerates verifiably), coordinated with the `feat/electron-visual-web-parity` owner.

## Consequences

- Web + any `@youtab/ui` consumer now inherit LENS_0 by default; a new app starts on-theme with no per-app override.
- The lint guard prevents future palette/icon drift in the enterprise/governance/runtime feature dirs.
- Desktop full parity (teal tokens from the shared package) remains pending the deferred items above; desktop feature code is already token-based, so it will inherit correctly once repointed.

## Non-goals

No functional/API/security/permission/persistence change; hides/removes no option; merges/deploys nothing by itself.
