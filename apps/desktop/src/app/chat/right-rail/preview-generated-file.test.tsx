import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { LocalFilePreview } from './preview-file'

vi.mock('@/lib/desktop-fs', () => ({
  desktopFileDiff: vi.fn(async () => ''),
  desktopGitRoot: vi.fn(async () => null),
  readDesktopFileDataUrl: vi.fn(),
  readDesktopFileText: vi.fn(async () => ({ text: '# Pilot report\n\nGenerated file contents.', language: 'markdown', byteSize: 47, binary: false })),
  writeDesktopFileText: vi.fn()
}))
afterEach(cleanup)

it('renders the actual Markdown file contents even when the output directory is not a Git repo', async () => {
  const path = 'C:\\Users\\Gaming\\Desktop\\report.md'
  render(<LocalFilePreview reloadKey={0} target={{ kind: 'file', path, label: 'report.md', language: 'markdown', previewKind: 'text', source: path, url: 'file:///C:/Users/Gaming/Desktop/report.md' }} />)
  expect(await screen.findByRole('heading', { name: 'Pilot report' })).toBeTruthy()
  expect(screen.getByText('Generated file contents.')).toBeTruthy()
})
