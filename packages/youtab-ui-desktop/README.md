# youtab ui

the shared design system for react ui @ youtab. components, hooks, utils, fonts, overlays, etc.

## setup

`npm i @youtab/ui` or `pnpm i @youtab/ui`

import the global styles in your app's root css file:

```css
@import 'tailwindcss';
@import '@youtab/ui/styles/globals.css';
```

## usage

everything exports from the top level export:

```tsx
import {
  Button,
  Grid
  // whatever else!
} from '@youtab/ui'
```

## local development

two ways to preview components while editing:

### storybook (per-component, primary)

```
pnpm storybook        # dev server on http://localhost:6006
pnpm build-storybook  # static build → ./storybook-static
```

stories live in `stories/*.stories.tsx`. the **Overview** story re-renders
the entire `/ds` page, so you always have a kitchen-sink view alongside the
isolated component stories. the **Lens** toolbar above the preview cycles
between the lens presets (`0`, `1`, `2`, `3`, `4`, `5`, `5i`, `6`).

### next.js showcase (kitchen-sink, secondary)

```
pnpm dev              # next dev on http://localhost:3000 → redirects to /ds
```

`app/ds/page.tsx` is the same content the Overview story renders.

## publishing

```
pnpm build     # tsc → dist/ + copies css / fonts / assets
pnpm release   # runs scripts/release.sh (must be on main, clean tree)
```

the published tarball only ships `dist/` and `README.md` (see `files` in
`package.json`). `.d.ts.map` + inline sources are emitted so consumers can
"go to definition" straight into the TypeScript source.

## deployment

`vercel.json` builds both and serves them on a single Next.js deployment:

- `/` and `/ds` — the Next.js showcase (same as `pnpm dev`)
- `/storybook` — the static Storybook build (copied to `public/storybook/`
  before `next build` so Next serves it like any other static asset)

Want Storybook-only? replace `buildCommand` with `pnpm build-storybook` and
add `"outputDirectory": "storybook-static"`. `app/ds` can stay for local
`pnpm dev` either way — the Overview story renders the same content.
