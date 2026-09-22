/**
 * Built-in desktop themes. Names match the CLI skins / dashboard presets.
 * Add new themes here — no code changes needed elsewhere.
 */

import type { DesktopTheme, DesktopThemeTypography } from './types'

// Color-emoji fonts to append to every stack as a last resort. None of the UI
// text/mono fonts carry emoji glyphs, so without this emoji render as tofu
// boxes on platforms whose default text font lacks them (e.g. Linux/#40364).
// Covers macOS, Windows, Linux, plus the `emoji` generic for anything else.
export const EMOJI_FALLBACK = '"Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji", emoji'

// Match the Youtab Web Platform's default font stacks verbatim
// (web/src/index.css `--theme-font-sans` / `--theme-font-mono`), with the
// emoji fallback appended so glyphs still render. The web dashboard renders
// body/wordmark text in this system stack (not a bundled display face), so
// mirroring it here gives the desktop the same typography.
const SYSTEM_SANS =
  'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif, ' + EMOJI_FALLBACK

const SYSTEM_MONO = 'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace, ' + EMOJI_FALLBACK

export const DEFAULT_TYPOGRAPHY: DesktopThemeTypography = { fontSans: SYSTEM_SANS, fontMono: SYSTEM_MONO }

const YOUTAB_EMERALD = '#34D399' // day-mode accent (matches the web emerald seed)
const PSYCHE_WARM = '#FFE6CB'

// Youtab Web Platform "Youtab Teal" (LENS_0) default palette — the visual
// source of truth. See web/src/index.css + web/src/themes/presets.ts.
const WEB_TEAL_BG = '#041C1C' // deep teal canvas (web --background-base)
const WEB_CREAM = PSYCHE_WARM // #FFE6CB — web --midground-base (primary text/chrome)
const WEB_CREAM_15 = 'color-mix(in srgb, #FFE6CB 15%, transparent)' // web --color-border / --color-input
const WEB_CREAM_18 = 'color-mix(in srgb, #FFE6CB 18%, transparent)'
const WEB_DESTRUCTIVE = '#FB2C36' // web --color-destructive

/**
 * Youtab — canonical Youtab desktop identity. The palette keeps the current
 * glass geometry neutral, then lets the old bb/gui blue and psyche cream
 * return as accent seeds.
 */
export const youtabTheme: DesktopTheme = {
  name: 'youtab',
  label: 'Youtab',
  description: 'Glass neutrals with Youtab blue accents',
  // LIGHT variant — clean WHITE canvas + near-BLACK text (Day mode). The emerald
  // Youtab accent keeps chrome alive; borders are a soft neutral grey. This is
  // the deliberate white/black day theme paired with the Sun/Moon quick toggle.
  colors: {
    background: '#FFFFFF',
    foreground: '#0A0A0A',
    card: '#FFFFFF',
    cardForeground: '#0A0A0A',
    muted: '#F0F0EE',
    mutedForeground: '#5A5A57',
    popover: '#FFFFFF',
    popoverForeground: '#0A0A0A',
    primary: YOUTAB_EMERALD,
    primaryForeground: '#04231A',
    secondary: '#F0F0EE',
    secondaryForeground: '#1A1A18',
    accent: YOUTAB_EMERALD,
    accentForeground: '#04231A',
    border: '#E2E2DE',
    input: '#E2E2DE',
    ring: YOUTAB_EMERALD,
    midground: '#0A0A0A',
    composerRing: YOUTAB_EMERALD,
    destructive: '#C72E4D',
    destructiveForeground: '#FFFFFF',
    sidebarBackground: '#F7F7F6',
    sidebarBorder: '#E2E2DE',
    userBubble: '#F0F0EE',
    userBubbleBorder: '#E2E2DE'
  },
  // DARK variant (desktop default) — the web "Youtab Teal" (LENS_0) palette:
  // deep teal canvas, cream primary/text, cream-alpha borders. Every derived
  // --ui-*/--dt-*/--color-* token in styles.css recomputes from these seeds,
  // so the whole app inherits the web look.
  darkColors: {
    background: WEB_TEAL_BG,
    foreground: WEB_CREAM,
    card: '#0C2222',
    cardForeground: WEB_CREAM,
    muted: '#152A2A',
    mutedForeground: '#C9BBA6',
    popover: '#0C2222',
    popoverForeground: WEB_CREAM,
    primary: WEB_CREAM,
    primaryForeground: WEB_TEAL_BG,
    secondary: '#102626',
    secondaryForeground: WEB_CREAM,
    accent: '#182B2B',
    accentForeground: WEB_CREAM,
    border: WEB_CREAM_15,
    input: WEB_CREAM_15,
    ring: WEB_CREAM,
    midground: WEB_CREAM,
    composerRing: WEB_CREAM,
    destructive: WEB_DESTRUCTIVE,
    destructiveForeground: '#FFFFFF',
    sidebarBackground: '#052121',
    sidebarBorder: WEB_CREAM_15,
    userBubble: '#0C2222',
    userBubbleBorder: WEB_CREAM_18
  },
  // Match the web default typography: system sans + system mono, no external
  // font fetch (the web dashboard does not network-load a mono face by default).
  typography: {
    fontSans: SYSTEM_SANS,
    fontMono: SYSTEM_MONO
  }
}

/** Deep blue-violet with cool accents. Matches the dashboard midnight theme. */
export const midnightTheme: DesktopTheme = {
  name: 'midnight',
  label: 'Midnight',
  description: 'Deep blue-violet with cool accents',
  colors: {
    background: '#08081c',
    foreground: '#ddd6ff',
    card: '#0d0d28',
    cardForeground: '#ddd6ff',
    muted: '#13133a',
    mutedForeground: '#7c7ab0',
    popover: '#0f0f2e',
    popoverForeground: '#ddd6ff',
    primary: '#ddd6ff',
    primaryForeground: '#08081c',
    secondary: '#1a1a4a',
    secondaryForeground: '#c4bff0',
    accent: '#1a1a44',
    accentForeground: '#d0c8ff',
    border: '#1e1e52',
    input: '#1e1e52',
    ring: '#8b80e8',
    midground: '#8b80e8',
    destructive: '#b03060',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#06061a',
    sidebarBorder: '#12123a',
    userBubble: '#14143a',
    userBubbleBorder: '#242466'
  },
  typography: {
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`,
    fontUrl: 'https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&display=swap'
  }
}

/** Warm crimson and bronze — forge vibes. Matches the CLI ares skin. */
export const emberTheme: DesktopTheme = {
  name: 'ember',
  label: 'Ember',
  description: 'Warm crimson and bronze — forge vibes',
  colors: {
    background: '#160800',
    foreground: '#ffd8b0',
    card: '#1e0e04',
    cardForeground: '#ffd8b0',
    muted: '#2a1408',
    mutedForeground: '#aa7a56',
    popover: '#221008',
    popoverForeground: '#ffd8b0',
    primary: '#ffd8b0',
    primaryForeground: '#160800',
    secondary: '#341800',
    secondaryForeground: '#f0c090',
    accent: '#301600',
    accentForeground: '#e8c080',
    border: '#3a1c08',
    input: '#3a1c08',
    ring: '#d97316',
    midground: '#d97316',
    destructive: '#c43010',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#100600',
    sidebarBorder: '#2a1004',
    userBubble: '#2a1000',
    userBubbleBorder: '#4a2010'
  },
  typography: {
    fontMono: `"IBM Plex Mono", ${SYSTEM_MONO}`,
    fontUrl: 'https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;700&display=swap'
  }
}

/** Clean grayscale. Matches the CLI mono skin and dashboard mono theme. */
export const monoTheme: DesktopTheme = {
  name: 'mono',
  label: 'Mono',
  description: 'Clean grayscale — minimal and focused',
  colors: {
    background: '#0e0e0e',
    foreground: '#eaeaea',
    card: '#141414',
    cardForeground: '#eaeaea',
    muted: '#1e1e1e',
    mutedForeground: '#808080',
    popover: '#181818',
    popoverForeground: '#eaeaea',
    primary: '#eaeaea',
    primaryForeground: '#0e0e0e',
    secondary: '#262626',
    secondaryForeground: '#c8c8c8',
    accent: '#222222',
    accentForeground: '#d8d8d8',
    border: '#2a2a2a',
    input: '#2a2a2a',
    ring: '#9a9a9a',
    midground: '#9a9a9a',
    destructive: '#a84040',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#0a0a0a',
    sidebarBorder: '#202020',
    userBubble: '#1a1a1a',
    userBubbleBorder: '#363636'
  }
}

/** Neon green on black. Matches the CLI cyberpunk skin and dashboard theme. */
export const cyberpunkTheme: DesktopTheme = {
  name: 'cyberpunk',
  label: 'Cyberpunk',
  description: 'Neon green on black — matrix terminal',
  colors: {
    background: '#000a00',
    foreground: '#00ff41',
    card: '#001200',
    cardForeground: '#00ff41',
    muted: '#001a00',
    mutedForeground: '#1a8a30',
    popover: '#001000',
    popoverForeground: '#00ff41',
    primary: '#00ff41',
    primaryForeground: '#000a00',
    secondary: '#002800',
    secondaryForeground: '#00cc34',
    accent: '#002000',
    accentForeground: '#00e038',
    border: '#003000',
    input: '#003000',
    ring: '#00ff41',
    midground: '#00ff41',
    destructive: '#ff003c',
    destructiveForeground: '#000a00',
    sidebarBackground: '#000600',
    sidebarBorder: '#001800',
    userBubble: '#001400',
    userBubbleBorder: '#004800'
  },
  typography: {
    fontMono: `"Courier New", Courier, monospace, ${EMOJI_FALLBACK}`,
    fontSans: `"Courier New", Courier, monospace, ${EMOJI_FALLBACK}`
  }
}

/** Cool slate blue for developers. Matches the CLI slate skin. */
export const slateTheme: DesktopTheme = {
  name: 'slate',
  label: 'Slate',
  description: 'Cool slate blue — focused developer theme',
  colors: {
    background: '#0d1117',
    foreground: '#c9d1d9',
    card: '#161b22',
    cardForeground: '#c9d1d9',
    muted: '#21262d',
    mutedForeground: '#8b949e',
    popover: '#1c2128',
    popoverForeground: '#c9d1d9',
    primary: '#c9d1d9',
    primaryForeground: '#0d1117',
    secondary: '#2a3038',
    secondaryForeground: '#adb5bf',
    accent: '#1e2530',
    accentForeground: '#c0c8d0',
    border: '#30363d',
    input: '#30363d',
    ring: '#58a6ff',
    midground: '#58a6ff',
    destructive: '#cf4848',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#090d13',
    sidebarBorder: '#1c2228',
    userBubble: '#1e2a38',
    userBubbleBorder: '#2e4060'
  },
  typography: {
    fontMono: `"JetBrains Mono", ${SYSTEM_MONO}`
  }
}

export const BUILTIN_THEMES: Record<string, DesktopTheme> = {
  youtab: youtabTheme,
  midnight: midnightTheme,
  ember: emberTheme,
  mono: monoTheme,
  cyberpunk: cyberpunkTheme,
  slate: slateTheme
}

export const BUILTIN_THEME_LIST = Object.values(BUILTIN_THEMES)

/** Skin used when nothing is persisted or the persisted name is retired. */
export const DEFAULT_SKIN_NAME = 'youtab'
