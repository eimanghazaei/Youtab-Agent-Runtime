import { type CSSProperties } from 'react'
import { useStore } from '@nanostores/react'

import { HackeryButton } from '../components/hackery-button'
import { $installationKind, $releaseChannel, openLogDir, startInstall, startUpdate } from '../store'

/*
 * Welcome screen.
 *
 * Mirrors the desktop's chat intro (apps/desktop/src/components/chat/intro.tsx):
 *   - YOUTAB AGENT wordmark rendered in Collapse Bold, uppercase, tracked
 *   - mix-blend-plus-lighter so the type "glows" on the canvas
 *   - fit-text utility so the wordmark sizes itself to the column
 *
 * No install-path footer. The default install location is correct for
 * 99% of users; the rest will use the CLI installer with a -YoutabHome
 * flag. Showing %LOCALAPPDATA% to grandma is developer-brain.
 */
export default function Welcome() {
  const kind = useStore($installationKind)
  const channel = useStore($releaseChannel)
  return (
    <div className="youtab-fade-in flex h-full flex-col items-center justify-center gap-10 px-12 py-10">
      {/* Hero — same recipe the desktop's chat/intro.tsx uses */}
      <div className="w-full max-w-2xl min-w-0 text-center">
        <p
          className="fit-text youtab-display-title mx-auto mb-4 w-full uppercase leading-[0.9] tracking-[0.08em] text-midground mix-blend-plus-lighter dark:text-foreground/90"
          style={
            {
              '--fit-text-line-height': '0.9',
              '--fit-text-max': '6rem',
              '--fit-text-min': '2.5rem'
            } as CSSProperties
          }
        >
          <span>
            <span>YOUTAB AGENT</span>
          </span>
          <span aria-hidden="true">YOUTAB AGENT</span>
        </p>

        <p className="m-0 text-center text-base leading-normal tracking-tight text-muted-foreground">
          {kind === 'legacy' ? 'Upgrade your existing Youtab installation to the common updater. Your chats, settings and credentials are preserved.'
            : kind === 'unknown' ? 'Checking your installation. If this does not finish, open the logs before continuing.'
            : 'The agent that grows with you. Setup takes a few minutes.'}
        </p>
      </div>

      {kind !== 'unknown' && <label className="flex items-center gap-3 text-sm">Update channel
        <select aria-label="Update channel" value={channel} onChange={event => $releaseChannel.set(event.target.value === 'pilot' ? 'pilot' : 'stable')}>
          <option value="stable">Stable</option><option value="pilot">Pilot / Test</option>
        </select>
      </label>}
      {kind !== 'unknown' && <HackeryButton label={kind === 'fresh' ? 'Install' : 'Upgrade'} onClick={() => {
        if (kind === 'artifact') {void startUpdate()} else {void startInstall({ migrateLegacy: kind === 'legacy' })}
      }} />}
      {kind === 'unknown' && <HackeryButton label="Open logs" onClick={() => void openLogDir()} />}
    </div>
  )
}
