import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import { desktopFsCacheKey, isDesktopFsRemoteMode, readDesktopFileDataUrl } from '@/lib/desktop-fs'
import { normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { notifyError } from '@/store/notifications'

export function FileDownloadButton({ path, cwd }: { path: string; cwd?: string | null }) {
  const { t } = useI18n()
  const [saving, setSaving] = useState(false)

  const download = async () => {
    if (saving) {
      return
    }
    setSaving(true)
    const connectionKey = desktopFsCacheKey()

    try {
      const target = await normalizeOrLocalPreviewTarget(path, cwd)

      if (!target || target.kind !== 'file' || !target.path) {
        throw new Error(t.preview.unavailable)
      }

      if (desktopFsCacheKey() !== connectionKey) {
        return
      }

      if (!isDesktopFsRemoteMode() && window.youtabDesktop?.saveFileCopy) {
        await window.youtabDesktop.saveFileCopy(target.path)

        return
      }

      const dataUrl = await readDesktopFileDataUrl(target.path)

      if (desktopFsCacheKey() !== connectionKey) {
        return
      }

      if (!dataUrl.startsWith('data:')) {
        throw new Error(t.preview.unavailable)
      }
      const link = document.createElement('a')
      link.href = dataUrl
      link.download = target.label
      link.rel = 'noopener noreferrer'
      document.body.appendChild(link)
      link.click()
      link.remove()
    } catch (error) {
      notifyError(error, t.preview.unavailable)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Button
      aria-label={t.artifactPreview.download}
      disabled={saving}
      onClick={() => void download()}
      size="icon-xs"
      variant="ghost"
    >
      <Codicon name="cloud-download" />
    </Button>
  )
}
