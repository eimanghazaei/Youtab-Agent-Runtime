import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { useI18n } from '@/i18n'
import { desktopFsCacheKey } from '@/lib/desktop-fs'
import { Package } from '@/lib/icons'
import { notifyError } from '@/store/notifications'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $gatewayState } from '@/store/session'

import { EmptyState, ListRow, Pill, SectionHeading } from './primitives'

interface RuntimePlugin {
  name: string
  version: string
  description: string
  source: string
  status: string
}

/** Runtime plugins have a backend registry; Desktop UI extensions have a
 * different one. Show both without loading Python plugins in the renderer. */
export function RuntimePluginsSettings() {
  const { t } = useI18n()
  const profile = useStore($activeGatewayProfile)
  const connection = useStore($connection)
  const gatewayState = useStore($gatewayState)
  const { gateway } = useGatewayRequest()
  const [busy, setBusy] = useState<string | null>(null)
  const scope = `${profile}:${connection?.mode}:${desktopFsCacheKey()}`

  const query = useQuery({
    queryKey: ['runtime-plugins', scope],
    enabled: gatewayState === 'open' && Boolean(gateway),
    queryFn: () => {
      if (!gateway) {throw new Error(t.runtimePlugins.failed)}

      return gateway.request<{ plugins: RuntimePlugin[] }>('plugins.manage', { action: 'list' })
    }
  })

  const toggle = async (name: string, enable: boolean) => {
    if (!gateway) {return}
    setBusy(name)

    try {
      await gateway.request('plugins.manage', { action: 'toggle', name, enable })
      await query.refetch()
    } catch (error) {
      notifyError(error, t.runtimePlugins.title)
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="mb-6">
      <SectionHeading icon={Package} meta={t.settings.plugins.count(query.data?.plugins.length ?? 0)} title={t.runtimePlugins.title} />
      <p className="mb-4 text-[length:var(--conversation-caption-font-size)] text-(--ui-text-tertiary)">{t.runtimePlugins.description}</p>
      {gatewayState !== 'open' || query.isPending ? <PageLoader /> : query.isError ? (
        <div role="alert">
          <p>{t.runtimePlugins.failed}</p>
          <Button onClick={() => void query.refetch()} size="sm" variant="outline">{t.common.retry}</Button>
        </div>
      ) : query.data?.plugins.length ? (
        <div className="divide-y divide-(--ui-stroke-tertiary)">
          {query.data.plugins.map(plugin => (
            <ListRow action={<Switch aria-label={`${t.settings.plugins.enable} ${plugin.name}`} checked={plugin.status === 'enabled'} disabled={busy !== null}
                onCheckedChange={enabled => void toggle(plugin.name, enabled)} />} description={plugin.description}
              key={`${plugin.source}:${plugin.name}`}
              title={<span>{plugin.name} <Pill>{plugin.version || plugin.source}</Pill></span>}
            />
          ))}
        </div>
      ) : <EmptyState title={t.runtimePlugins.empty} />}
    </section>
  )
}
