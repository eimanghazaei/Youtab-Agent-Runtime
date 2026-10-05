import { useStore } from '@nanostores/react'
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { DesktopAccountSyncState } from '@/global'
import { useI18n } from '@/i18n'
import { $activeGatewayProfile } from '@/store/profile'
import { useTheme } from '@/themes/context'
import type { PaginatedSessions } from '@/types/youtab'

import { openSession } from '../open-session'

import { accountSyncCopy } from './account-sync-copy'
import { ToggleRow } from './primitives'

export function AccountSyncSettings() {
  const navigate = useNavigate()
  const { locale, setLocale } = useI18n()
  const { mode, setMode } = useTheme()
  const copy = accountSyncCopy(locale)
  const profile = useStore($activeGatewayProfile)
  const [state, setState] = useState<DesktopAccountSyncState | null>(null)
  const [local, setLocal] = useState<{ id: string; title: string | null }[]>([])
  const [selected, setSelected] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [sessionEpoch, setSessionEpoch] = useState(0)
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)
  const generation = useRef(0)
  useEffect(() => {
    const expected = ++generation.current

    const invalidate = () => { generation.current++ }
    setState(null); setLocal([]); setSelected(''); setPendingDelete(null); setBusy(false); setError(false); setLoading(true)
    const api = window.youtabDesktop?.accountSync

    if (!api) {setLoading(false);

 return}

    const refresh = async () => {
      try {
        const result = await api.status(profile)

        if (expected !== generation.current) {return}
        setState(result)
        setLoading(false)
        const sessions = await window.youtabDesktop.api<PaginatedSessions>({ path: '/api/sessions?limit=100&min_messages=1', profile }).catch(() => null)

        if (sessions && expected === generation.current) {setLocal(sessions.sessions.map(v => ({ id: v.id, title: v.title })))}
      } catch {
        if (expected === generation.current) {setState(null); setLocal([]); setLoading(false)}
      }
    }

    void refresh()
    const interval = setInterval(() => void refresh(), 30000)

    const unsubscribe = api.onSessionChanged(() => {
      invalidate(); setState(null); setLocal([]); setSelected(''); setPendingDelete(null)
      setSessionEpoch(value => value + 1)
    })

    return () => {invalidate(); clearInterval(interval); unsubscribe()}
  }, [profile, sessionEpoch])

  const run = async (action: () => Promise<DesktopAccountSyncState>) => {
    const expected = generation.current
    setBusy(true); setError(false)

    try {
      const result = await action()

      if (expected === generation.current) {setState(result)}
    } catch {if (expected === generation.current) {setError(true)}}
    finally {if (expected === generation.current) {setBusy(false)}}
  }

  return (
    <section className="mt-6 grid gap-3">
      <h3 className="font-semibold">{copy.title}</h3>
      {!state ? <div><p className="text-sm text-muted-foreground" role="status">{loading ? copy.loading : copy.unavailable}</p>
        {!loading && <Button onClick={() => setSessionEpoch(value => value + 1)} variant="text">{copy.retry}</Button>}</div> : <>
        <ToggleRow checked={state.enabled} description={copy.description} disabled={busy} label={copy.consent}
          onChange={enabled => void run(async () => window.youtabDesktop.accountSync.consent(profile, enabled))} />
        {state.enabled && <>
          {state.discoveryLimited && <p className="text-sm" role="status">{copy.discoveryLimited}</p>}
          {Object.keys(state.migrationFailures ?? {}).length > 0 && <div className="text-sm text-destructive" role="alert"><p>{copy.incomplete}</p>
            <ul>{Object.keys(state.migrationFailures ?? {}).map(id => <li key={id}>{local.find(chat => chat.id === id)?.title || id}</li>)}</ul>
          </div>}
          <div className="flex gap-2"><Button disabled={busy} onClick={() => void run(() => window.youtabDesktop.accountSync.run(profile))}>{copy.sync}</Button>
            <span className="text-sm">{copy.pending}: {state.pending}</span></div>
          <label className="grid gap-2 text-sm">{copy.local}
            <Select disabled={busy} onValueChange={setSelected} value={selected}>
              <SelectTrigger aria-label={copy.local}><SelectValue placeholder="—" /></SelectTrigger>
              <SelectContent>{local.map(chat => <SelectItem key={chat.id} value={chat.id}>{chat.title || chat.id}</SelectItem>)}</SelectContent>
            </Select>
          </label>
          <Button disabled={busy || !selected} onClick={() => void run(() => window.youtabDesktop.accountSync.share(profile, selected))}>{copy.share}</Button>
          <h4 className="font-medium">{copy.cloud}</h4>
          <div className="flex flex-wrap gap-2">
            <Button disabled={busy} onClick={() => void run(() => window.youtabDesktop.accountSync.preferences(profile, { language: locale, appearance: mode }))} variant="text">{copy.savePreferences}</Button>
            <Button disabled={busy || !state.preferences?.length} onClick={() => {
              const expected = generation.current
              const preferences = state.preferences[0]
              setBusy(true); setError(false)
              void (async () => {
                if (preferences.language) {await setLocale(preferences.language, profile ?? 'default', () => expected === generation.current)}

                if (expected === generation.current && preferences.appearance) {setMode(preferences.appearance)}
              })().catch(() => {if (expected === generation.current) {setError(true)}})
                .finally(() => {if (expected === generation.current) {setBusy(false)}})
            }} variant="text">{copy.applyPreferences}</Button>
          </div>
          {!state.chats.length && <p className="text-sm text-muted-foreground">{copy.empty}</p>}
          {state.chats.map(chat => <details className="py-3" key={chat.id}>
            <summary>{chat.title || chat.id}{chat.started_at !== undefined && <time className="ml-2 text-xs text-muted-foreground" dateTime={new Date(chat.started_at * 1000).toISOString()}>{new Date(chat.started_at * 1000).toLocaleString(locale)}</time>}</summary>
            <div className="mt-3 max-h-80 space-y-3 overflow-auto">
              {chat.incomplete && <p role="alert">{copy.incomplete}</p>}
              {chat.messages.map((message, index) => <p className="whitespace-pre-wrap text-sm" dir="auto" key={index}>{message.content}</p>)}
            </div>
            <Button className="mt-3" disabled={busy || chat.incomplete} onClick={() => {
              const expected = generation.current
              setBusy(true); setError(false)
              void window.youtabDesktop.accountSync.continueChat(profile, chat.id).then(result => {
                if (expected === generation.current) {openSession(result.localId, navigate)}
              }).catch(() => {if (expected === generation.current) {setError(true)}})
                .finally(() => {if (expected === generation.current) {setBusy(false)}})
            }}>{copy.continueChat}</Button>
            {pendingDelete === chat.id ? <div className="mt-3 grid gap-2">
              <p className="text-sm">{copy.confirm}</p>
              <div className="flex gap-2"><Button disabled={busy} onClick={() => {
                void run(() => window.youtabDesktop.accountSync.remove(profile, chat.id)).then(() => setPendingDelete(null))
              }} variant="destructive">{copy.remove}</Button><Button disabled={busy} onClick={() => setPendingDelete(null)} variant="text">{copy.cancel}</Button></div>
            </div> : <Button className="mt-3" disabled={busy} onClick={() => setPendingDelete(chat.id)} variant="text">{copy.remove}</Button>}
          </details>)}
        </>}
      </>}
      {error && <p className="text-sm text-destructive" role="alert">{copy.error}</p>}
    </section>
  )
}
