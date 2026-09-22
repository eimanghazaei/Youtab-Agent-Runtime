// Image clipboard read/write for the desktop, via the SUPPORTED async path.
//
// electron@44's MAIN-process clipboard cannot read or write images: it has no
// sync `writeImage`/`readImage`, and `clipboard.write([...])` demands
// ClipboardItem instances while `ClipboardItem` is not constructible in the main
// process (verified at runtime — see scripts/clipboard-image-roundtrip.cjs). The
// working path is the renderer's secure-context Async Clipboard API
// (`navigator.clipboard` + `ClipboardItem`), which exists because the app loads
// its renderer from file:// (a secure context). We drive it through the app's
// own focused `webContents` — no new IPC surface, no preload change.
//
// Both helpers feature-detect and surface explicit errors; the caller prefers a
// sync `clipboard.writeImage`/`readImage` when a build provides it and only
// falls back to these when it does not.
import type { WebContents } from 'electron'

interface RendererResult {
  ok: boolean
  reason?: string
  b64?: string | null
}

// executeJavaScript with userGesture=true so the Async Clipboard write is
// treated as user-initiated (image copy is triggered from a context-menu click).
async function runInRenderer(webContents: WebContents, code: string): Promise<RendererResult> {
  return (await webContents.executeJavaScript(code, true)) as RendererResult
}

/** Write a PNG buffer to the clipboard via the renderer's Async Clipboard API. */
export async function writeImagePngToClipboardViaRenderer(webContents: WebContents, pngBuffer: Buffer): Promise<void> {
  const b64 = pngBuffer.toString('base64')

  const code = `(async () => {
    try {
      if (typeof ClipboardItem === 'undefined' || !(navigator.clipboard && navigator.clipboard.write)) {
        return { ok: false, reason: 'async clipboard image write unavailable in this renderer' };
      }
      const bin = atob(${JSON.stringify(b64)});
      const arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      await navigator.clipboard.write([new ClipboardItem({ 'image/png': new Blob([arr], { type: 'image/png' }) })]);
      return { ok: true };
    } catch (e) { return { ok: false, reason: (e && e.name ? e.name + ': ' : '') + (e && e.message || String(e)) }; }
  })()`

  const res = await runInRenderer(webContents, code)

  if (!res || !res.ok) {
    throw new Error(`clipboard image write failed: ${res && res.reason ? res.reason : 'unknown'}`)
  }
}

/**
 * Read a PNG image from the clipboard via the renderer's Async Clipboard API.
 * Returns the PNG bytes, or null when the clipboard holds no image.
 */
export async function readImagePngFromClipboardViaRenderer(webContents: WebContents): Promise<Buffer | null> {
  const code = `(async () => {
    try {
      if (!(navigator.clipboard && navigator.clipboard.read)) {
        return { ok: false, reason: 'async clipboard read unavailable in this renderer' };
      }
      const items = await navigator.clipboard.read();
      for (const it of items) {
        if (it.types && it.types.includes('image/png')) {
          const blob = await it.getType('image/png');
          const buf = new Uint8Array(await blob.arrayBuffer());
          let s = '';
          for (let i = 0; i < buf.length; i++) s += String.fromCharCode(buf[i]);
          return { ok: true, b64: btoa(s) };
        }
      }
      return { ok: true, b64: null };
    } catch (e) { return { ok: false, reason: (e && e.message) || String(e) }; }
  })()`

  const res = await runInRenderer(webContents, code)

  if (!res || !res.ok) {
    throw new Error(`clipboard image read failed: ${res && res.reason ? res.reason : 'unknown'}`)
  }

  return res.b64 ? Buffer.from(res.b64, 'base64') : null
}
