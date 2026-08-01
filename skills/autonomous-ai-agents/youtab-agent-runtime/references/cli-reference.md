# Youtab CLI Reference

Live sources when anything looks stale: `youtab --help`, `youtab <command> --help`,
https://youtab-agent-runtime.youtab.io/docs/reference/cli-commands

### Global Flags

```
youtab [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
youtab chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
youtab setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
youtab model                Interactive model/provider picker
youtab fallback [add|remove|list]  Fallback provider chain
youtab config [show|edit|get|set|unset|path|env-path|check|migrate]
youtab login / logout       OAuth sign-in / clear stored auth
youtab doctor [--fix]       Check dependencies and config
youtab status [--all]       Component status
```

### Tools & Skills

```
youtab tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

youtab skills list|browse|search QUERY|inspect ID
youtab skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
youtab skills config        Enable/disable skills per platform
youtab skills check|update|uninstall|publish PATH
youtab skills tap add REPO  Add a GitHub repo as a skill source
youtab bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
youtab mcp add NAME (--url or --command) | remove | list | test NAME
youtab mcp catalog | install NAME     Curated catalog install
youtab mcp configure NAME             Toggle tool selection
youtab mcp serve                      Run Youtab as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
youtab gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `youtab photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://youtab-agent-runtime.youtab.io/docs/user-guide/messaging/

### Sessions

```
youtab sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
youtab cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
youtab webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
youtab profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
youtab profile rename A B | alias NAME | export NAME | import FILE
```

### Credentials & Pools

```
youtab auth                 Interactive credential manager
youtab auth add [PROVIDER]  Add OAuth or API-key credential (youtab, openai-codex, qwen-oauth, …)
youtab auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
youtab desktop / gui        Native desktop app
youtab dashboard            Web admin panel + embedded chat (--stop / --status)
youtab proxy                OpenAI-compatible local proxy backed by an OAuth provider
youtab portal               Quick setup / sign in via Youtab Portal
youtab kanban <verb>        Multi-agent work-queue board
youtab project              Named multi-folder workspaces
youtab skin list|use|set    Switch/tweak skins (see references/themes.md)
youtab pets <verb>          Pet mascots (see references/petdex.md)
youtab memory setup|status|off|reset   Memory provider
youtab secrets bitwarden|onepassword   External secret stores
youtab moa                  Mixture-of-Agents slots
youtab hooks / security / backup / import / checkpoints / console
youtab logs [-f] [errors]   View agent/error logs
youtab send                 One-off message through a gateway platform
youtab pairing / plugins / insights / journey / computer-use
youtab acp                  ACP server (IDE integration)
youtab completion bash|zsh|fish
youtab update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `youtab photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `youtab config edit` · [Configuration docs](https://youtab-agent-runtime.youtab.io/docs/user-guide/configuration) |
| Tools / toolsets | `youtab tools list` · [Tools reference](https://youtab-agent-runtime.youtab.io/docs/reference/tools-reference) |
| Skills catalog | `youtab skills browse` · [Skills catalog](https://youtab-agent-runtime.youtab.io/docs/reference/skills-catalog) |
| Provider setup | `youtab model` · [Providers guide](https://youtab-agent-runtime.youtab.io/docs/integrations/providers) |
| Env variables | `youtab config env-path` · [Env vars reference](https://youtab-agent-runtime.youtab.io/docs/reference/environment-variables) |
| Gateway logs | `~/.youtab-agent-runtime/logs/gateway.log` (or `youtab logs`) |
| Sessions | `youtab sessions browse` (reads state.db) |
