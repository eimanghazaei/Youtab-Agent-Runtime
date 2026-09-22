/**
 * Packaged-app acceptance: the REAL user path through the installed binary and
 * its REAL bundled sidecar backend — NOT dev source, NOT BOOT_FAKE.
 *
 * Open the packaged Youtab app, attach an image via a genuine composer paste
 * (the renderer's onPaste reads the image blob and stages it through the
 * bundled backend), submit the turn so the bundled backend PERSISTS it, verify
 * the thumbnail renders, then close the whole process tree, relaunch the SAME
 * packaged installation against the SAME isolated profile, reopen the session,
 * and verify the attachment survived with no stray `[screenshot]` placeholder
 * and no visible `@image:` reference. Also asserts the backend started from the
 * packaged resources (sidecar integrity gate passed) and leaves no orphan.
 */
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { DatabaseSync } from 'node:sqlite'

import {
  closePackagedApp,
  createSandbox,
  launchPackagedAppRealBackend,
  packagedBinaryExists,
  type Sandbox,
  waitForAppReady,
  writeEnvFile,
  writeMockProviderConfig,
} from './fixtures'
import { startMockServer } from './mock-server'
import { type ElectronApplication, expect, type Page, test } from './test'

const CAPTION = 'E2E packaged attachment must survive a relaunch'
const NATIVE_IMAGE_CONFIG = 'agent:\n  image_input_mode: native'
const IMAGE_NAME = 'e2e packaged capture.png'
const SURFACE = '[data-composer-target]:not([data-pane-hidden] [data-composer-target])'
// A 1x1-ish opaque PNG (same fixture the dev attachment spec uses).
const PNG_BASE64 =
  'iVBORw0KGgoAAAANSUhEUgAAAKAAAABkCAIAAACO1KzYAAAA30lEQVR42u3dwQ2AIBAAQTAWAx1iBXYI7diCuWhEMvP2dZsj+CL30hLr2oxAYARGYARGYARGYIERGIH53n7nozpOk5rQqIcNdkQjMAIjMNPeomP3N54V+5exwY5oBEZgBEZgBEZggREYgREYgREYgQVGYARGYARGYARGYIERGIERGIERGIEFRmAERmAERmAEFhiBERiBERiBERiBBUZgBEZgBEZgBBYYgREYgREYgRFYYCMQGIERmPSj94Njb9ligxEYgRFYYJaQe2mmYIMRGIERGIERGIEFRmAERmDedAFtjAtAGWDnoAAAAABJRU5ErkJggg=='

function activeViewportText(surfaceSelector: string): string {
  const surfaces = document.querySelectorAll(surfaceSelector)

  return surfaces[surfaces.length - 1]?.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? ''
}

async function transcriptText(page: Page): Promise<string> {
  return page.evaluate(activeViewportText, SURFACE)
}

function sessionRow(page: Page) {
  return page.locator('[data-slot="sidebar"] button').filter({ hasText: CAPTION }).first()
}

async function assertRendersThumbnail(page: Page, label: string): Promise<void> {
  const thumbnail = page.locator('[data-slot="aui_directive-image"] img')
  await expect(thumbnail, `${label}: the attachment should render as an image`).toHaveCount(1)
  await expect(thumbnail, `${label}: the thumbnail should resolve off disk`).toHaveAttribute('src', /^data:image\//)

  const text = await transcriptText(page)
  expect(text, `${label}: the caption should survive alongside the image`).toContain(CAPTION)
  expect(text, `${label}: the raw image path should not leak`).not.toContain(IMAGE_NAME)
  expect(text, `${label}: the image directive should not render literally`).not.toContain('@image:')
  expect(text, `${label}: the flattening placeholder should not render`).not.toContain('[screenshot]')
}

/** Read-only: does the profile's durable state.db hold a persisted user message
 * carrying the caption (i.e. the bundled backend flushed the turn)? */
function durableMessageHasCaption(youtabHome: string): boolean {
  const dbPath = path.join(youtabHome, 'state.db')
  if (!fs.existsSync(dbPath)) return false
  let db: DatabaseSync | null = null
  try {
    db = new DatabaseSync(dbPath, { readOnly: true })
    const row = db
      .prepare("SELECT COUNT(*) AS n FROM messages WHERE role = 'user' AND content LIKE ?")
      .get(`%${CAPTION}%`) as { n?: number } | undefined
    return Boolean(row && Number(row.n) > 0)
  } catch {
    return false
  } finally {
    try {
      db?.close()
    } catch {
      // ignore
    }
  }
}

/** Count live frozen-backend processes owned by the packaged app. */
function bundledBackendProcessCount(): number {
  try {
    const out = execFileSync('tasklist', ['/FI', 'IMAGENAME eq youtab-backend.exe', '/NH'], {
      encoding: 'utf8',
    })
    return (out.match(/youtab-backend\.exe/gi) || []).length
  } catch {
    return 0
  }
}

async function focusComposer(page: Page) {
  const composer = page.locator('[contenteditable="true"]').first()
  await composer.waitFor({ state: 'visible', timeout: 30_000 })
  await composer.click()
  return composer
}

/** Attach an image through the composer exactly as a user paste would: dispatch
 * a real DOM `paste` carrying an image/png blob; the composer's onPaste stages
 * it through the bundled backend (image.attach_bytes). */
async function pasteImage(page: Page): Promise<void> {
  const composer = page.locator('[contenteditable="true"]').first()
  await composer.evaluate((el, b64) => {
    const bin = atob(b64)
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    const file = new File([bytes], 'e2e packaged capture.png', { type: 'image/png' })
    const dt = new DataTransfer()
    dt.items.add(file)
    const evt = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true })
    el.dispatchEvent(evt)
  }, PNG_BASE64)
}

test.describe('packaged app: attachment persists across a full relaunch', () => {
  let mock: Awaited<ReturnType<typeof startMockServer>> | null = null
  let app: ElectronApplication | null = null
  let sandbox: Sandbox | null = null

  test.afterEach(async () => {
    if (app) await closePackagedApp(app).catch(() => undefined)
    app = null
    if (mock) await mock.close().catch(() => undefined)
    mock = null
    if (sandbox) sandbox.cleanup()
    sandbox = null
  })

  test('paste → submit → thumbnail → close tree → relaunch → thumbnail survives', async ({}, testInfo) => {
    test.skip(!packagedBinaryExists(), 'requires the packaged binary — run npm run dist:win first')
    // Two full packaged boots of a real frozen backend do not fit the default budget.
    test.slow()
    test.setTimeout(600_000)

    mock = await startMockServer()
    sandbox = createSandbox('packaged-attach')
    writeMockProviderConfig(sandbox.youtabHome, mock.url, undefined, NATIVE_IMAGE_CONFIG)
    writeEnvFile(sandbox.youtabHome)

    // ── Launch 1: packaged binary + REAL bundled backend ──────────────
    ;({ app } = await launchPackagedAppRealBackend(sandbox))
    const page1 = await app.firstWindow()
    await waitForAppReady({ page: page1, app } as never, 240_000)

    // The app reached ready with NO dev-root override, so its backend is the
    // packaged bundled sidecar (resolveYoutabBackend's IS_PACKAGED branch), and
    // the bundled backend only launches when the integrity gate passes over the
    // shipped bundle (sidecar-refused otherwise surfaces a boot-failure overlay
    // that waitForAppReady would never clear).

    // Attach + submit through the packaged UI so the BUNDLED backend persists.
    const composer = await focusComposer(page1)
    await composer.type(CAPTION, { delay: 10 })
    await pasteImage(page1)
    // The staged attachment pill must appear before we submit — this is the
    // bundled backend having staged the pasted bytes into the session workspace
    // (image.attach_bytes → composer-images/…png).
    await page1.locator('[data-slot="composer-attachments"]').waitFor({ state: 'visible', timeout: 30_000 })
    await page1.keyboard.press('Enter')

    // The bundled backend runs the turn (mock provider) and PERSISTS it. On the
    // LIVE turn the attached image renders with its Open/Download affordance
    // (the persisted @image: directive-image form is asserted after relaunch).
    await page1.getByRole('button', { name: /open image/i }).first()
      .waitFor({ state: 'visible', timeout: 180_000 })
    const liveText = await transcriptText(page1)
    expect(liveText, 'first open: caption present').toContain(CAPTION)
    expect(liveText, 'first open: no [screenshot] leak').not.toContain('[screenshot]')
    expect(liveText, 'first open: no @image: leak').not.toContain('@image:')
    await page1.screenshot({ path: testInfo.outputPath('packaged-first-open.png') })

    // The bundled backend must DURABLY persist the turn before we tear down —
    // poll the profile's state.db (read-only) until the user message with the
    // caption is committed, so the relaunch reads a real durable row.
    await expect
      .poll(() => durableMessageHasCaption(sandbox!.youtabHome), {
        timeout: 120_000,
        message: 'bundled backend must durably persist the attachment turn to state.db',
      })
      .toBe(true)

    // ── Full process-tree close (app + bundled sidecar) ───────────────
    await closePackagedApp(app)
    app = null
    await expect
      .poll(() => bundledBackendProcessCount(), { timeout: 20_000, message: 'bundled backend must not orphan' })
      .toBe(0)

    // ── Launch 2: SAME packaged installation, SAME isolated profile ───
    ;({ app } = await launchPackagedAppRealBackend(sandbox))
    const page2 = await app.firstWindow()
    await waitForAppReady({ page: page2, app } as never, 240_000)

    const row = sessionRow(page2)
    await row.waitFor({ state: 'visible', timeout: 60_000 })
    await row.click()
    await page2.waitForFunction(
      ([expected, surfaceSelector]: [string, string]) => {
        const surfaces = document.querySelectorAll(surfaceSelector)
        const text = surfaces[surfaces.length - 1]?.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? ''
        return text.includes(expected)
      },
      [CAPTION, SURFACE] as [string, string],
      { timeout: 30_000 },
    )
    await assertRendersThumbnail(page2, 'packaged relaunch')
    await page2.screenshot({ path: testInfo.outputPath('packaged-relaunch.png') })

    await closePackagedApp(app)
    app = null
    await expect
      .poll(() => bundledBackendProcessCount(), { timeout: 20_000, message: 'no orphan after relaunch close' })
      .toBe(0)

    // Diagnostics reference (redacted): the profile was isolated + disposable.
    expect(fs.existsSync(path.join(sandbox.youtabHome, 'state.db')), 'the profile persisted the session DB across launches').toBe(true)
  })
})
