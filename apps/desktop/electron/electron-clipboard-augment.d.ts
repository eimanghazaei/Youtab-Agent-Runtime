// Version-honest typing for the Electron main-process clipboard IMAGE API.
//
// FINDING (verified at runtime, electron@44.4.3 — see scripts/clipboard-smoke.cjs):
// this Electron build's `clipboard` exposes ONLY the async web-style surface
// (read/readText/write/writeText); the classic synchronous image methods
// `writeImage`/`readImage` are ABSENT at runtime (`typeof … === 'undefined'`),
// and electron's own `electron.d.ts` declares no such methods. Older/other
// Electron builds DO provide them, and the desktop's pre-existing image
// copy/paste (main.ts copyImageFromUrl / youtab:saveClipboardImage) calls them.
//
// So these methods are declared OPTIONAL — the honest shape: they MAY be present
// depending on the Electron build. This is NOT a fabricated "always present"
// declaration; call sites must feature-detect before use (and now do), which
// makes the code type-safe AND runtime-safe (no "writeImage is not a function"
// throw on a build that lacks them). No `any`, no `@ts-ignore`, no unsafe cast.
declare global {
  namespace Electron {
    interface Clipboard {
      writeImage?(image: NativeImage, type?: 'selection' | 'clipboard'): void
      readImage?(type?: 'selection' | 'clipboard'): NativeImage
    }
  }
}

export {}
