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
  // Select the session row by its stable session-id hook, NOT by label text:
  // the sidebar row shows the auto-generated session TITLE (e.g. the
  // assistant's first line), which is nondeterministic and is NOT the caption,
  // so matching by CAPTION races title generation. The row body button carries
  // data-session-id (session-row.tsx). The suite uses a single session, so the
  // first such row is the one under test.
  return page.locator('[data-slot="sidebar"] [data-session-id]').first()
}

async function assertRendersThumbnail(page: Page, label: string): Promise<void> {
  const thumbnail = page.locator('[data-slot="aui_directive-image"] img')
  // Resolving a persisted @image: directive to an inline data: thumbnail on a
  // cold reopen reads the file off disk asynchronously; wait for it to mount
  // before asserting (a generous wait for the async render, NOT a durability
  // wait — durability is enforced in product code before message.complete).
  await thumbnail.first().waitFor({ state: 'visible', timeout: 60_000 })
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
  return durableUserTurnRecovered(youtabHome, { requireImageRef: false })
}

/** Read-only durable-recovery check: the profile's state.db must hold a user
 * message carrying the caption AND (by default) its @image: attachment ref — the
 * deterministic proof that the completed attachment turn survived a prompt close.
 */
function durableUserTurnRecovered(
  youtabHome: string,
  opts: { requireImageRef?: boolean } = {},
): boolean {
  const requireImageRef = opts.requireImageRef ?? true
  const dbPath = path.join(youtabHome, 'state.db')
  if (!fs.existsSync(dbPath)) return false
  let db: DatabaseSync | null = null
  try {
    db = new DatabaseSync(dbPath, { readOnly: true })
    const clause = requireImageRef
      ? "content LIKE ? AND content LIKE '%@image:%'"
      : "content LIKE ?"
    const row = db
      .prepare(`SELECT COUNT(*) AS n FROM messages WHERE role = 'user' AND ${clause}`)
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

// The crash-recovery continuation the gateway synthesizes on resume is a
// DISTINCT user record that embeds the original prompt so the model re-answers
// it (tui_gateway/server.py _AUTO_CONTINUE_NOTE_PREFIX). It is not a duplicate
// of the user's turn, so the no-duplicate check must exclude it — otherwise a
// legitimate recovery note reads as a duplicated turn.
const AUTO_CONTINUE_NOTE_PREFIX = '[System note: Your previous turn was interrupted mid-run'

/** Count durable GENUINE user turns carrying the caption — excluding the
 * crash-recovery continuation note — for the no-duplicate-record check. */
function durableCaptionTurnCount(youtabHome: string): number {
  const dbPath = path.join(youtabHome, 'state.db')
  if (!fs.existsSync(dbPath)) return 0
  let db: DatabaseSync | null = null
  try {
    db = new DatabaseSync(dbPath, { readOnly: true })
    const row = db
      .prepare(
        "SELECT COUNT(*) AS n FROM messages WHERE role = 'user' AND content LIKE ? AND content NOT LIKE ?",
      )
      .get(`%${CAPTION}%`, `${AUTO_CONTINUE_NOTE_PREFIX}%`) as { n?: number } | undefined
    return row ? Number(row.n) : 0
  } catch {
    return 0
  } finally {
    try { db?.close() } catch { /* ignore */ }
  }
}

/** Count durable ASSISTANT messages in the profile's state.db — the turn's
 * COMPLETION. Zero means the completion never persisted (the turn did not
 * durably complete); >=1 after recovery means the re-run committed the reply. */
function durableAssistantReplyCount(youtabHome: string): number {
  const dbPath = path.join(youtabHome, 'state.db')
  if (!fs.existsSync(dbPath)) return 0
  let db: DatabaseSync | null = null
  try {
    db = new DatabaseSync(dbPath, { readOnly: true })
    const row = db
      .prepare("SELECT COUNT(*) AS n FROM messages WHERE role = 'assistant'")
      .get() as { n?: number } | undefined
    return row ? Number(row.n) : 0
  } catch {
    return 0
  } finally {
    try { db?.close() } catch { /* ignore */ }
  }
}

/** Does the on-disk crash-recovery marker file hold at least one turn entry?
 * (tui_gateway/turn_marker.py: <home>/desktop/interrupted_turns.json). */
function crashMarkerHasEntry(youtabHome: string): boolean {
  const p = path.join(youtabHome, 'desktop', 'interrupted_turns.json')
  if (!fs.existsSync(p)) return false
  try {
    const data = JSON.parse(fs.readFileSync(p, 'utf8'))
    return data && typeof data === 'object' && Object.keys(data).length > 0
  } catch {
    return false
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

/** Full executable paths of live youtab-backend.exe processes (win32), so a test
 * can prove the running backend was spawned from the PACKAGED resources tree and
 * not a dev checkout. */
function backendProcessPaths(): string[] {
  if (process.platform !== 'win32') return []
  try {
    const out = execFileSync(
      'powershell',
      ['-NoProfile', '-Command',
        "Get-CimInstance Win32_Process -Filter \"Name='youtab-backend.exe'\" | Select-Object -ExpandProperty ExecutablePath"],
      { encoding: 'utf8' },
    )
    return out.split(/\r?\n/).map(s => s.trim()).filter(Boolean)
  } catch {
    return []
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
    // Diagnostic affordance (inert by default): keep the disposable profile on
    // disk for post-mortem inspection of state.db / interrupted_turns.json /
    // logs when investigating a failure. Never set in CI.
    if (sandbox && !process.env.YOUTAB_E2E_KEEP_SANDBOX) sandbox.cleanup()
    else if (sandbox) console.log(`[e2e] kept sandbox: ${sandbox.youtabHome}`)
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

    // Process-path evidence: the live backend was spawned from the PACKAGED
    // resources tree (…/release/win-unpacked/resources/backend-sidecar/…), never
    // a dev checkout venv/source.
    await expect
      .poll(() => backendProcessPaths().some(p => /win-unpacked[\\/]+resources[\\/]+backend-sidecar/i.test(p)), {
        timeout: 60_000,
        message: 'the running backend must be the packaged bundled sidecar',
      })
      .toBe(true)

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
    // Generous: a cold packaged relaunch boots the real bundled backend and
    // then fetches the session list; under sequential-suite load that surfacing
    // can exceed 60s. Durability itself is proven deterministically in state.db
    // before this UI wait, so this only sizes the reopen latency (not a retry).
    await row.waitFor({ state: 'visible', timeout: 180_000 })
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

  // Regression for the acknowledgement boundary found during investigation: a
  // user-visible COMPLETED turn (composer idle again) must be durable the moment
  // the turn is acknowledged — closing promptly WITHOUT waiting on state.db must
  // not lose the turn. This test deliberately does NOT poll the DB before the
  // hard close; if the product loses the turn, the persistence/ack boundary must
  // be fixed in product code, not by waiting here.
  test('completed turn survives a PROMPT close (no durable-poll) then relaunch', async ({}, testInfo) => {
    test.skip(!packagedBinaryExists(), 'requires the packaged binary — run npm run dist:win first')
    test.slow()
    test.setTimeout(600_000)

    mock = await startMockServer()
    sandbox = createSandbox('packaged-attach-promptclose')
    writeMockProviderConfig(sandbox.youtabHome, mock.url, undefined, NATIVE_IMAGE_CONFIG)
    writeEnvFile(sandbox.youtabHome)

    ;({ app } = await launchPackagedAppRealBackend(sandbox))
    const page1 = await app.firstWindow()
    await waitForAppReady({ page: page1, app } as never, 240_000)

    const composer = await focusComposer(page1)
    await composer.type(CAPTION, { delay: 10 })
    await pasteImage(page1)
    await page1.locator('[data-slot="composer-attachments"]').waitFor({ state: 'visible', timeout: 30_000 })
    await page1.keyboard.press('Enter')

    // Wait for the turn to be USER-VISIBLY COMPLETE: the attached image rendered
    // AND the turn is idle again (the "Stop" affordance is gone). This is the
    // moment a user would consider the turn done and could close the app.
    await page1.getByRole('button', { name: /open image/i }).first()
      .waitFor({ state: 'visible', timeout: 180_000 })
    await expect
      .poll(() => page1.getByRole('button', { name: 'Stop' }).count(), {
        timeout: 180_000,
        message: 'turn must reach idle (Stop affordance cleared) before we close',
      })
      .toBe(0)

    // Close PROMPTLY — no state.db poll, no settle wait. If the acknowledgement
    // boundary is correct, the acknowledged turn is already durable.
    await closePackagedApp(app)
    app = null
    await expect
      .poll(() => bundledBackendProcessCount(), { timeout: 20_000 })
      .toBe(0)

    // Relaunch the same installation + profile.
    ;({ app } = await launchPackagedAppRealBackend(sandbox))
    const page2 = await app.firstWindow()
    await waitForAppReady({ page: page2, app } as never, 240_000)

    // PRIMARY (deterministic): the completed turn — caption AND its @image:
    // attachment ref — SURVIVED the prompt close and is durable in the profile
    // it relaunched from. This is the exact "the turn is not lost" guarantee the
    // acknowledgement-boundary fix provides, proven at the durable-store level so
    // it does not depend on any UI render timing.
    expect(
      durableUserTurnRecovered(sandbox.youtabHome),
      'the completed attachment turn (caption + @image: ref) must survive a prompt close (durable in state.db)',
    ).toBe(true)

    // The reopened UI must surface the recovered session: open it and require the
    // caption in the transcript with no [screenshot] placeholder and no literal
    // @image: leak. (The exact attachment render component on a cold reopen — an
    // inline data: thumbnail vs a file-referenced <img> — is a desktop-render
    // concern the durable-poll test's strict thumbnail assertion already covers;
    // this test's subject is prompt-close durability + session recovery.)
    const row = sessionRow(page2)
    // Generous: a cold packaged relaunch boots the real bundled backend and
    // then fetches the session list; under sequential-suite load that surfacing
    // can exceed 60s. Durability itself is proven deterministically in state.db
    // before this UI wait, so this only sizes the reopen latency (not a retry).
    await row.waitFor({ state: 'visible', timeout: 180_000 })
    // ONE normal reopen — a single click. A single click reliably drives
    // session.resume (open-session → route → useRouteResume → resumeSession,
    // which awaits a concurrent prefetch + session.resume before painting); the
    // cold-reopen gap is a visible session loader, not a dropped resume. We wait
    // generously for that in-flight hydration to complete instead of re-clicking:
    // re-clicking would mask reopen LATENCY as success, which the customer-admin
    // path must not rely on.
    await row.click()
    await expect
      .poll(() => transcriptText(page2), { timeout: 120_000, message: 'one reopen must hydrate the recovered turn' })
      .toContain(CAPTION)
    const recoveredText = await transcriptText(page2)
    expect(recoveredText, 'prompt-close relaunch: caption recovered in the UI').toContain(CAPTION)
    expect(recoveredText, 'prompt-close relaunch: no [screenshot] placeholder').not.toContain('[screenshot]')
    expect(recoveredText, 'prompt-close relaunch: no literal @image: leak').not.toContain('@image:')
    await page2.screenshot({ path: testInfo.outputPath('packaged-promptclose-relaunch.png') })
  })

  // FAULT-INJECTED durability across the REAL turn handler + a process relaunch.
  //
  // The fault is injected at the REAL durable-write chokepoint the whole handler
  // routes message writes through (run_agent._flush_messages_to_session_db_unlocked),
  // not the gateway finalizer — faulting only the finalizer would miss the
  // incremental mid-turn flushes that already made the transcript durable. The
  // `assistant` mode models a process death while the turn's COMPLETION is being
  // persisted: the inbound user turn (flushed before the LLM call) stays durable
  // — so the session stays sidebar-visible and RESUMABLE — but the assistant
  // reply never lands and the crash-recovery marker is preserved. On a normal
  // reopen after relaunch, session.resume auto-continues the interrupted turn and
  // commits it exactly once (no duplicate). This is a realistic recoverable
  // crash; a total-persistence failure that loses even the user turn would leave
  // a 0-message session that the sidebar hides (min_message_count=1) — a separate
  // narrow orphan window, not this recoverable path, and NOT claimed here.
  test('completion-commit failure keeps the marker; a normal reopen after relaunch recovers the turn once (no duplicate)', async ({}, testInfo) => {
    test.skip(!packagedBinaryExists(), 'requires the packaged binary — run npm run dist:win first')
    test.slow()
    test.setTimeout(600_000)

    mock = await startMockServer()
    sandbox = createSandbox('packaged-attach-fault')
    writeMockProviderConfig(sandbox.youtabHome, mock.url, undefined, NATIVE_IMAGE_CONFIG)
    writeEnvFile(sandbox.youtabHome)

    // Launch 1 with the completion-commit fault armed: the assistant reply's
    // durable write fails; the earlier user-turn write is untouched.
    ;({ app } = await launchPackagedAppRealBackend(sandbox, {
      YOUTAB_AGENT_TEST_PERSIST_FAULT: 'assistant',
    }))
    const page1 = await app.firstWindow()
    await waitForAppReady({ page: page1, app } as never, 240_000)

    const composer = await focusComposer(page1)
    await composer.type(CAPTION, { delay: 10 })
    await pasteImage(page1)
    await page1.locator('[data-slot="composer-attachments"]').waitFor({ state: 'visible', timeout: 30_000 })
    await page1.keyboard.press('Enter')
    // The turn reaches a terminal frame (the completion commit failed → the turn
    // closes recoverable, Stop affordance cleared).
    await expect
      .poll(() => page1.getByRole('button', { name: 'Stop' }).count(), { timeout: 180_000 })
      .toBe(0)

    // The user turn (caption + @image ref) IS durable — the session is
    // recoverable/visible — but the turn did NOT durably COMPLETE: no assistant
    // reply landed. The on-disk crash marker is the recovery carrier that
    // survives a process death (NOT the in-memory retained turn).
    expect(
      durableUserTurnRecovered(sandbox.youtabHome),
      'the user turn must be durable so the session stays recoverable',
    ).toBe(true)
    expect(
      durableAssistantReplyCount(sandbox.youtabHome),
      'the completion commit failed, so no assistant reply may be durable yet',
    ).toBe(0)
    await expect
      .poll(() => crashMarkerHasEntry(sandbox!.youtabHome), { timeout: 30_000, message: 'crash-recovery marker must be preserved when the completion did not commit' })
      .toBe(true)

    // Prompt close the whole tree; the on-disk marker must survive it.
    await closePackagedApp(app)
    app = null
    await expect.poll(() => bundledBackendProcessCount(), { timeout: 20_000 }).toBe(0)
    expect(
      crashMarkerHasEntry(sandbox.youtabHome),
      'the crash-recovery marker must survive the prompt close',
    ).toBe(true)

    // Relaunch WITHOUT the fault. The session is sidebar-visible (its user turn
    // persisted), so the customer-admin's ONE normal reopen — a single click on
    // the session row — is the whole recovery trigger: it drives session.resume,
    // which auto-continues the interrupted turn from the marker; the re-run now
    // commits the completion.
    ;({ app } = await launchPackagedAppRealBackend(sandbox))
    const page2 = await app.firstWindow()
    await waitForAppReady({ page: page2, app } as never, 240_000)
    const row = sessionRow(page2)
    // Generous: a cold packaged relaunch boots the real bundled backend and
    // then fetches the session list; under sequential-suite load that surfacing
    // can exceed 60s. Durability itself is proven deterministically in state.db
    // before this UI wait, so this only sizes the reopen latency (not a retry).
    await row.waitFor({ state: 'visible', timeout: 180_000 })
    await row.click()

    // Recovery: the re-run durably commits an assistant reply, the caption user
    // turn stays SINGLE (no duplicate row), and the marker is retired once the
    // recovery commit is durable.
    await expect
      .poll(() => durableAssistantReplyCount(sandbox!.youtabHome), {
        timeout: 180_000,
        intervals: [2000, 3000, 5000],
        message: 'a normal reopen must auto-continue + durably commit the recovered turn',
      })
      .toBeGreaterThanOrEqual(1)
    expect(
      durableCaptionTurnCount(sandbox.youtabHome),
      'recovery must not duplicate the user turn',
    ).toBe(1)
    await expect
      .poll(() => crashMarkerHasEntry(sandbox!.youtabHome), { timeout: 60_000, message: 'marker must be retired after a durable recovery commit' })
      .toBe(false)
    // The reopened UI surfaces the recovered turn (requirement: one normal reopen
    // displays the saved turn + attachment).
    await expect
      .poll(() => transcriptText(page2), { timeout: 120_000, message: 'reopened session must display the recovered caption' })
      .toContain(CAPTION)
    await page2.screenshot({ path: testInfo.outputPath('packaged-fault-recovery.png') })
  })
})
