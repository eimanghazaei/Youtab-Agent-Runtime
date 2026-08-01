---
sidebar_position: 1
title: "Run Youtab Agent Runtime with Youtab Portal"
description: "Start-to-finish walkthrough: subscribe, set up, switch models, enable gateway tools, and verify routing"
---

# Run Youtab Agent Runtime with Youtab Portal

This guide walks you through running Youtab Agent Runtime on a [Youtab Portal](https://api.youtab.io) subscription end to end — from signing up to verifying that every tool routes correctly. If you just want the overview of what the Portal is and what's in the subscription, see the [Youtab Portal integration page](/integrations/youtab-portal). This page is the task script.

## Prerequisites

- Youtab Agent Runtime installed ([Quickstart](/getting-started/quickstart))
- A web browser on the machine you're setting up (or SSH port forwarding — see [OAuth over SSH](/guides/oauth-over-ssh))
- About 5 minutes

You do **not** need: an OpenAI key, an Anthropic key, a Firecrawl account, a FAL account, a Browser Use account, or any other per-vendor credential. That's the whole point.

## 1. Get a subscription

Open [api.youtab.io/manage-subscription](https://api.youtab.io/manage-subscription), sign up, and pick a plan.

Already subscribed? Skip to step 2.

## 2. Run the one-shot setup

```bash
youtab setup --portal
```

This single command does five things:

1. Opens your browser to api.youtab.io for OAuth login
2. Stores the refresh token at `~/.youtab-agent-runtime/auth.json`
3. Sets `model.provider: youtab` in `~/.youtab-agent-runtime/config.yaml`
4. Picks a default agentic model (`anthropic/claude-sonnet-4.6` or similar)
5. Turns on the Tool Gateway for web search, image generation, TTS, and browser automation

When it finishes, you're back at your terminal ready to chat.

### What if I'm SSH'd into a server?

OAuth needs a browser, but the loopback callback runs on the machine where Youtab is running. Two options:

```bash
# Option A: SSH port forwarding (preferred)
ssh -N -L 8642:127.0.0.1:8642 user@remote-host    # in a local terminal
youtab setup --portal                              # on the remote, open the printed URL in your local browser

# Option B: device-code login (works from Cloud Shell, Codespaces, EC2 Instance Connect)
youtab auth add youtab --type oauth
# Then re-run `youtab setup --portal` to wire the provider + gateway
```

See [OAuth over SSH / Remote Hosts](/guides/oauth-over-ssh) for the full walkthrough including ProxyJump chains, mosh/tmux, and ControlMaster gotchas.

## 3. Verify it worked

```bash
youtab portal info
```

You should see:

```
  Youtab Portal
  ───────────
  Auth:    ✓ logged in
  Portal:  https://api.youtab.io
  Model:   ✓ using Youtab as inference provider

  Tool Gateway
  ────────────
  Web search & extract  via Youtab Portal
  Image generation      via Youtab Portal
  Text-to-speech        via Youtab Portal
  Browser automation    via Youtab Portal
```

If any line shows something other than "via Youtab Portal" or the auth line says "not logged in", jump to [Troubleshooting](#troubleshooting) below.

## 4. Run your first conversation

```bash
youtab chat
```

Try something that exercises both the model and the Tool Gateway:

```
Hey, search the web for "Youtab Agent Runtime release notes" and summarize the top 3 hits.
```

You should see Youtab call `web_search` (Firecrawl-backed, through the gateway) and respond with a summary. If the search runs and the response makes sense, you're done — the Portal is wired up end to end.

## 5. Pick the model you actually want

`youtab setup --portal` lets you pick a model during setup, but the whole point of the subscription is access to the full catalog — switch any time with `/model` mid-session:

```bash
/model anthropic/claude-sonnet-4.6     # best general-purpose agentic
/model openai/gpt-5.4                  # strong reasoning + tool calling
/model google/gemini-2.5-pro           # huge context window
/model deepseek/deepseek-v3.2          # cost-effective coder
/model anthropic/claude-opus-4.6       # heavyweight for hard problems
```

Or pop the picker to browse:

```bash
/model
```

Pick a different default permanently:

```bash
# in your terminal, outside any session
youtab config set model.default anthropic/claude-sonnet-4.6
```

### Don't pick Youtab-4 for agent work

Youtab-4-70B and Youtab-4-405B are available on the Portal at deep discounts, but they're **chat/reasoning models**, not tool-call-tuned. They will struggle with multi-step agent loops. Use them for conversation/research work through the [subscription proxy](/user-guide/features/subscription-proxy) from non-agent tools. For Youtab Agent Runtime itself, stick to the frontier agentic models above.

The Portal's own [info page](https://api.youtab.io/info) carries this warning too — it's the official Youtab guidance, not just a Youtab-side opinion.

## 6. (Optional) Customize Tool Gateway routing

The gateway is opt-in per tool, not all-or-nothing. If you already have a Browserbase account and want to keep using it while routing web search and image generation through Youtab, that's supported:

```bash
youtab tools
# → Web search       → "Youtab Subscription"     (recommended)
# → Image generation → "Youtab Subscription"     (recommended)
# → Browser          → "Browserbase"           (your existing key)
# → TTS              → "Youtab Subscription"     (recommended)
```

These rows appear in `youtab tools` even before you've logged into Youtab Portal — if you pick "Youtab Subscription" without an active session, Youtab runs the Portal login inline (without changing your inference provider or your other tools).

Verify your mix with:

```bash
youtab portal tools
```

You'll see per-tool routing — `via Youtab Portal` for the ones routed through the subscription, and the partner name (`browserbase`, `firecrawl`, etc.) for the ones using your own keys.

## 7. (Optional) Enable voice mode

Because the Tool Gateway includes OpenAI TTS, [voice mode](/user-guide/features/voice-mode) works without a separate OpenAI key:

```bash
youtab setup tts
# → pick "Youtab Subscription" for TTS
# → pick a speech-to-text backend (local faster-whisper is free, no setup)
```

Then in any messaging-platform session (Telegram, Discord, Signal, etc.), send a voice message and Youtab will transcribe it, respond, and reply with synthesized voice — all on your Portal subscription.

## 8. (Optional) Cron + always-on workflows

The Portal subscription works for [cron jobs](/user-guide/features/cron) and [batch processing](/user-guide/features/batch-processing) the same way it works for interactive chat — the OAuth refresh token is reused automatically. No additional setup; just schedule cron jobs and they'll bill against your subscription.

```bash
youtab cron create "0 9 * * *" \
  "Search the web for top AI news and summarize the 5 most important stories" \
  --name "Daily AI news"
```

The cron job runs unattended, calls the model + web search + summarization all through your Portal subscription.

## Profiles and multi-user setups

If you use [Youtab profiles](/user-guide/profiles) (e.g. a separate config per project), the Portal refresh token is automatically shared across all profiles via a shared token store. Sign in once on any profile, and the rest pick it up automatically.

For team setups where multiple humans share a machine, each human has their own Portal account → each home directory holds its own `~/.youtab-agent-runtime/auth.json` → no token sharing across users. This is the right boundary.

## Troubleshooting

### `youtab portal info` shows "not logged in" after `youtab setup --portal`

The OAuth flow didn't complete. Re-run it:

```bash
youtab portal
```

If your browser doesn't open or the callback fails, you're likely on a remote/headless host — see [OAuth over SSH](/guides/oauth-over-ssh) for the port-forwarding workarounds.

### "Model: currently openrouter" (or some other provider) instead of "using Youtab as inference provider"

Your local config drifted. The OAuth worked but `model.provider` is still pointing at a different provider. Fix:

```bash
youtab config set model.provider youtab
```

Or interactively:

```bash
youtab model
# pick Youtab Portal
```

Re-verify with `youtab portal info`.

### Tool Gateway tools showing partner names instead of "via Youtab Portal"

Per-tool config is overriding the gateway. Run:

```bash
youtab tools
# pick "Youtab Subscription" for any tool you want gateway-routed
```

Some users intentionally mix — e.g. routing web through Youtab but using their own Browserbase key for browser. If that's intentional, leave it alone. If not, this command fixes it.

### "Re-authentication required" mid-session

Your Portal refresh token was invalidated (password change, manual revoke, session expiry). The token is now quarantined locally so Youtab doesn't replay it endlessly. Just log in again:

```bash
youtab auth add youtab
```

The quarantine clears automatically on successful re-login.

### Model I want isn't in the `/model` picker

The Portal catalog draws on OpenRouter's model list (300+) plus models served through proprietary or secondary providers. If a model is missing, try typing the OpenRouter-style slug directly:

```bash
/model anthropic/claude-opus-4.6
/model openai/o1-2025-12-17
```

If a model is genuinely unavailable, [open an issue](https://github.com/eimanghazaei/Youtab-Agent-Runtime/issues) — most gaps are routing config we can update.

### Billing not appearing on my Portal account

`youtab portal info` will tell you whether you're actually routing through the Portal or some other provider. Common causes:

- `model.provider` set to `openrouter`/`anthropic`/etc. instead of `youtab`
- An OAuth refresh failure that fell back to a different configured provider
- Multiple Youtab profiles where you're using the wrong one (check `youtab profile list`)

### Want to revoke and start clean

```bash
youtab auth logout youtab       # wipes the local refresh token
# Then re-run setup or remove the subscription from the Portal web UI
```

## What this gets you, in plain numbers

| Without Portal | With Portal |
|----------------|-------------|
| 1× OpenRouter / Anthropic / OpenAI key in `.env` | 1× OAuth refresh token, no `.env` keys |
| 1× Firecrawl key for web | Web routed through gateway |
| 1× FAL key for image gen | Image gen routed through gateway |
| 1× Browser Use / Browserbase key for browser | Browser routed through gateway |
| 1× OpenAI key for TTS / voice mode | TTS routed through gateway |
| 5 separate dashboards, top-ups, invoices | 1 subscription, 1 invoice |
| Cross-machine: replicate all 5 keys | Cross-machine: re-OAuth once |

That's the deal. If you're using more than two of those backends anyway, the subscription pays for itself.

## See also

- **[Youtab Portal integration page](/integrations/youtab-portal)** — Overview of what's in the subscription
- **[Tool Gateway](/user-guide/features/tool-gateway)** — Full details on every gateway-routed tool
- **[Subscription proxy](/user-guide/features/subscription-proxy)** — Use your Portal subscription from non-Youtab tools
- **[Voice mode](/user-guide/features/voice-mode)** — Set up voice conversations on the Portal subscription
- **[OAuth over SSH](/guides/oauth-over-ssh)** — Remote / headless login patterns
- **[Profiles](/user-guide/profiles)** — Share one Portal login across multiple Youtab configurations
