import { contextBridge, ipcRenderer, webUtils } from 'electron'

contextBridge.exposeInMainWorld('youtabDesktop', {
  getConnection: profile => ipcRenderer.invoke('youtab:connection', profile),
  revalidateConnection: () => ipcRenderer.invoke('youtab:connection:revalidate'),
  touchBackend: profile => ipcRenderer.invoke('youtab:backend:touch', profile),
  getGatewayWsUrl: profile => ipcRenderer.invoke('youtab:gateway:ws-url', profile),
  openSessionWindow: (sessionId, opts) => ipcRenderer.invoke('youtab:window:openSession', sessionId, opts),
  openWindow: () => ipcRenderer.invoke('youtab:window:openInstance'),
  claimAmbientCue: key => ipcRenderer.invoke('youtab:ambient:claim', key),
  petOverlay: {
    // Main renderer → main process: window lifecycle + drag. `request` is
    // `{ bounds, screen }`; resolves with the screen bounds it actually used.
    open: request => ipcRenderer.invoke('youtab:pet-overlay:open', request),
    close: () => ipcRenderer.invoke('youtab:pet-overlay:close'),
    setBounds: bounds => ipcRenderer.send('youtab:pet-overlay:set-bounds', bounds),
    setIgnoreMouse: ignore => ipcRenderer.send('youtab:pet-overlay:ignore-mouse', ignore),
    // Flip the overlay focusable (and focus it) while the composer needs keys.
    setFocusable: focusable => ipcRenderer.send('youtab:pet-overlay:set-focusable', focusable),
    // Main renderer → overlay (forwarded by main): push the latest pet state.
    pushState: payload => ipcRenderer.send('youtab:pet-overlay:state', payload),
    // Overlay → main renderer (forwarded by main): pop back in / composer submit.
    control: payload => ipcRenderer.send('youtab:pet-overlay:control', payload),
    // Overlay subscribes to state pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('youtab:pet-overlay:state', listener)

      return () => ipcRenderer.removeListener('youtab:pet-overlay:state', listener)
    },
    // Main renderer subscribes to overlay control messages.
    onControl: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('youtab:pet-overlay:control', listener)

      return () => ipcRenderer.removeListener('youtab:pet-overlay:control', listener)
    }
  },
  // Quick Entry: the global-hotkey mini composer window. Main owns the OS
  // shortcut + the persisted preference; the quick window only captures text
  // and hands it back, and the primary renderer submits it through the normal
  // prompt path.
  quickEntry: {
    getSettings: () => ipcRenderer.invoke('youtab:quick-entry:settings:get'),
    setSettings: patch => ipcRenderer.invoke('youtab:quick-entry:settings:set', patch),
    submit: payload => ipcRenderer.send('youtab:quick-entry:submit', payload),
    dismiss: () => ipcRenderer.send('youtab:quick-entry:dismiss'),
    // Primary renderer → main → quick window: gateway connection state + the
    // recent-session options the target picker offers. Main caches the latest
    // payload so a freshly spawned quick window starts from truth.
    pushState: payload => ipcRenderer.send('youtab:quick-entry:state', payload),
    // Quick window subscribes to those pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('youtab:quick-entry:state', listener)

      return () => ipcRenderer.removeListener('youtab:quick-entry:state', listener)
    },
    // Main → primary renderer: a submit captured by the quick window.
    onSubmit: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('youtab:quick-entry:submit', listener)

      return () => ipcRenderer.removeListener('youtab:quick-entry:submit', listener)
    },
    // Main → quick window: you were just summoned (reset draft + refocus).
    onShown: callback => {
      const listener = () => callback()
      ipcRenderer.on('youtab:quick-entry:shown', listener)

      return () => ipcRenderer.removeListener('youtab:quick-entry:shown', listener)
    }
  },
  getBootProgress: () => ipcRenderer.invoke('youtab:boot-progress:get'),
  getConnectionConfig: profile => ipcRenderer.invoke('youtab:connection-config:get', profile),
  saveConnectionConfig: payload => ipcRenderer.invoke('youtab:connection-config:save', payload),
  applyConnectionConfig: payload => ipcRenderer.invoke('youtab:connection-config:apply', payload),
  testConnectionConfig: payload => ipcRenderer.invoke('youtab:connection-config:test', payload),
  sshConfigHosts: () => ipcRenderer.invoke('youtab:ssh-config:hosts'),
  sshResolveHost: host => ipcRenderer.invoke('youtab:ssh-config:resolve', host),
  probeConnectionConfig: remoteUrl => ipcRenderer.invoke('youtab:connection-config:probe', remoteUrl),
  oauthLoginConnectionConfig: (remoteUrl, options) =>
    ipcRenderer.invoke('youtab:connection-config:oauth-login', remoteUrl, options),
  oauthLogoutConnectionConfig: (remoteUrl, options) =>
    ipcRenderer.invoke('youtab:connection-config:oauth-logout', remoteUrl, options),
  // Youtab Cloud: one portal login powers discovery + silent per-agent sign-in
  // (cloud-auto-discovery Phase 3).
  cloud: {
    status: () => ipcRenderer.invoke('youtab:cloud:status'),
    login: () => ipcRenderer.invoke('youtab:cloud:login'),
    logout: () => ipcRenderer.invoke('youtab:cloud:logout'),
    discover: org => ipcRenderer.invoke('youtab:cloud:discover', org),
    agentSignIn: dashboardUrl => ipcRenderer.invoke('youtab:cloud:agent-sign-in', dashboardUrl)
  },
  profile: {
    get: () => ipcRenderer.invoke('youtab:profile:get'),
    set: name => ipcRenderer.invoke('youtab:profile:set', name)
  },
  api: request => ipcRenderer.invoke('youtab:api', request),
  notify: payload => ipcRenderer.invoke('youtab:notify', payload),
  requestMicrophoneAccess: () => ipcRenderer.invoke('youtab:requestMicrophoneAccess'),
  readFileDataUrl: filePath => ipcRenderer.invoke('youtab:readFileDataUrl', filePath),
  readFileDataUrlForAttach: filePath => ipcRenderer.invoke('youtab:readFileDataUrlForAttach', filePath),
  dataUrlReadMax: {
    get: () => ipcRenderer.invoke('youtab:data-url-read-max:get'),
    set: maxMb => ipcRenderer.invoke('youtab:data-url-read-max:set', maxMb)
  },
  readFileText: filePath => ipcRenderer.invoke('youtab:readFileText', filePath),
  selectPaths: options => ipcRenderer.invoke('youtab:selectPaths', options),
  writeClipboard: text => ipcRenderer.invoke('youtab:writeClipboard', text),
  readClipboard: () => ipcRenderer.invoke('youtab:readClipboard'),
  saveImageFromUrl: url => ipcRenderer.invoke('youtab:saveImageFromUrl', url),
  saveFileCopy: filePath => ipcRenderer.invoke('youtab:saveFileCopy', filePath),
  saveImageBuffer: (data, ext) => ipcRenderer.invoke('youtab:saveImageBuffer', { data, ext }),
  saveClipboardImage: () => ipcRenderer.invoke('youtab:saveClipboardImage'),
  getPathForFile: file => {
    try {
      return webUtils.getPathForFile(file) || ''
    } catch {
      return ''
    }
  },
  normalizePreviewTarget: (target, baseDir) => ipcRenderer.invoke('youtab:normalizePreviewTarget', target, baseDir),
  watchPreviewFile: url => ipcRenderer.invoke('youtab:watchPreviewFile', url),
  watchDirectory: dir => ipcRenderer.invoke('youtab:watchDirectory', dir),
  stopPreviewFileWatch: id => ipcRenderer.invoke('youtab:stopPreviewFileWatch', id),
  setActiveWork: payload => ipcRenderer.send('youtab:active-work', payload),
  setTitleBarTheme: payload => ipcRenderer.send('youtab:titlebar-theme', payload),
  setNativeTheme: mode => ipcRenderer.send('youtab:native-theme', mode),
  setTranslucency: payload => ipcRenderer.send('youtab:translucency', payload),
  setKeepAwake: on => ipcRenderer.send('youtab:keep-awake', on),
  setPreviewShortcutActive: active => ipcRenderer.send('youtab:previewShortcutActive', Boolean(active)),
  openExternal: url => ipcRenderer.invoke('youtab:openExternal', url),
  openPreviewInBrowser: url => ipcRenderer.invoke('youtab:openPreviewInBrowser', url),
  fetchLinkTitle: url => ipcRenderer.invoke('youtab:fetchLinkTitle', url),
  sanitizeWorkspaceCwd: cwd => ipcRenderer.invoke('youtab:workspace:sanitize', cwd),
  settings: {
    getDefaultProjectDir: () => ipcRenderer.invoke('youtab:setting:defaultProjectDir:get'),
    setDefaultProjectDir: dir => ipcRenderer.invoke('youtab:setting:defaultProjectDir:set', dir),
    pickDefaultProjectDir: () => ipcRenderer.invoke('youtab:setting:defaultProjectDir:pick')
  },
  zoom: {
    // Current zoom of this window, as { level, percent }.
    get: () => ipcRenderer.invoke('youtab:zoom:get'),
    setPercent: percent => ipcRenderer.send('youtab:zoom:set-percent', percent),
    // Fires on every zoom change, including the Ctrl/Cmd +/-/0 shortcuts,
    // so the settings UI can stay in sync with the keyboard.
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('youtab:zoom:changed', listener)

      return () => ipcRenderer.removeListener('youtab:zoom:changed', listener)
    }
  },
  revealLogs: () => ipcRenderer.invoke('youtab:logs:reveal'),
  getRecentLogs: () => ipcRenderer.invoke('youtab:logs:recent'),
  readDir: dirPath => ipcRenderer.invoke('youtab:fs:readDir', dirPath),
  gitRoot: startPath => ipcRenderer.invoke('youtab:fs:gitRoot', startPath),
  revealPath: targetPath => ipcRenderer.invoke('youtab:fs:reveal', targetPath),
  openDir: dirPath => ipcRenderer.invoke('youtab:fs:openDir', dirPath),
  desktopPluginsRoot: () => ipcRenderer.invoke('youtab:fs:desktopPluginsRoot'),
  renamePath: (targetPath, newName) => ipcRenderer.invoke('youtab:fs:rename', targetPath, newName),
  writeTextFile: (filePath, content) => ipcRenderer.invoke('youtab:fs:writeText', filePath, content),
  trashPath: targetPath => ipcRenderer.invoke('youtab:fs:trash', targetPath),
  git: {
    worktreeList: repoPath => ipcRenderer.invoke('youtab:git:worktreeList', repoPath),
    worktreeAdd: (repoPath, options) => ipcRenderer.invoke('youtab:git:worktreeAdd', repoPath, options),
    worktreeRemove: (repoPath, worktreePath, options) =>
      ipcRenderer.invoke('youtab:git:worktreeRemove', repoPath, worktreePath, options),
    branchSwitch: (repoPath, branch) => ipcRenderer.invoke('youtab:git:branchSwitch', repoPath, branch),
    branchList: repoPath => ipcRenderer.invoke('youtab:git:branchList', repoPath),
    baseBranchList: repoPath => ipcRenderer.invoke('youtab:git:baseBranchList', repoPath),
    repoStatus: repoPath => ipcRenderer.invoke('youtab:git:repoStatus', repoPath),
    fileDiff: (repoPath, filePath) => ipcRenderer.invoke('youtab:git:fileDiff', repoPath, filePath),
    scanRepos: (roots, options) => ipcRenderer.invoke('youtab:git:scanRepos', roots, options),
    review: {
      list: (repoPath, scope, baseRef) => ipcRenderer.invoke('youtab:git:review:list', repoPath, scope, baseRef),
      diff: (repoPath, filePath, scope, baseRef, staged) =>
        ipcRenderer.invoke('youtab:git:review:diff', repoPath, filePath, scope, baseRef, staged),
      stage: (repoPath, filePath) => ipcRenderer.invoke('youtab:git:review:stage', repoPath, filePath),
      unstage: (repoPath, filePath) => ipcRenderer.invoke('youtab:git:review:unstage', repoPath, filePath),
      revert: (repoPath, filePath) => ipcRenderer.invoke('youtab:git:review:revert', repoPath, filePath),
      revParse: (repoPath, ref) => ipcRenderer.invoke('youtab:git:review:revParse', repoPath, ref),
      commit: (repoPath, message, push) => ipcRenderer.invoke('youtab:git:review:commit', repoPath, message, push),
      commitContext: repoPath => ipcRenderer.invoke('youtab:git:review:commitContext', repoPath),
      push: repoPath => ipcRenderer.invoke('youtab:git:review:push', repoPath),
      shipInfo: repoPath => ipcRenderer.invoke('youtab:git:review:shipInfo', repoPath),
      createPr: repoPath => ipcRenderer.invoke('youtab:git:review:createPr', repoPath)
    }
  },
  terminal: {
    cwd: id => ipcRenderer.invoke('youtab:terminal:cwd', id),
    dispose: id => ipcRenderer.invoke('youtab:terminal:dispose', id),
    resize: (id, size) => ipcRenderer.invoke('youtab:terminal:resize', id, size),
    start: options => ipcRenderer.invoke('youtab:terminal:start', options),
    write: (id, data) => ipcRenderer.invoke('youtab:terminal:write', id, data),
    onData: (id, callback) => {
      const channel = `youtab:terminal:${id}:data`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    },
    onExit: (id, callback) => {
      const channel = `youtab:terminal:${id}:exit`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    }
  },
  onClosePreviewRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('youtab:close-preview-requested', listener)

    return () => ipcRenderer.removeListener('youtab:close-preview-requested', listener)
  },
  onOpenFolderRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('youtab:open-folder-requested', listener)

    return () => ipcRenderer.removeListener('youtab:open-folder-requested', listener)
  },
  onOpenUpdatesRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('youtab:open-updates', listener)

    return () => ipcRenderer.removeListener('youtab:open-updates', listener)
  },
  onDeepLink: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('youtab:deep-link', listener)

    return () => ipcRenderer.removeListener('youtab:deep-link', listener)
  },
  signalDeepLinkReady: () => ipcRenderer.invoke('youtab:deep-link-ready'),
  onWindowStateChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('youtab:window-state-changed', listener)

    return () => ipcRenderer.removeListener('youtab:window-state-changed', listener)
  },
  onFocusSession: callback => {
    const listener = (_event, sessionId) => callback(sessionId)
    ipcRenderer.on('youtab:focus-session', listener)

    return () => ipcRenderer.removeListener('youtab:focus-session', listener)
  },
  onNotificationAction: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('youtab:notification-action', listener)

    return () => ipcRenderer.removeListener('youtab:notification-action', listener)
  },
  onPreviewFileChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('youtab:preview-file-changed', listener)

    return () => ipcRenderer.removeListener('youtab:preview-file-changed', listener)
  },
  onBackendExit: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('youtab:backend-exit', listener)

    return () => ipcRenderer.removeListener('youtab:backend-exit', listener)
  },
  // Soft gateway-mode apply finished tearing down the primary backend. Renderer
  // should wipe session lists + re-dial without a window reload.
  onConnectionApplied: callback => {
    const listener = () => callback()
    ipcRenderer.on('youtab:connection:applied', listener)

    return () => ipcRenderer.removeListener('youtab:connection:applied', listener)
  },
  onPowerResume: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('youtab:power-resume', listener)

    return () => ipcRenderer.removeListener('youtab:power-resume', listener)
  },
  onBootProgress: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('youtab:boot-progress', listener)

    return () => ipcRenderer.removeListener('youtab:boot-progress', listener)
  },
  // First-launch bootstrap progress -- emitted by the install.ps1 stage
  // runner in main.ts (apps/desktop/electron/bootstrap-runner.ts).
  // Renderer's install overlay subscribes to live events and queries the
  // current snapshot via getBootstrapState() to recover after a devtools
  // reload mid-bootstrap.
  getBootstrapState: () => ipcRenderer.invoke('youtab:bootstrap:get'),
  continueBootstrapLocal: () => ipcRenderer.invoke('youtab:bootstrap:continue-local'),
  resetBootstrap: () => ipcRenderer.invoke('youtab:bootstrap:reset'),
  repairBootstrap: () => ipcRenderer.invoke('youtab:bootstrap:repair'),
  cancelBootstrap: () => ipcRenderer.invoke('youtab:bootstrap:cancel'),
  onBootstrapEvent: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('youtab:bootstrap:event', listener)

    return () => ipcRenderer.removeListener('youtab:bootstrap:event', listener)
  },
  getVersion: () => ipcRenderer.invoke('youtab:version'),
  getRemoteDisplayReason: () => ipcRenderer.invoke('youtab:get-remote-display-reason'),
  uninstall: {
    summary: () => ipcRenderer.invoke('youtab:uninstall:summary'),
    run: mode => ipcRenderer.invoke('youtab:uninstall:run', { mode })
  },
  updates: {
    check: () => ipcRenderer.invoke('youtab:updates:check'),
    apply: opts => ipcRenderer.invoke('youtab:updates:apply', opts),
    getBranch: () => ipcRenderer.invoke('youtab:updates:branch:get'),
    setBranch: name => ipcRenderer.invoke('youtab:updates:branch:set', name),
    onProgress: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('youtab:updates:progress', listener)

      return () => ipcRenderer.removeListener('youtab:updates:progress', listener)
    }
  },
  themes: {
    fetchMarketplace: id => ipcRenderer.invoke('youtab:vscode-theme:fetch', id),
    searchMarketplace: query => ipcRenderer.invoke('youtab:vscode-theme:search', query)
  },
  // Find-in-page (Ctrl/Cmd+F): delegates to Electron's
  // webContents.findInPage on the IPC sender's window so a Cmd+F pressed
  // in a secondary session window searches THAT window, not the primary.
  // `onFoundInPage` returns the unsubscribe fn; the renderer wires it via
  // `initFindInPageListener` in store/find-in-page.ts and tears it down
  // when the FindBar unmounts.
  findInPage: (query, options) => ipcRenderer.invoke('youtab:find-in-page', query, options),
  stopFindInPage: () => ipcRenderer.invoke('youtab:stop-find-in-page'),
  onFoundInPage: callback => {
    const listener = (_event, result) => callback(result)
    ipcRenderer.on('youtab:found-in-page', listener)

    return () => ipcRenderer.removeListener('youtab:found-in-page', listener)
  }
})
