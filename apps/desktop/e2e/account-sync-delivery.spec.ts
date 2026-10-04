import { expect, test } from './test'
import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'

type DeliveryWindow = Window & {
  youtabDesktop: {
    accountSync: { status: (profile: string) => Promise<unknown> }
    updates: {
      setChannel: (channel: string) => Promise<unknown>
      getBranch: () => Promise<{ channel: string }>
    }
  }
}

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend()
})
test.afterAll(async () => { await fixture?.cleanup(); fixture = null })

test('real Desktop boots and sync IPC requires a signed-in account', async () => {
  await waitForAppReady(fixture!, 120_000)
  const result = await fixture!.page.evaluate(async () => {
    try {
      await (window as unknown as DeliveryWindow).youtabDesktop.accountSync.status('default')
      return 'UNEXPECTED_AUTHORIZATION'
    } catch (error) {
      return String(error)
    }
  })
  expect(result).toContain('ACCOUNT_SYNC_SIGN_IN_REQUIRED')
})

test('the same Desktop persists Pilot and Stable selection and rejects invalid channels', async () => {
  const result = await fixture!.page.evaluate(async () => {
    const updates = (window as unknown as DeliveryWindow).youtabDesktop.updates
    await updates.setChannel('pilot')
    const pilot = await updates.getBranch()
    await updates.setChannel('stable')
    const stable = await updates.getBranch()
    let rejected = false
    try { await updates.setChannel('unknown') } catch { rejected = true }
    return { pilot: pilot.channel, stable: stable.channel, rejected }
  })
  expect(result).toEqual({ pilot: 'pilot', stable: 'stable', rejected: true })
})
