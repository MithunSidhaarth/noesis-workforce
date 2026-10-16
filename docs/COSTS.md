# Cost estimate

All prices are estimates from memory, not quotes. Check each provider's
current price page before you buy anything.

## Infrastructure (monthly)

| Item | Low-cost option | Notes |
|---|---|---|
| Server | €0 on Oracle Cloud Always Free (ARM, see [ORACLE](ORACLE.md)), or ~€4–6 for a VPS (2 vCPU, 4 GB RAM, e.g. Hetzner CX22 class) | Measured on the build machine: worker ≈380 MB with one process per bot. `NOESIS_WORKER_MODE=shared` uses less. 2 GB RAM is the minimum; 4 GB leaves room for FFmpeg renders. 1 GB plans are too small. |
| Domain | €0 in private mode (Tailscale), else ≈€10/year or a free DuckDNS-style subdomain | Only public mode needs one, for Caddy's Let's Encrypt certificate. |
| Backups off-server | €0 (copy to your PC) to ~€1 (object storage) | `scripts/backup.py` keeps 14 nightly archives locally. |
| Telegram, Syncthing, Obsidian, Gmail/Calendar API (personal use) | €0 | Google APIs are free at this volume. |
| LinkedIn / Instagram APIs | €0 | Needs developer apps; publishing is off by default. |
| **Infrastructure total** | **€0 on Oracle Always Free, ≈€5–8 / month on a VPS** | |

## Models (monthly)

Models are optional. Every bot has a deterministic path that works with no
model, so **€0 is a valid configuration**.

| Option | Cost | What you get |
|---|---|---|
| No model | €0 | Rule-based scoring, templated drafts, keyword summaries. |
| Gemini (Google AI Studio) and/or Groq free tiers | €0 | Better summaries and drafts. Rate limits apply; free tiers can change or be withdrawn, and some free tiers may use your prompts for training (check the terms). |
| Anthropic API (pay as you go) | capped by `global_daily_budget_usd: 0.50` → at most ≈$15/month | Best quality. The default caps allow only cheap and standard tiers for most bots. |
| Local model via Ollama | €0 in fees, needs a bigger VPS (8 GB+) | Usually costs more in server than the free API tiers. |

**Claude Pro is not API access.** A Claude Pro subscription does not include
API credits and must not be used for unattended runtime calls. The
`ANTHROPIC_API_KEY` is billed separately per token.

Budgets are enforced in code: each bot's `model.daily_call_limit` and
`daily_budget_usd`, plus the global daily cap. When a cap is reached the task
is deferred to the next day rather than calling anyway
(`test_usage_budgets_prevent_unbounded_model_calls`). The Usage page shows
spend by bot and model. Token prices in `config/models.yaml` drive these
budgets, so update them if the provider's prices change.

## Recommended starting point

Oracle Always Free in private mode with the Gemini free tier: **€0 a month**
(the account must be upgraded to Pay As You Go to avoid idle reclamation; set
the $1 budget alert). If Oracle has no capacity: a 4 GB VPS, about €5 a month.
Add an Anthropic key later with the $0.50/day cap if the free-tier quality is
not enough.
