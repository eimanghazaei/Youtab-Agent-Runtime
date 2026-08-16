---
sidebar_position: 1
title: "Youtab Portal"
description: "One subscription, 300+ frontier models, and the Tool Gateway — the recommended way to run Youtab Agent Runtime"
---

# Youtab Portal

[Youtab Portal](https://api.youtab.io) is Youtab B.V.'s unified subscription gateway and **the recommended way to run Youtab Agent Runtime**. One OAuth login replaces the juggling act of separate accounts, API keys, and billing relationships across every model lab, search API, image generator, and browser provider you'd otherwise need to wire up by hand.

If you only have time to set up one thing, set up this. The fastest path:

```bash
youtab setup --portal
```

That single command runs the Portal OAuth, lets you pick a Youtab model, sets Youtab as your inference provider in `config.yaml`, and turns on the Tool Gateway. You're ready to `youtab chat` immediately after.

Don't have a subscription yet? [api.youtab.io/manage-subscription](https://api.youtab.io/manage-subscription) — sign up, then come back and run the command above.

## What's in the subscription

### 300+ frontier models, one bill

The Portal proxies a curated catalog of agentic models from across the ecosystem — billed against your Youtab subscription instead of one credit balance per lab.

| Family | Models |
|--------|--------|
| **Anthropic Claude** | Opus 4.7, Opus 4.6, Sonnet 4.6, Haiku 4.5 |
| **OpenAI** | GPT-5.5, GPT-5.5 Pro, GPT-5.4 Mini, GPT-5.4 Nano, GPT-5.3 Codex |
| **Google Gemini** | Gemini 3 Pro Preview, Gemini 3 Flash Preview, Gemini 3.1 Pro Preview, Gemini 3.1 Flash Lite Preview |
| **DeepSeek** | DeepSeek V4 Pro |
| **Qwen** | Qwen3.7-Max, Qwen3.6-35B-A3B |
| **Kimi / Moonshot** | Kimi K2.6 |
| **GLM / Zhipu** | GLM-5.1 |
| **MiniMax** | MiniMax M2.7 |
| **xAI** | Grok 4.3 |
| **NVIDIA** | Nemotron-3 Super 120B-A12B |
| **Tencent** | Hunyuan 3 Preview |
| **Xiaomi** | MiMo V2.5 Pro |
| **StepFun** | Step 3.5 Flash |
| **Nous Research** | Hermes-4-70B, Hermes-4-405B (chat, see [note below](#a-note-on-hermes-4)) |
| **+ everything else** | 280+ additional models — the full agentic frontier |

Under the hood, the Portal routes each model to the backend best suited for it — some models go through OpenRouter, others through proprietary or secondary providers, and the routing for a given model can change over time. Everything is billed against your Youtab subscription either way. Switch between Claude Sonnet 4.6 for code and Gemini 3 Pro for long context with `/model` mid-session — no new credentials, no top-ups, no surprise zero-balance errors.

:::note
Because routing is per-model and not always through OpenRouter, OpenRouter-specific request extensions (such as `provider` routing preferences, `session_id` sticky routing, or top-level `cache_control`) are not part of the Portal's API contract and may be ignored depending on which backend serves the model.
:::

### The Youtab Tool Gateway

The same subscription unlocks the [Tool Gateway](/user-guide/features/tool-gateway), which routes Youtab Agent Runtime's tool calls through Youtab-managed infrastructure. Five backends, one login:

| Tool | Partner | What it does |
|------|---------|--------------|
| **Web search & extract** | Firecrawl | Agent-grade search and full-page extraction. No Firecrawl API key, no rate limit babysitting. |
| **Image generation** | FAL | Nine models under one endpoint: FLUX 2 Klein 9B, FLUX 2 Pro, Z-Image Turbo, Nano Banana Pro (Gemini 3 Pro Image), GPT Image 1.5, GPT Image 2, Ideogram V3, Recraft V4 Pro, Qwen Image. |
| **Text-to-speech** | OpenAI TTS | High-quality TTS without a separate OpenAI key. Enables [voice mode](/user-guide/features/voice-mode) across messaging platforms. |
| **Cloud browser automation** | Browser Use | Headless Chromium sessions for `browser_navigate`, `browser_click`, `browser_type`, `browser_vision`. No Browserbase account needed. |
| **Cloud terminal sandbox** | Modal | Serverless terminal sandboxes for code execution (optional add-on). |

Without the gateway, hooking each of those up means a Firecrawl account, a FAL account, a Browser Use account, an OpenAI key, and a Modal account — five separate signups, five separate dashboards, five separate top-up flows. With the gateway, all of it routes through one subscription.

You can also enable just specific gateway tools (e.g. web search but not image generation) — see [Mixing the gateway with your own backends](#mixing-the-gateway-with-your-own-backends) below.

### No credentials in your dotfiles

Because everything routes through one OAuth-authenticated Portal session, you don't accumulate a `.env` file with a dozen long-lived API keys. The refresh token at `~/.youtab-agent-runtime/auth.json` is the only credential on disk, and Youtab mints short-lived JWTs from it per request — see [Token handling](#token-handling) below.

### Cross-platform parity

[Native Windows](/user-guide/windows-native) makes per-tool API key setup its rough edge — installing a Firecrawl account, a FAL account, a Browser Use account, an OpenAI key from Windows is the highest-friction part of getting a useful agent. A Portal subscription smooths that out: one OAuth covers the model and every gateway tool, so Windows users get the same experience as macOS/Linux without manually configuring four backends.

## A note on Hermes 4

Nous Research's **Hermes 4** family (Hermes-4-70B, Hermes-4-405B) is resold through the Portal at heavily discounted rates. These are third-party **hybrid-reasoning chat models** — strong at math, science, instruction following, schema adherence, roleplay, and long-form writing. They are not Youtab models; Youtab B.V. neither trains nor owns them.

They are **not recommended for use inside Youtab Agent Runtime**, however. Hermes 4 is tuned for chat and reasoning, not the rapid-fire tool-calling loop the agent relies on. Use them for research workflows or via the [subscription proxy](/user-guide/features/subscription-proxy) from other tooling — but for agent work, pick a frontier agentic model from the catalog instead:

```bash
/model anthropic/claude-sonnet-4.6     # best general-purpose agentic model
/model openai/gpt-5.5-pro              # strong reasoning + tool calling
/model google/gemini-3-pro-preview     # huge context window
/model deepseek/deepseek-v4-pro        # cost-effective coder
```

The Portal's own [model info page](https://api.youtab.io/info) carries the same warning, so this is not a runtime-side opinion — it matches the Portal's published guidance.

## Setup

### Fresh install — one command

```bash
youtab setup --portal
```

This runs the full setup in one shot:

1. Opens your browser to api.youtab.io for OAuth login
2. Stores the refresh token at `~/.youtab-agent-runtime/auth.json`
3. Lets you pick a Youtab model from the curated list (or skip to keep your current one)
4. Sets Youtab as your inference provider in `~/.youtab-agent-runtime/config.yaml` (when you pick a model)
5. Turns on the Tool Gateway (web, image, TTS, browser routing)
6. Returns you to your terminal ready to `youtab chat`

If you don't have a subscription yet, sign up at [api.youtab.io/manage-subscription](https://api.youtab.io/manage-subscription) first.

### Existing install — add Portal alongside other providers

If you already have Youtab configured with OpenRouter, Anthropic, or any other provider and you want to add the Portal alongside them:

```bash
youtab model
# pick "Youtab Portal" from the provider list
# browser opens, sign in, done
```

Your existing providers stay configured. You can switch between them with `/model` mid-session or `youtab model` between sessions — the Portal becomes one of your available providers, not your only one.

### Headless / SSH / remote setup

OAuth needs a browser, but the loopback callback runs on the machine where Youtab is running. For remote hosts, see [OAuth over SSH / Remote Hosts](/guides/oauth-over-ssh) — the same patterns work for the Portal as for any other OAuth-based provider (`ssh -L` port forwarding).

### Profile setup

If you use [Youtab profiles](/user-guide/profiles), the Portal refresh token is automatically shared across all profiles via a shared token store. Sign in once on any profile, and the rest pick it up automatically — no need to repeat the OAuth flow per profile.

## Using the Portal day-to-day

### Inspecting what's wired up

```bash
youtab portal            # log in to Youtab Portal + set it up (one-shot onboarding)
youtab portal info       # login status, subscription info, model + gateway routing
youtab portal status     # alias for `portal info`
youtab portal tools      # detailed Tool Gateway catalog with per-tool routing
youtab portal open       # open the subscription management page in your browser
```

`youtab portal` (with no subcommand) is the human-readable alias for `youtab auth add youtab --type oauth` — it logs you in, lets you pick a Youtab model, sets Youtab as your inference provider, and offers the Tool Gateway opt-in (identical to `youtab setup --portal`, and the same Youtab flow as the first-time quick setup).

`youtab portal info` gives you the high-level overview:

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
  Cloud terminal        not configured
```

### Switching models

Inside a session:

```bash
/model anthropic/claude-sonnet-4.6
/model openai/gpt-5.5-pro
/model google/gemini-3-pro-preview
```

Or open the picker:

```bash
/model
# arrow keys, enter to select
```

Outside a session (the full setup wizard, useful when adding a new provider):

```bash
youtab model
```

### Mixing the gateway with your own backends

If you already have, say, a Browserbase account and want to keep using it while routing web search and image generation through Youtab, that's supported. Use `youtab tools` to pick backends per tool:

```bash
youtab tools
# → Web search       → "Youtab Subscription"
# → Image generation → "Youtab Subscription"
# → Browser          → "Browserbase"  (your existing key)
# → TTS              → "Youtab Subscription"
```

The Tool Gateway is opt-in per tool, not all-or-nothing. The managed backends show up in `youtab tools` whether or not you're logged into Youtab Portal — if you pick "Youtab Subscription" before authenticating, Youtab runs the Portal login inline (it won't change your inference provider or touch your other tools). See the [Tool Gateway docs](/user-guide/features/tool-gateway) for the full per-tool configuration matrix.

### Subscription management

Manage your plan, view usage, or upgrade/cancel at any time:

- **Web:** [api.youtab.io/manage-subscription](https://api.youtab.io/manage-subscription)
- **CLI shortcut:** `youtab portal open` (opens the same page in your default browser)

## Configuration reference

After `youtab setup --portal`, `~/.youtab-agent-runtime/config.yaml` will look like:

```yaml
model:
  provider: youtab
  default: anthropic/claude-sonnet-4.6     # or whatever model you picked
  base_url: https://inference-api.youtab.io/v1
```

The Tool Gateway settings live under their respective tool sections:

```yaml
web:
  backend: firecrawl
  use_gateway: true   # web search/extract routes through Tool Gateway

image_gen:
  use_gateway: true

tts:
  provider: openai
  use_gateway: true

browser:
  cloud_provider: browser-use
  use_gateway: true
```

The OAuth refresh token is stored separately at `~/.youtab-agent-runtime/auth.json` (not in `config.yaml` — credentials and configuration are kept separate by design).

## Token handling

Youtab mints a short-lived JWT from your stored Portal refresh token on each inference call rather than reusing a long-lived API key. The token lifecycle is fully automatic — refresh, mint, retry on transient 401 — and you never see it.

If the Portal invalidates the refresh token (password change, manual revoke, session expiry), the invalid refresh token is **quarantined locally** so Youtab stops replaying it and you don't see a stream of identical 401s. The next call surfaces a clear "re-authentication required" message. Run `youtab auth add youtab` to log in again; the quarantine clears on the next successful login.

## Troubleshooting

### `youtab portal info` shows "not logged in"

You haven't completed the OAuth flow, or your refresh token was wiped. Run:

```bash
youtab portal
```

or use `youtab model` and re-select Youtab Portal.

### Got a "re-authentication required" message mid-session

Your Portal refresh token was invalidated (password change, manual revoke, or session expiry). Run `youtab auth add youtab` and your next request will use the new credentials. Any quarantine on the old token clears automatically on successful re-login.

### Want to use a specific provider model that the Portal doesn't expose

The Portal routes each model to a suitable backend — some through OpenRouter, others through proprietary or secondary providers — so most models OpenRouter supports are generally available. If a specific model isn't appearing in `/model`, try the OpenRouter-style slug directly:

```bash
/model anthropic/claude-opus-4.6
```

If a model is genuinely missing, [open an issue](https://github.com/eimanghazaei/Youtab-Agent-Runtime/issues) — we surface the Portal's catalog to Youtab and gaps usually mean a routing config we can update.

### Bills not appearing on my Portal account

Check `youtab portal info` first — if it shows you're using a different provider (`Model: currently openrouter` instead of `using Youtab as inference provider`), your local config has drifted. Run `youtab model`, pick Youtab Portal, and the next request will route through your subscription.

## See also

- **[Tool Gateway](/user-guide/features/tool-gateway)** — Full details on every gateway tool, per-tool config, and pricing
- **[Subscription proxy](/user-guide/features/subscription-proxy)** — Use your Portal subscription from non-Youtab tools (other agents, scripts, third-party clients)
- **[Voice mode](/user-guide/features/voice-mode)** — Voice conversations using the Portal's OpenAI TTS
- **[AI Providers](/integrations/providers)** — Full provider catalog if you want to compare alternatives
- **[OAuth over SSH](/guides/oauth-over-ssh)** — Login from remote hosts or browser-only environments
- **[Profiles](/user-guide/profiles)** — Multiple Youtab configurations sharing one Portal login
