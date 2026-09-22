// Type completeness for the Electron main-process clipboard image API.
//
// electron@44's bundled `electron.d.ts` declares the async web-style Clipboard
// (read/readText/write/writeText → Promise) inside `declare namespace Electron`
// but OMITS the synchronous main-process image methods
// `clipboard.writeImage()` / `clipboard.readImage()`, even though they exist at
// runtime and the desktop uses them (paste/copy of composer images). Under the
// TS 6 toolchain this surfaces as
// `Property 'writeImage' does not exist on type 'Clipboard'` (TS2339).
//
// `clipboard` is typed as `Electron.Clipboard`, so augment that global
// namespace's interface with the real, documented signatures. This is a
// type-completeness fix, NOT a suppression — no `any`, no `@ts-ignore`, no
// disabled rule; it only affects the electron (main-process) project.
export {}

declare global {
  namespace Electron {
    interface Clipboard {
      writeImage(image: NativeImage, type?: 'selection' | 'clipboard'): void
      readImage(type?: 'selection' | 'clipboard'): NativeImage
    }
  }
}
