# Live-Benchmark Provider Selection Matrix (WAVE-30B, section 14)

> **DECISION-SUPPORT ONLY - PROVIDER-NEUTRAL - NO RECOMMENDATION.**
> This document exists solely to help the repository Owner *later* choose a live-benchmark
> provider. It **does not recommend, rank, prefer, or default to any provider** (explicitly
> including Anthropic). Every pricing, retention, residency, and privacy figure below is a
> point-in-time reading of a provider public documentation and **must be re-verified by the
> Owner at selection time**, because provider prices and terms change frequently and without
> notice. Where a figure is not clearly documented on a reachable official page, it is marked
> **Unknown** and left Unknown - it was not guessed or estimated. Cost figures are **conservative
> worst-case ceilings**, not predictions.

- **Access date for all sources:** 2026-09-05 (dates stamped per row).
- **Author context:** Youtab-Agent-Runtime, WAVE-30B live-benchmark provider selection.
- **FX rate used:** 1 USD = EUR 0.86 (see "Currency and FX" note below).
- **Budget frame:** HARD cumulative **EUR 10.00** ceiling across Canary + Pilot + Full.

## Benchmark stage maxima (given)

| Stage  | Requests | Token ceiling                      | Token split used for cost math |
| ------ | -------- | ---------------------------------- | ------------------------------ |
| Canary | 1        | ~4,096 input + 512 output (~4,608) | Split **given** (4,096 in / 512 out) |
| Pilot  | up to 40 | up to 250,000 total                | **Worst-case: 100% output** (see assumption) |
| Full   | up to 392| up to 1,000,000 total              | **Worst-case: 100% output** (see assumption) |

**Cost assumption (conservative):** For Pilot and Full the input/output split is unspecified, so
each stage entire token budget is priced at the model **output** rate (output is the more expensive
side for every provider here). This over-states real cost and is deliberately pessimistic. Canary
uses the given 4,096-in / 512-out split. All costs assume **standard (non-batch, non-cached)**
pricing; batch/cache discounts would only lower these numbers.

---

## Table A - Protocol, secret handling, model, price, worst-case cost

Prices are **per 1,000,000 tokens**. Currency noted per cell (USD unless stated). EUR cost columns use 1 USD = EUR 0.86.

| Provider | Protocol (repo class) | File-based secret (post-WAVE-30B) | Representative cheap model id | Price source URL + access date | Input / Output / Cached-input (per 1M) | Canary max (EUR) | Pilot max 250k @ output (EUR) | Full max 1M @ output (EUR) | Feature notes (tools / streaming / vision) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **OpenAI** | OpenAI Chat Completions | yes (OPENAI_API_KEY_FILE) | gpt-4o-mini (cheaper: gpt-5-nano $0.05/$0.40) | https://developers.openai.com/api/docs/pricing (2026-09-05) | $0.15 / $0.60 / $0.075 USD | ~$0.0009 -> **EUR 0.0008** | $0.15 -> **EUR 0.13** | $0.60 -> **EUR 0.52** | tools yes, streaming yes, vision yes |
| **Anthropic** | Anthropic Messages | yes (ANTHROPIC_API_KEY_FILE) | claude-haiku-4-5 | https://platform.claude.com/docs/en/docs/about-claude/pricing (2026-09-05) | $1.00 / $5.00 / $0.10 USD (cache-hit) | ~$0.0067 -> **EUR 0.0057** | $1.25 -> **EUR 1.08** | $5.00 -> **EUR 4.30** | tools yes, streaming yes, vision yes |
| **Google (Gemini / AI Studio)** | Gemini-native | yes (GOOGLE_API_KEY_FILE / GEMINI_API_KEY_FILE) | gemini-2.5-flash-lite (retires 2026-10-16 -> gemini-3.1-flash-lite $0.25/$1.50) | https://ai.google.dev/gemini-api/docs/pricing (2026-09-05) | $0.10 / $0.40 / $0.01 USD (text) | ~$0.0006 -> **EUR 0.0005** | $0.10 -> **EUR 0.09** | $0.40 -> **EUR 0.34** | tools yes, streaming yes, vision yes; free tier exists (see privacy row) |
| **DeepSeek** | OpenAI Chat Completions | yes (DEEPSEEK_API_KEY_FILE) | deepseek-v4-flash (peak rates used) | https://api-docs.deepseek.com/quick_start/pricing/ (2026-09-05) | $0.44 / $1.32 / $0.014 USD **peak** (off-peak $0.22/$0.66/$0.007) | ~$0.0025 -> **EUR 0.0021** | $0.33 -> **EUR 0.28** | $1.32 -> **EUR 1.14** | tools yes, streaming yes; vision = separate deepseek-v4-flash-vision-exp |
| **xAI** | OpenAI Responses | yes (XAI_API_KEY_FILE) | grok-4.3 (cheapest general standard) | https://docs.x.ai/docs/models (2026-09-05) | $1.25 / $2.50 / $0.20 USD (<200k ctx) | ~$0.0064 -> **EUR 0.0055** | $0.625 -> **EUR 0.54** | $2.50 -> **EUR 2.15** | tools yes, streaming yes; vision on select Grok models (verify per model) |
| **Z.AI / GLM** | OpenAI Chat Completions | yes (GLM_API_KEY_FILE / ZAI_API_KEY_FILE) | glm-4.5-air (free: glm-4.5-flash, glm-4.7-flash) | https://docs.z.ai/guides/overview/pricing (2026-09-05) | $0.20 / $1.10 / $0.03 USD | ~$0.0014 -> **EUR 0.0012** | $0.275 -> **EUR 0.24** | $1.10 -> **EUR 0.95** | tools yes, streaming yes; vision via separate GLM-V model |
| **Mistral** (OpenAI-compat route) | OpenAI Chat Completions (via custom/compat endpoint) | yes on the compat route env _FILE (no native registry provider) | mistral-small-latest (Mistral Small 4) | https://mistral.ai/pricing/api (2026-09-05) | $0.15 / $0.60 / (cache -90%) USD | ~$0.0009 -> **EUR 0.0008** | $0.15 -> **EUR 0.13** | $0.60 -> **EUR 0.52** | tools yes, streaming yes, vision yes (Mistral Small multimodal) |
| **OpenRouter** | OpenAI-compatible aggregator | yes (OPENROUTER_API_KEY_FILE) | pass-through (pick underlying model) | https://openrouter.ai/models (2026-09-05) | Varies by routed model (+~5% credit fee); cheap routes e.g. $0.04/$0.15 USD | Depends on routed model | Depends on routed model | Depends on routed model | tools/streaming/vision depend on routed model |
| **Alibaba / Qwen** | OpenAI Chat Completions | yes (DASHSCOPE_API_KEY_FILE) | qwen-turbo / qwen-plus | Official Model Studio pricing page JS-rendered, **returned no figures**: https://www.alibabacloud.com/help/en/model-studio/models (2026-09-05) | **Unknown at official page** (aggregator-reported ~$0.05/$0.20 turbo, ~$0.40/$1.20 plus - NOT official) | Unknown (price unconfirmed) | Unknown (price unconfirmed) | Unknown (price unconfirmed) | tools yes, streaming yes; vision via separate Qwen-VL model |
| **Local (Ollama / vLLM)** | Custom / local OpenAI-compatible | N/A (no cloud key; custom provider) | any self-hosted weight (e.g. qwen2.5, llama3.x) | Self-hosted - no per-token price | EUR 0 API cost (hardware/electricity only) | **EUR 0** | **EUR 0** | **EUR 0** | tools/streaming/vision depend on served model; full local residency |

**Notes on Table A**
- OpenAI cheapest overall is gpt-5-nano ($0.05 in / $0.40 out / $0.005 cached); gpt-4o-mini shown as a well-understood tool+vision baseline.
- DeepSeek uses **peak** rates for the worst-case ceiling; peak hours are 01:00-04:00 and 06:00-10:00 UTC Mon-Fri, all other hours off-peak (cheaper). deepseek-chat / deepseek-reasoner are no longer on the pricing page - the current line is the V4 family.
- Z.AI lists glm-4.5-flash and glm-4.7-flash as **free**, plus a promotional glm-5.3-flash at $0.075/$0.25 (through 2026-09-09). Free quotas/limits must be confirmed by the Owner.
- Google Gemini gemini-2.5-flash-lite **retires 2026-10-16**; plan for gemini-3.1-flash-lite ($0.25/$1.50) if the benchmark runs after that date.
- Mistral is **not a native PROVIDER_REGISTRY entry**; it is reachable only via the repo custom/OpenAI-compatible route, so <ENV>_FILE support attaches to that route configured env var, not a dedicated MISTRAL_API_KEY.

---

## Table B - Data governance (residency, retention, training, ZDR, keys, spend controls, uncertainty)

| Provider | EU data-residency option | Retention / training-on-data policy | Zero-Data-Retention (ZDR) | Enterprise / project-scoped keys | Provider-side spend cap / rate-limit controls | Known privacy / contractual UNCERTAINTY |
| --- | --- | --- | --- | --- | --- | --- |
| **OpenAI** | **Yes** - project-level data residency incl. **EU**; EU supports regional inference (non-US regions need approval for abuse controls). Source: https://developers.openai.com/api/docs/guides/your-data (2026-09-05) | API data **not used for training by default** (since 2023-03-01, opt-in only); abuse logs retained **up to 30 days** unless law requires longer; some stateful endpoints retain longer | **Yes** - ZDR for eligible customers after OpenAI approval; excludes some endpoints; forces store=false | Yes - projects + project-scoped API keys | Dashboard usage/monthly budget limits; tiered rate limits | Residency and ZDR are **approval-gated** - confirm eligibility for the specific account/region |
| **Anthropic** | **Unknown** on fetched pages (no first-party EU residency documented; inference_geo supports "us" only). Source: https://platform.claude.com/docs/en/about-claude/pricing + https://privacy.claude.com/en/articles/7996866 (2026-09-05) | Default: inputs/outputs auto-deleted **within 30 days**; violations extend to 2 years. Whether commercial/API data is used for training **was not stated on the fetched retention page** -> **Unknown, confirm in Commercial Terms** | **Yes** - ZDR available to eligible Claude API / Claude Code Enterprise customers on approval; safety-classifier results still retained. Source: https://privacy.claude.com/en/articles/8956058 (2026-09-05) | Yes - organizations + workspace/API keys | Usage tiers (Start/Build/Scale); console spend limits; sales for higher | Training-on-commercial-data default and EU residency **not confirmed from fetched docs** - verify in Commercial Terms/DPA |
| **Google (Gemini / AI Studio)** | Vertex AI offers regional (incl. EU) processing; **AI Studio Gemini API** residency **Unknown/limited** - verify. EEA/CH/UK: paid-tier protections apply to **all** services incl. free. Source: https://ai.google.dev/gemini-api/terms (2026-09-05) | **Paid tier:** prompts/responses **not** used to improve products; logged a limited period for abuse/security only. **Free (unpaid) tier:** content **is** used to improve products and **human reviewers may read** it | **Unknown** as a named Gemini-API feature (Vertex has controls) - verify | Yes - Google Cloud projects + API keys / service accounts | Cloud billing budgets and alerts (Vertex); AI Studio quota limits | Free vs paid tier changes everything - do **not** send confidential data on the free tier; AI-Studio residency Unknown |
| **DeepSeek** | **No EU residency** - data stored on **servers in the PRC**; no per-customer EU option documented. Source: https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html (2026-09-05) | Consumer policy: inputs (incl. sensitive) may be **stored (potentially indefinitely) and used for AI training**; no universal API-wide no-training / retention commitment published | **Unknown / not universally published** - verify per account | Project/API keys yes; enterprise DPA terms **Unknown** | API-side spend/rate limits exist; specifics **Unknown** | **High uncertainty**: PRC storage, possible government access under PRC law, training on inputs; API-specific safeguards must be contracted and verified |
| **xAI** | **Unknown** (no documented EU residency). Source: https://docs.x.ai/developers/faq/security (2026-09-05) | Requests/responses stored **encrypted 30 days** for abuse auditing then deleted; **not used for training** (per FAQ). Enterprise API adds SOC 2 Type 2, GDPR, CCPA | **Yes** on Enterprise API; x-zero-data-retention response header reports ZDR state | Yes - enterprise API + API keys | API rate limits; spend controls **Unknown** - verify in console | 2026 Grok Build over-upload incident shows operational risk; confirm ZDR enrollment + residency in writing |
| **Z.AI / GLM** | International entity (Singapore Pte. Ltd.) processes data **generally in Singapore**; transfers to affiliates (Beijing parent) **not excluded**; no EU residency documented. Source: https://docs.z.ai/legal-agreement/privacy-policy (2026-09-05) | API content **not used for training** unless explicitly agreed (opt-in); API DPA claims **real-time processing, no storage** - but no retention table/audit backs it | **Unknown** - no formal ZDR product documented; DPA no-storage claim unverified | Yes - API keys; enterprise DPA terms partly **Unknown** | API rate limits; spend caps **Unknown** - verify | Affiliate transfer to PRC parent not excluded; no-storage claim **unverified** - get contractual confirmation |
| **Mistral** | **Yes** - data hosted in the **EU by default**; natively GDPR-subject; US endpoint optional. Source: https://help.mistral.ai/en/articles/347629 + https://help.mistral.ai/en/articles/347612 (2026-09-05) | Inputs/outputs retained **30 rolling days** for abuse monitoring then deleted; **not used for training** unless opt-in (paid API opted out by default) | **Yes** - scoped to **Scale plan** and stateless calls (chat/embeddings/moderation/OCR/audio); removes the 30-day window | Yes - org + API keys | Console usage limits; specifics **Unknown** - verify | ZDR requires Scale plan; verify DPA/SCCs on legal.mistral.ai for regulated use |
| **OpenRouter** | Depends on **routed** provider (pass-through). Source: https://openrouter.ai/docs/features/privacy-and-logging (2026-09-05) | OpenRouter own logging/training policy is **separate**; users toggle whether to allow providers that train; **no automatic routing by retention** - user must filter | Depends on routed provider; can restrict to providers with a given data policy per-request/account | Yes - API keys; provisioning keys | Credit-based prepaid balance (implicit spend cap); per-key limits | Compliance is **delegated to the user provider filter** - a single misrouted request can hit a training-on-data provider |
| **Alibaba / Qwen** | **Yes (possible)** - Model Studio regions include **Germany (Frankfurt)**; static data stays in selected region; also Singapore/US/Japan. Source: https://www.alibabacloud.com/help/en/model-studio/faq-about-alibaba-cloud-model-studio (2026-09-05) | FAQ states data is **never used for model training**; AES-256 in transit; per-region static data residency | **Unknown** - no formal ZDR product documented | Yes - Alibaba Cloud accounts + API keys | Cloud billing/quota controls | Retention duration and ZDR **not documented**; confirm which legal entity/region governs your account |
| **Local (Ollama / vLLM)** | **Full** - self-hosted, data never leaves Owner infrastructure | No external retention or training - Owner-controlled | **Inherent** (nothing leaves the host) | N/A (local) | Hardware-bounded; no external spend | None external; only local security/ops responsibility |

---

## How these map to the EUR 10 ceiling (no recommendation)

At the **cheap representative model tier used above**, every cloud provider worst-case Full run
(1,000,000 tokens priced entirely at the output rate) lands well under EUR 10 on its own - ranging
from about **EUR 0.34** (Gemini Flash-Lite) to **EUR 4.30** (Claude Haiku 4.5). Summing the three
stages at their worst case for the priciest representative here (Anthropic Haiku:
EUR 0.006 + EUR 1.08 + EUR 4.30 = about **EUR 5.38**) still fits inside the EUR 10 cumulative ceiling.
The DeepSeek/Z.AI/Qwen free tiers and the **local Ollama/vLLM option (EUR 0)** trivially fit.
**The EUR 10 ceiling therefore constrains model *tier*, not provider:** if the Owner instead selects a
flagship model (e.g. an Opus/Sonnet-class, Gemini Pro-class, or grok-4.6-class model at $10-$25 output
per 1M), a single worst-case Full run can exceed EUR 10 and would breach the cumulative cap. Qwen fit is
**unconfirmed** because its official per-token price was not reachable at fetch time. None of the above
is a recommendation - it is arithmetic against a fixed ceiling.

## Currency and FX

All EUR figures use **1 USD = EUR 0.86**, a mid-market USD->EUR rate read on **2026-09-05**
(source: search of Wise / trading-economics / xe mid-market quotes, which showed about EUR 0.860 that
day; weekly range about EUR 0.858-0.864). FX rates move continuously; re-convert with the rate at
purchase/billing time. All native prices remain in **USD** as published by each provider (no provider
on this list quotes the representative model in EUR).

## What the Owner must still verify (contractual facts, not agent assertions)

These are legal/commercial facts the Owner must confirm directly with the provider before a live run -
they are **not** things this agent can or did settle:

- [ ] **EU DPA + SCCs / GDPR basis** - signed Data Processing Addendum and Standard Contractual Clauses (or adequacy mechanism) for any provider processing outside the EEA, and the identity of the contracting legal entity (esp. DeepSeek/Z.AI/Qwen).
- [ ] **Retention and training opt-out** - written confirmation that inputs/outputs are not used for training and the exact retention window for the chosen account/region (re-verify; terms change).
- [ ] **ZDR enrollment** - whether Zero-Data-Retention is actually enabled for the account (many are approval-gated: OpenAI approval, Anthropic eligibility, Mistral Scale plan, xAI Enterprise API); confirm scope and any excluded endpoints.
- [ ] **Project-/workspace-scoped key** - provision a dedicated, least-privilege, project-scoped API key for the benchmark only (not a personal/org-wide key).
- [ ] **Provider-side spend cap** - set a hard budget/usage limit in the provider console as defense-in-depth on top of the harness EUR 10 ceiling (do not rely on client-side limits alone).
- [ ] **Data residency region** - pin the inference/storage region (e.g. OpenAI EU project, Qwen Frankfurt, Mistral EU default) and confirm it in the console before the first live call.
- [ ] **Re-verify all prices** - pull each provider official pricing page again on the run date; figures above are stamped 2026-09-05 and will drift (e.g. Gemini 2.5 Flash-Lite retires 2026-10-16; DeepSeek has peak/off-peak; Z.AI promo ends 2026-09-09).
- [ ] **Qwen official price** - obtain the official Alibaba Model Studio per-token price (the JS-rendered page did not expose it to automated fetch); do not rely on the aggregator figures noted here.

---

---

## WAVE-30C addendum — dual-track framing + ranked shortlist (`OWNER_SELECTION_REQUIRED`)

The benchmark now has **two comparable tracks** (see
[`tests/benchmark/COMPARABILITY.md`](../../tests/benchmark/COMPARABILITY.md)):

- **Track A — Local / ECO** uses the local Ollama-served ECO engine at **€0 API
  cost** and is **excluded from the €10 cloud budget**. Its exact model tag is
  Owner/registry-supplied (`YOUTAB_ECO_MODEL`); note **"Qwen 3.5 9B" is not a
  canonical open-weight identifier** — the repo carries the placeholder
  `ollama/qwen3.5:9b` and the Owner must supply the real tag before a run.
- **Track B — Cloud** is exactly one **non-Anthropic** provider from the table
  above, **selected by the Owner** after re-verifying the criteria, bounded by the
  €10 campaign ledger.

> **This shortlist is `OWNER_SELECTION_REQUIRED`. It ranks candidates against
> explicit, stated criteria to aid the decision; it does NOT select, recommend or
> default to any provider, and Anthropic is excluded from Track B by directive.
> Rankings flip with the criterion weighted — both lenses are shown. Every figure
> must be re-verified by the Owner at selection time (§"What the Owner must still
> verify").**

**Lens 1 — privacy / EU-residency / no-training first** (safest when the benchmark
may process any non-public data):

1. **Mistral** — EU-hosted by default, GDPR-native, not trained on API data by
   default, ZDR on the Scale plan. Reachable via the OpenAI-compatible route.
2. **OpenAI** — documented EU data-residency option, no training on API data by
   default, ZDR available (approval-gated).
3. **Google (Vertex AI)** — EU regional processing on Vertex, paid-tier not used
   for training (AI Studio residency Unknown — prefer Vertex; never the free tier).
4. **Alibaba / Qwen (Model Studio, Frankfurt)** — EU region + "never used for
   training" claim, but retention/ZDR undocumented and official price not
   machine-verifiable — higher residual uncertainty.
5. **xAI / Z.AI / DeepSeek / OpenRouter** — lower on this lens: no documented EU
   residency (xAI), PRC-affiliate transfer not excluded (Z.AI), PRC storage +
   possible training on inputs (DeepSeek), or compliance delegated to per-request
   routing (OpenRouter). Selectable only with a signed DPA/ZDR and a pinned region.

**Lens 2 — lowest worst-case cost first** (at the cheap representative tier; all
fit under €10, so this constrains model *tier*, not provider):

1. **Google Gemini Flash-Lite** (~€0.34 full worst-case) · 2. **OpenAI
   gpt-4o-mini / gpt-5-nano** (~€0.52) · 3. **Mistral Small** (~€0.52) · 4. **Z.AI
   GLM** (~€0.95, free flash tiers exist) · 5. **DeepSeek** (~€1.14 peak, cheaper
   off-peak). Qwen unconfirmed (official price not machine-readable). OpenRouter
   depends on the routed model.

**Selection gate (all must hold before the Owner selects):** non-Anthropic ·
provider on the implemented-protocol list · signed EU DPA/SCCs where processing
leaves the EEA · written no-training + retention window · ZDR enrollment confirmed
· dedicated project-scoped spend-capped key · provider-side hard budget cap ·
pinned residency region · **all prices re-verified on the run date**.

---

*Prepared as provider-neutral decision-support for WAVE-30B §14 and the WAVE-30C
dual-track framing. No provider is recommended or defaulted; Track B remains
`OWNER_SELECTION_REQUIRED`. All figures require Owner re-verification at selection
time.*
