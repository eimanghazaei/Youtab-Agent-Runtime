/**
 * E2E integration test for the Enterprise + Governance surfaces and the
 * fail-closed Local files panel.
 *
 * Transports are REAL: electron → real `youtab serve` → real gateway →
 * renderer (the `mockBackend` fixture). Only the external LLM vendor is mocked
 * (e2e/mock-server.ts) — Gateway/Runtime/UI are genuine. The Enterprise and
 * Governance panels run against their DETERMINISTIC REFERENCE services, which
 * is surfaced honestly in the UI as `source: reference` and never fabricates a
 * runtime effect id.
 *
 * Scenarios (a–l) map to the task brief. Two app boots only:
 *   • mockBackend — a–j, l
 *   • dead backend (fake boot failure) — k (visible fail-closed state)
 *
 * Prerequisite: `npm run build` (the harness loads built dist/). Run with:
 *   YOUTAB_AGENT_DESKTOP_PYTHON=<python> \
 *     npx playwright test e2e/enterprise-governance.spec.ts --reporter=line
 */

import {
  type DeadBackendFixture,
  type MockBackendFixture,
  setupDeadBackend,
  setupMockBackend,
  waitForAppReady,
  waitForBootFailure
} from './fixtures'
import { allowErrorBanners, expect, type Page, test } from './test'

/** Clear the shared error-banner guard buffer after a test deliberately
 *  rendered an in-panel role="alert" (e.g. the denial failure-state), so the
 *  serial page's later tests start from a clean slate. */
async function clearErrorBannerBuffer(page: Page): Promise<void> {
  await page.evaluate(() => {
    const w = window as unknown as { __ERROR_BANNER_GUARD__?: string[] }

    if (w.__ERROR_BANNER_GUARD__) {
      w.__ERROR_BANNER_GUARD__.length = 0
    }
  })
}

// ─── Boot 1: real mock backend (a–j, l) ─────────────────────────────────────

test.describe.serial('enterprise + governance (real backend, reference services)', () => {
  let fixture: MockBackendFixture | null = null

  test.beforeAll(async () => {
    fixture = await setupMockBackend()
    await waitForAppReady(fixture, 120_000)
  })

  test.afterAll(async () => {
    await fixture?.cleanup()
    fixture = null
  })

  test('a. Enterprise and Governance nav entries are visible', async () => {
    const page = fixture!.page
    await expect(page.getByRole('button', { name: 'Enterprise' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Governance' })).toBeVisible()
  })

  test('b. CRM/ERP/SAP/CAD manifests render with a truthful source: reference', async () => {
    const page = fixture!.page
    await page.getByRole('button', { name: 'Enterprise' }).click()
    await expect(page.getByTestId('enterprise-panel')).toBeVisible()

    for (const domain of ['crm', 'erp', 'sap', 'cad'] as const) {
      await page.getByTestId(`domain-tab-${domain}`).click()
      await expect(page.getByTestId('source-indicator')).toHaveText('source: reference')
      await expect(page.getByTestId('connection-status')).toBeVisible()
      await expect(page.getByTestId('runtime-pending-note')).toBeVisible()
    }
  })

  test('c. Preview does NOT create an effect', async () => {
    const page = fixture!.page
    await page.getByTestId('domain-tab-crm').click()
    await page.getByTestId('preview-button').click()

    await expect(page.getByTestId('preview-panel')).toBeVisible()
    await expect(page.getByTestId('phase-status')).toContainText('preview')
    // No effect/receipt was produced by a preview.
    await expect(page.getByTestId('receipt')).toHaveCount(0)
  })

  test('d. Approval denial prevents execution', async () => {
    // The denial deliberately renders an in-panel role="alert" failure-state;
    // opt out of the notification-toast guard for this expected alert.
    allowErrorBanners()
    const page = fixture!.page
    await page.getByTestId('domain-tab-crm').click()
    // Merge accounts is high-risk and requires approval.
    await page.selectOption('#enterprise-op-select', 'crm.merge_accounts')
    await expect(page.getByTestId('approval-required-badge')).toBeVisible()

    await page.getByTestId('execute-button').click()
    await expect(page.getByTestId('approval-panel')).toBeVisible()

    await page.getByTestId('deny-button').click()
    await expect(page.getByTestId('failure-state')).toBeVisible()
    // Denied → no effect id was ever minted.
    await expect(page.getByTestId('receipt-effect-id')).toHaveText('—')

    // Clear the deliberate alert from the DOM + the shared guard buffer so the
    // later serial tests are not tripped by it.
    await page.getByTestId('failure-retry').click()
    await expect(page.getByTestId('failure-state')).toHaveCount(0)
    await clearErrorBannerBuffer(page)
  })

  test('e. Approval success executes exactly once; j. receipt + reconciliation visible', async () => {
    const page = fixture!.page
    await page.getByTestId('domain-tab-crm').click()
    await page.selectOption('#enterprise-op-select', 'crm.merge_accounts')

    await page.getByTestId('execute-button').click()
    await expect(page.getByTestId('approval-panel')).toBeVisible()

    await page.getByTestId('approve-button').click()

    // Exactly one effect: a non-empty effect id + a visible server receipt +
    // a reconciliation result (scenario j).
    await expect(page.getByTestId('phase-status')).toContainText('reconciled')
    await expect(page.getByTestId('receipt')).toBeVisible()
    await expect(page.getByTestId('reconciliation-ok')).toBeVisible()
    const effectId = await page.getByTestId('receipt-effect-id').textContent()
    expect(effectId?.trim()).not.toBe('—')
    expect(effectId?.trim().length ?? 0).toBeGreaterThan(0)
    // Reference receipt stays truthfully labelled.
    await expect(page.getByTestId('receipt-source')).toHaveText('reference')

    // A second effect is not possible without a NEW approval: re-executing
    // drops back to the approval gate, minting no second effect.
    await page.getByTestId('execute-button').click()
    await expect(page.getByTestId('phase-status')).toContainText('approval_required')
    await expect(page.getByTestId('approval-panel')).toBeVisible()
  })

  test('f–i. Governance adversarial checks are rejected (real reference service)', async () => {
    const page = fixture!.page
    await page.getByRole('button', { name: 'Governance' }).click()
    await expect(page.getByTestId('governance-source-indicator')).toContainText('source: reference')

    const phase = page.getByTestId('governance-phase')

    // f. Replay cannot duplicate the effect.
    await page.getByTestId('gov-replay').click()
    await expect(phase).toHaveAttribute('data-phase', 'replay_rejected')

    // g. Payload modification is rejected.
    await page.getByTestId('gov-tamper-payload').click()
    await expect(phase).toHaveAttribute('data-phase', 'payload_modified')

    // h. Workspace mismatch is rejected.
    await page.getByTestId('gov-foreign-workspace').click()
    await expect(phase).toHaveAttribute('data-phase', 'workspace_mismatch')

    // i. Revoked delegation is rejected.
    await page.getByTestId('gov-revoked-delegation').click()
    await expect(phase).toHaveAttribute('data-phase', 'revoked_delegation')

    // Every adversarial receipt stays truthful — reference, no minted ids.
    await expect(phase).toContainText('source: reference')
    await expect(phase).toContainText('effectId: —')
    await expect(phase).toContainText('receiptId: —')
  })

  test('j. Governance approval → effect → reconciliation is visible end-to-end', async () => {
    const page = fixture!.page
    await page.getByRole('button', { name: 'Governance' }).click()
    const phase = page.getByTestId('governance-phase')

    await page.getByRole('button', { name: /submit request/i }).click()
    await expect(phase).toHaveAttribute('data-phase', 'approval_required')
    await page.getByRole('button', { name: /^approve$/i }).click()
    await expect(phase).toHaveAttribute('data-phase', 'approved')
    await page.getByRole('button', { name: /run effect/i }).click()
    await expect(phase).toHaveAttribute('data-phase', 'effect_complete')
    await page.getByRole('button', { name: /reconcile/i }).click()
    await expect(page.getByTestId('governance-phase-label')).toBeVisible()
    const dataPhase = await phase.getAttribute('data-phase')
    expect(['reconcile_succeeded', 'reconcile_failed']).toContain(dataPhase)

    // No secret / credential / local path leaked into the governance surface.
    const text = (await page.getByTestId('governance-phase').textContent()) ?? ''
    expect(text).not.toMatch(/[a-zA-Z]:\\/)
    expect(text).not.toMatch(/\b(?:sk|pk|ghp)[-_][A-Za-z0-9]{8,}/)
  })

  test('l. Local files controls stay UNAVAILABLE (fail-closed) until real capability', async () => {
    const page = fixture!.page
    const folder = fixture!.sandbox.youtabHome

    // Open a real folder as a project so a workspace exists (the file pane is
    // workspace-gated), through the shipped command palette — no fakes.
    await page.keyboard.press('Control+K')
    await page.getByPlaceholder('Search sessions, views, and actions').fill(folder)
    const openFolder = page.getByRole('option', { name: /open folder as project/i })
    await expect(openFolder).toBeVisible()
    // cmdk owns selection: dispatch the item's own click so onSelect fires.
    const handle = await openFolder.first().elementHandle()
    await handle?.evaluate((el: HTMLElement) => el.click())
    // Reveal the right sidebar (Files pane) where LocalFilesPanel is mounted.
    await expect(page.getByRole('button', { name: 'Show right sidebar' })).toBeVisible()
    await page.getByRole('button', { name: 'Show right sidebar' }).click()

    const panel = page.locator('[data-slot="local-files-panel"]')
    await expect(panel).toBeVisible()

    // Add-files control is present but DISABLED (fail-closed).
    const addBtn = panel.getByRole('button', { name: 'Add files' })
    await expect(addBtn).toBeVisible()
    await expect(addBtn).toBeDisabled()

    // The truthful capability status block renders, unavailable.
    const status = panel.locator('[data-slot="file-ingest-status"]')
    await expect(status).toBeVisible()
    await expect(status).toHaveAttribute('data-capability-state', 'unavailable')
  })
})

// ─── Boot 2: fail-closed backend (k) ────────────────────────────────────────

test.describe('fail-closed backend surfaces a visible error state', () => {
  let dead: DeadBackendFixture | null = null

  test.afterAll(async () => {
    await dead?.cleanup()
    dead = null
  })

  test('k. Missing/unavailable backend shows a visible fail-closed state', async () => {
    // This test deliberately triggers a boot-failure toast — opt out of the
    // error-banner guard so the expected failure state is asserted, not thrown.
    allowErrorBanners()
    dead = await setupDeadBackend({ fakeError: true })
    // A visible terminal failure UI (Retry / Repair / boot-failed) — asserted,
    // not a silent pass.
    await waitForBootFailure(dead.page, 90_000)
    const text = (await dead.page.evaluate(() => document.body.textContent)) ?? ''
    expect(/Retry|Repair|Desktop boot failed|Use local gateway|Connection settings/.test(text)).toBe(true)
  })
})
