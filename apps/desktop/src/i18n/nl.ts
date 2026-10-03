import { defineLocale } from './define-locale'

// Untranslated specialist messages use the existing English fallback contract.
export const nl = defineLocale({
  runtimePlugins: {
    title: "Runtime-plug-ins",
    description: "Meegeleverde en geïnstalleerde agentintegraties. Beschikbaarheid hangt af van afhankelijkheden en accountinstellingen.",
    empty: "Geen Runtime-plug-ins gevonden.",
    failed: "Runtime-plug-ins konden niet worden geladen.",
  },
  common: {
    apply: 'Toepassen', back: 'Terug', save: 'Opslaan', saving: 'Opslaan…', cancel: 'Annuleren',
    change: 'Wijzigen', choose: 'Kiezen', clear: 'Wissen', close: 'Sluiten', collapse: 'Inklappen',
    confirm: 'Bevestigen', connect: 'Verbinden', connecting: 'Verbinden', continue: 'Doorgaan',
    copied: 'Gekopieerd', copy: 'Kopiëren', copyFailed: 'Kopiëren mislukt', delete: 'Verwijderen',
    docs: 'Documentatie', done: 'Klaar', error: 'Fout', expand: 'Uitklappen', failed: 'Mislukt',
    formatJson: 'JSON opmaken', free: 'Gratis', loading: 'Laden…', notSet: 'Niet ingesteld',
    refresh: 'Vernieuwen', remove: 'Verwijderen', replace: 'Vervangen', retry: 'Opnieuw proberen',
    run: 'Uitvoeren', send: 'Verzenden', set: 'Instellen', skip: 'Overslaan', update: 'Bijwerken',
    tryHint: term => `Probeer “${term}”`, on: 'Aan', off: 'Uit'
  },
  language: {
    label: 'Taal', description: 'Kies de taal van de desktopinterface.', saving: 'Taal opslaan…',
    saveError: 'De taal kon niet worden opgeslagen.', switchTo: 'Taal wijzigen',
    searchPlaceholder: 'Talen zoeken…', noResults: 'Geen talen gevonden'
  },
  settings: {
    about: {
      heading: 'Youtab Desktop', version: value => `Versie ${value}`, versionUnavailable: 'Versie niet beschikbaar',
      updates: 'Updates', checkNow: 'Nu controleren', checking: 'Controleren…', seeWhatsNew: 'Wat is er nieuw?',
      updateNow: 'Nu bijwerken', releaseNotes: 'Releaseopmerkingen', onLatest: 'Je hebt de nieuwste versie.',
      installing: 'Er wordt een update geïnstalleerd.', cantUpdate: 'Deze versie kan zichzelf niet bijwerken.',
      cantReach: 'De updateserver is niet bereikbaar.', tapCheck: 'Klik op “Nu controleren” om updates te zoeken.',
      updateReady: count => `Een update met ${count} wijzigingen is klaar.`, lastChecked: age => `Laatst gecontroleerd: ${age}`,
      justNowSuffix: ' · zojuist', automaticUpdates: 'Automatische updates',
      automaticUpdatesDesc: 'Youtab controleert op de achtergrond op updates en meldt wanneer er een klaarstaat.',
      branchCommit: (branch, commit) => `Branch ${branch} · Commit ${commit}`, never: 'nooit', justNow: 'zojuist',
      minAgo: count => `${count} min geleden`, hoursAgo: count => `${count} uur geleden`, daysAgo: count => `${count} dagen geleden`
    },
    closeSettings: 'Instellingen sluiten', exportConfig: 'Configuratie exporteren', importConfig: 'Configuratie importeren',
    resetToDefaults: 'Standaardinstellingen herstellen', resetConfirm: 'Alle instellingen herstellen?',
    nav: {
      providers: 'Providers', providerAccounts: 'Accounts', providerApiKeys: 'API-sleutels',
      providerCustomEndpoints: 'Eigen endpoints', gateway: 'Gateway', apiKeys: 'Tools en sleutels',
      keybinds: 'Sneltoetsen', keysTools: 'Tools', keysSettings: 'Instellingen', archivedChats: 'Gearchiveerde chats',
      about: 'Over', billing: 'Facturering', notifications: 'Meldingen', plugins: 'Plug-ins'
    },
    sections: {
      model: 'Model', chat: 'Chat', appearance: 'Weergave', workspace: 'Werkruimte', safety: 'Veiligheid',
      memory: 'Geheugen en context', voice: 'Spraak', advanced: 'Geavanceerd'
    },
    modeOptions: {
      light: { label: 'Licht', description: 'Heldere interface' },
      dark: { label: 'Donker', description: 'Rustige, donkere werkruimte' },
      system: { label: 'Systeem', description: 'Systeemweergave volgen' }
    },
    appearance: {
      title: 'Weergave', intro: 'Modus bepaalt de helderheid; thema bepaalt de kleuren en chatweergave.',
      themeTitle: 'Thema', themeDesc: 'Kleuren voor de desktop; de gekozen modus wordt erop toegepast.',
      colorMode: 'Kleurmodus', colorModeDesc: 'Kies een modus of volg de systeeminstelling.',
      toolViewTitle: 'Toolweergave', toolViewDesc: 'Technisch toont de volledige invoer en uitvoer.',
      uiScaleTitle: 'Interfaceschaal', uiScaleDesc: percent => `Schaalt tekst en bediening. Huidig: ${percent}%.`,
      translucencyTitle: 'Venstertransparantie', translucencyDesc: 'Toon het bureaublad door het venster.',
      backdropTitle: 'Chatachtergrond', backdropDesc: 'De subtiele afbeelding achter het gesprek.',
      reactionsTitle: 'Berichtreacties', reactionsDesc: 'Reageer met emoji op berichten.'
    }
  },
  sidebar: {
    nav: { 'new-session': 'Nieuwe sessie', skills: 'Verbindingen', messaging: 'Berichten', artifacts: 'Artefacten' },
    searchAria: 'Sessies zoeken', searchPlaceholder: 'Sessies zoeken…', clearSearch: 'Zoekopdracht wissen',
    pinned: 'Vastgezet', sessions: 'Sessies', results: 'Resultaten', cronJobs: 'Geplande taken'
  },
  notifications: {
    native: { approvalTitle: 'Goedkeuring nodig', approveAction: 'Goedkeuren', rejectAction: 'Afwijzen',
      inputTitle: 'Antwoord nodig', inputBody: 'Youtab wacht op je antwoord.' }
  },
  shell: {
    approvalMode: { title: 'Goedkeuringsmodus', manual: 'Handmatig', manualDescription: 'Vraag toestemming voor acties.',
      smart: 'Slim', off: 'Uit', offDescription: 'Uitvoeren zonder goedkeuringsvragen' }
  },
  assistant: {
    clarify: { notReady: 'De vraag is nog niet klaar', gatewayDisconnected: 'Gateway is niet verbonden',
      sendFailed: 'Antwoord verzenden mislukt', loadingQuestion: 'Vraag laden…',
      other: 'Anders (typ je antwoord)', placeholder: 'Typ je antwoord…', skip: 'Overslaan', skipped: 'Overgeslagen',
      continueLabel: 'Doorgaan', lateAnswer: (question, choice) => `Mijn antwoord op “${question}”: ${choice}`,
      lateAnswerTip: 'Dit antwoord als vervolgbericht opstellen', lateAnswerHint: 'Deze vraag wacht niet meer. Stel je antwoord op als vervolgbericht.' },
    approval: { run: 'Uitvoeren', reject: 'Afwijzen', command: 'Opdracht', moreOptions: 'Meer opties',
      allowSession: 'Toestaan voor deze sessie', alwaysAllow: 'Altijd toestaan', alwaysAllowMenu: 'Altijd toestaan…' }
  }
})
