import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { FileDownloadButton } from './file-download-button'

vi.mock('@/lib/local-preview', () => ({
  normalizeOrLocalPreviewTarget: vi.fn(async (path: string) => ({ kind: 'file', path, label: 'report.md' }))
}))
vi.mock('@/lib/desktop-fs', () => ({
  desktopFsCacheKey: () => 'local:',
  isDesktopFsRemoteMode: () => false,
  readDesktopFileDataUrl: vi.fn()
}))

afterEach(() => {
  delete (window as unknown as { youtabDesktop?: unknown }).youtabDesktop
})

it('downloads the actual file through the native save dialog on click', async () => {
  const saveFileCopy = vi.fn(async () => true)

  ;(window as unknown as { youtabDesktop?: unknown }).youtabDesktop = { saveFileCopy }
  render(<FileDownloadButton path={'C:\\Users\\Gaming\\Desktop\\report.md'} />)
  expect(saveFileCopy).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Download' }))
  await waitFor(() => expect(saveFileCopy).toHaveBeenCalledWith('C:\\Users\\Gaming\\Desktop\\report.md'))
})
