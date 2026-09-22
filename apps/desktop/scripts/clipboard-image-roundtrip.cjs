// Targeted runtime test: prove an ACTUAL PNG image round-trips through the
// clipboard in the app's real Electron/renderer environment — the same path
// electron/clipboard-image.ts uses (renderer secure-context navigator.clipboard
// + ClipboardItem, driven from main via webContents.executeJavaScript). This is
// NOT a text-only or API-presence check: it writes real PNG bytes and reads them
// back, verifying length and the PNG signature.
//
// Run: electron scripts/clipboard-image-roundtrip.cjs
// Prints CLIPBOARD_IMAGE_ROUNDTRIP_OK … or CLIPBOARD_IMAGE_ROUNDTRIP_FAIL:<reason>.
const { app, BrowserWindow, nativeImage } = require('electron')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

app.whenReady().then(async () => {
  // The renderer must be a SECURE context for ClipboardItem to exist, exactly
  // like the packaged app (loaded from file://). Load a real file:// page.
  const htmlPath = path.join(os.tmpdir(), `youtab-clip-rt-${Date.now()}.html`)
  fs.writeFileSync(htmlPath, '<!doctype html><html><body>clip</body></html>')
  const url = 'file:///' + htmlPath.replace(/\\/g, '/')

  const win = new BrowserWindow({ show: true, width: 320, height: 200 })
  try {
    await win.loadURL(url)
    win.focus()
    win.webContents.focus()
    await new Promise(r => setTimeout(r, 600))

    // A recognisable 8x8 PNG produced by nativeImage (main side).
    const png = nativeImage.createFromBitmap(Buffer.alloc(8 * 8 * 4, 0xcc), { width: 8, height: 8 }).toPNG()
    const b64 = png.toString('base64')

    // Drive the SAME renderer path clipboard-image.ts uses (userGesture=true).
    const res = await win.webContents.executeJavaScript(
      `(async () => {
        try {
          if (typeof ClipboardItem === 'undefined' || !(navigator.clipboard && navigator.clipboard.write && navigator.clipboard.read)) {
            return { ok:false, reason:'renderer lacks async image clipboard' };
          }
          const bin = atob(${JSON.stringify(b64)}); const arr = new Uint8Array(bin.length);
          for (let i=0;i<bin.length;i++) arr[i]=bin.charCodeAt(i);
          await navigator.clipboard.write([new ClipboardItem({ 'image/png': new Blob([arr], { type:'image/png' }) })]);
          const items = await navigator.clipboard.read();
          for (const it of items) {
            if (it.types && it.types.includes('image/png')) {
              const b = await it.getType('image/png'); const buf = new Uint8Array(await b.arrayBuffer());
              const sig = buf[0]===0x89 && buf[1]===0x50 && buf[2]===0x4e && buf[3]===0x47;
              return { ok:true, wrote: arr.length, read: buf.length, pngSig: sig };
            }
          }
          return { ok:false, reason:'no image/png read back' };
        } catch (e) { return { ok:false, reason: (e && e.name ? e.name+': ' : '') + (e && e.message || String(e)) }; }
      })()`,
      true
    )

    if (res && res.ok && res.read === png.length && res.pngSig) {
      console.log(`CLIPBOARD_IMAGE_ROUNDTRIP_OK wrote=${res.wrote} read=${res.read} pngSig=${res.pngSig} electron=${process.versions.electron}`)
      app.exit(0)
    } else {
      console.error('CLIPBOARD_IMAGE_ROUNDTRIP_FAIL:' + JSON.stringify(res))
      app.exit(1)
    }
  } catch (e) {
    console.error('CLIPBOARD_IMAGE_ROUNDTRIP_FAIL:' + (e && e.message ? e.message : String(e)))
    app.exit(1)
  } finally {
    try {
      fs.unlinkSync(htmlPath)
    } catch {
      // best-effort
    }
  }
})
