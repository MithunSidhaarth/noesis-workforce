# NOESIS Workforce

A private, single-owner team of ten AI bots that runs around the clock on a small
Linux server. It finds and checks opportunities, prepares applications from facts
you have verified, plans and renders content, triages email into drafts, keeps
reminders, files knowledge into your Obsidian vault, and reports to you on
Telegram and a web dashboard.

It starts **safe**: dry-run on, publishing off, email send off, outreach off.
Nothing leaves the server until you turn it on, and the risky actions still need
your tap on an approval.

## What is in the box

| Part | Where |
|---|---|
| Ten bots (Scout, Applications, Content Strategist, Content Studio, Communications, Outreach, Knowledge, Personal Ops, Quality, Chief of Staff) | `workforce/agents/`, `config/bots/*.yaml`, `prompts/` |
| Durable queue, typed message bus (18 message types), scheduler | `workforce/core/queue.py`, `workforce/messaging/bus.py`, `workforce/scheduler.py` |
| Policy engine, approvals, audit trail, idempotent external actions | `workforce/policies/engine.py`, `policies/*.yaml` |
| Model router with budgets (works with **no** model configured) | `workforce/model_router/`, `config/models.yaml` |
| Owner actions shared by Telegram, dashboard and CLI | `workforce/owner_actions.py` |
| API + dashboard (17 pages, static export served by the API) | `services/api/`, `apps/dashboard/` |
| Telegram command centre with buttons, owner-id only | `services/telegram/bot.py` |
| Obsidian vault adapter (never overwrites your notes) | `integrations/obsidian/vault.py` |
| Docker Compose deployment with Caddy HTTPS, backups | `docker-compose.yml`, `deploy/`, `scripts/` |
| Tests (82, all passing) | `tests/` |

## Quick start (local, no Docker)

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # fill in what you have; blanks are fine
python -m pytest -q               # 82 passed
python -m services.scheduler.main &        # migrates the DB, runs schedules + housekeeping
python -m services.worker.main --shared &  # all bots in one process
python -m uvicorn services.api.app:app --port 8000   # API (+ dashboard once built: cd apps/dashboard && npm ci && npm run build)
```

For the real server, follow **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** (12 steps), or
**[docs/ORACLE.md](docs/ORACLE.md)** for the free Oracle Cloud option with no domain.

## Using it day to day

- **Morning brief at 08:00** on Telegram: what needs you, what closes this week,
  what is new, what is at risk. Non-urgent alerts wait for it; urgent ones come
  right away outside quiet hours (23:00–07:30 by default, max 8 pings a day).
- **Opportunity cards** come with *Prepare*, *Remind me* and *Not for me*
  buttons. *Prepare* means "this is real"; the Applications bot then drafts it
  from your verified profile and tells you exactly which facts are missing.
- **Approvals** come with *Approve* / *Deny* buttons, or `/inbox` then `/approve 1`.
- `/help` lists every command. `python scripts/noesisctl.py doctor` tells you
  what is misconfigured.

## Documentation

| Doc | What it covers |
|---|---|
| [ARCHITECTURE](docs/ARCHITECTURE.md) | components, how work flows, design decisions |
| [DEPLOYMENT](docs/DEPLOYMENT.md) | VPS setup from a clean machine, update, rollback |
| [ORACLE](docs/ORACLE.md) | €0 option: Oracle Cloud Always Free ARM machine, private mode over Tailscale |
| [CONFIGURATION](docs/CONFIGURATION.md) | every env var, policy and bot setting |
| [INTEGRATIONS](docs/INTEGRATIONS.md) | integration checklist and what is live-tested |
| [SECURITY](docs/SECURITY.md) | threat model, controls, recovery runbook |
| [BOT_GUIDE](docs/BOT_GUIDE.md) | how to add or change a bot |
| [TEST_REPORT](docs/TEST_REPORT.md) | the 24 acceptance criteria and their tests |
| [MANUAL_STEPS](docs/MANUAL_STEPS.md) | what only you can do |
| [COSTS](docs/COSTS.md) | infrastructure and model cost estimate |
| [STATUS](docs/STATUS.md) | what works, what is untested, known gaps |
| [IMPLEMENTATION_PLAN](docs/IMPLEMENTATION_PLAN.md) | phases and what each delivered |
| [ROADMAP](docs/ROADMAP.md) | what to build next |

## Honest status

Everything listed above is implemented and covered by automated tests that use
fakes for outside services. **No external service (Telegram, Gmail, Calendar,
LinkedIn, Instagram, opportunity websites, model providers) was called live**
from the build environment, because its network blocks them. The first real
run of each is yours; [INTEGRATIONS](docs/INTEGRATIONS.md) walks through it.
