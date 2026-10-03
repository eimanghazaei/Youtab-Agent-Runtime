import type { ThemeAssets } from "./types";

/** Well-known asset slots, mirrored by `_THEME_NAMED_ASSET_KEYS` in the backend. */
const NAMED_ASSET_KEYS = ["bg", "hero", "logo", "crest", "sidebar", "header"] as const;

/** Build CSS variables for named and custom theme assets. */
export function assetVars(assets: ThemeAssets | undefined): Record<string, string> {
  if (!assets) return {};
  const out: Record<string, string> = {};
  const wrap = (v: string): string => {
    const trimmed = v.trim();
    if (!trimmed) return "";
    // Existing CSS expressions are passed through by design.
    if (/^(url\(|linear-gradient|radial-gradient|conic-gradient|none$)/i.test(trimmed)) {
      return trimmed;
    }
    // Escape a CSS quoted string. A backslash must be escaped before a quote;
    // otherwise an input can escape the closing quote of url("...").
    const escaped = Array.from(trimmed, (char) => {
      if (char === "\\") return "\\\\";
      if (char === '"') return '\\"';
      const code = char.charCodeAt(0);
      return code < 0x20 || code === 0x7f ? `\\${code.toString(16)} ` : char;
    }).join("");
    return `url("${escaped}")`;
  };
  for (const key of NAMED_ASSET_KEYS) {
    const val = assets[key];
    if (typeof val === "string" && val.trim()) {
      out[`--theme-asset-${key}`] = wrap(val);
      out[`--theme-asset-${key}-raw`] = val;
    }
  }
  if (assets.custom) {
    for (const [key, val] of Object.entries(assets.custom)) {
      if (typeof val !== "string" || !val.trim()) continue;
      if (!/^[a-zA-Z0-9_-]+$/.test(key)) continue;
      out[`--theme-asset-custom-${key}`] = wrap(val);
      out[`--theme-asset-custom-${key}-raw`] = val;
    }
  }
  return out;
}
