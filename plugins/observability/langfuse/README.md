# Langfuse Observability Plugin

This plugin ships bundled with Youtab but is **opt-in** — it only loads when
you explicitly enable it.

## Enable

Pick one:

```bash
# Interactive: walks you through credentials + SDK install + enable
youtab tools  # → Langfuse Observability

# Manual
pip install langfuse
youtab plugins enable observability/langfuse
```

## Required credentials

Set these in `~/.youtab-agent-runtime/.env` (or via `youtab tools`):

```bash
YOUTAB_AGENT_LANGFUSE_PUBLIC_KEY=pk-lf-...
YOUTAB_AGENT_LANGFUSE_SECRET_KEY=sk-lf-...
YOUTAB_AGENT_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or your self-hosted URL
```

Without the SDK or credentials the hooks no-op silently — the plugin fails
open.

## Verify

```bash
youtab plugins list                 # observability/langfuse should show "enabled"
youtab chat -q "hello"              # then check Langfuse for a "Youtab turn" trace
```

## Optional tuning

```bash
YOUTAB_AGENT_LANGFUSE_ENV=production       # environment tag
YOUTAB_AGENT_LANGFUSE_RELEASE=v1.0.0       # release tag
YOUTAB_AGENT_LANGFUSE_SAMPLE_RATE=0.5      # sample 50% of traces
YOUTAB_AGENT_LANGFUSE_MAX_CHARS=12000      # max chars per field (default: 12000)
YOUTAB_AGENT_LANGFUSE_DEBUG=true           # verbose plugin logging
```

## Disable

```bash
youtab plugins disable observability/langfuse
```
