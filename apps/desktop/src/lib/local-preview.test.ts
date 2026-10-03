import { expect, it } from 'vitest'

import { localPreviewTarget } from './local-preview'

it('keeps Windows absolute outputs outside the current workspace and emits a valid file URL', () => {
  const target = localPreviewTarget('C:\\Users\\Gaming\\Desktop\\pilot report.md', 'G:\\workspace')
  expect(target?.path).toBe('C:\\Users\\Gaming\\Desktop\\pilot report.md')
  expect(target?.url).toBe('file:///C:/Users/Gaming/Desktop/pilot%20report.md')
})

it('decodes a Windows file URL for the filesystem reader', () => {
  expect(localPreviewTarget('file:///C:/Users/Gaming/Desktop/report.md')?.path).toBe('C:/Users/Gaming/Desktop/report.md')
})

it('resolves relative outputs against the owning session workspace', () => {
  expect(localPreviewTarget('outputs/report.md', '/workspace')?.path).toBe('/workspace/outputs/report.md')
})
