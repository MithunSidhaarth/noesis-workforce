# Architecture

NOESIS Workforce is a single-owner system of ten independent bots that share one
durable runtime. The design goal is measurable work completed with as few moving
parts as possible: one SQLite database, plain Python processes, and a static
dashboard served by the API.

```
             Telegram (long poll)        Browser (HTTPS, Caddy)
                     │                           │
            services/telegram            services/api  ──► apps/dashboard (static export)
                     │                           │  SSE /api/events
                     ▼                           ▼
   ┌────────────────────────── SQLite (WAL) ───────────────────────────┐
   │ bots · tasks · messages · schedules · approvals · external_actions │
   │ audit_log · exec_log · model_usage · domain tables (opportunities…) │
   └────────────────────────────────────────────────────────────────────┘
          ▲                 ▲                         ▲
   services/scheduler   services/worker (supervisor) ── one OS process per bot
   (schedules, lease     ├─ BotRuntime(opportunity_scout)
    reclaim, liveness)   ├─ BotRuntime(quality)  …ten in total
                         └─ optional Redis pub/sub wake (else 2 s SQLite poll)
```

## Components

| Requirement | Where it lives |
|---|---|
| Central control plane | `services/api/app.py` (FastAPI) + `workforce/registry.py` |
| Independent runtime per bot | `workforce/runtime.py` `BotRuntime`; `services/worker/main.py` runs one process per bot and restarts crashed ones with backoff |
| Durable task queue | `workforce/core/queue.py` (leases, heartbeats, retries with jittered backoff, dead-letter, idempotency keys, dependency DAG, checkpoints, cancellation) |
| Persistent message bus | `workforce/messaging/bus.py` (typed `Message`, permissions, topics, dedupe, loop detection, expiry, ack/nack, dead-letter) |
| Scheduler / event dispatcher | `workforce/scheduler.py`, `services/scheduler/main.py`; `workforce/core/notify.py` for wake-ups |
| Shared operational database | `database/migrations/*.sql`, `workforce/core/db.py` |
| Shared knowledge layer | `integrations/obsidian/vault.py` (NOESIS vault adapter) + `knowledge` bot |
| Model routing | `workforce/model_router/router.py`, `config/models.yaml` |
| Permissions & policy engine | `workforce/policies/engine.py` (`PolicyEngine`, `ActionGateway`), `policies/*.yaml` |
| Dashboard & Telegram | `apps/dashboard/`, `services/telegram/bot.py` |
| Integrations | `integrations/*` behind small interfaces; agents never import provider SDKs directly |
| Audit & observability | `audit_log`, `exec_log`, `external_actions`, `model_usage`; JSON logs; `/api/health` |

## Bots

Each bot is a YAML file in `config/bots/` (identity, purpose, instructions file,
tools, who it may message, topics, memory scope, model tiers and budget,
schedules, concurrency, timeout, retry, delegation depth) plus an `Agent` class
in `workforce/agents/`. The registry syncs YAML into the `bots` table on start;
a bot a human disabled stays disabled.

| Bot | Starts work by itself | Can cause external effects via |
|---|---|---|
| Opportunity Scout | sweep every 15 min, digest 08:30 | Telegram alert, vault note |
| Quality & Verification | on VERIFICATION_REQUEST | none (read-only fetches) |
| Applications | on delegated `evaluate_opportunity` | `application.submit` (policy + approval) |
| Content Strategist | Monday 10:00 plan | none |
| Content Studio | on brief | `social.publish` (disabled by policy) |
| Communications | inbox every 10 min | `email.draft`; `email.send` / calendar writes disabled by policy |
| Outreach | follow-ups 11:00, report Friday | `outreach.send` (disabled by policy) |
| Knowledge | on URL / request | `vault.write` |
| Personal Ops | reminder sweep every 5 min | Telegram reminders |
| Chief of Staff | objectives, 21:00 daily and Sunday weekly summary | Telegram, vault review notes |

Bots are not "labels in one chatbot": each has its own process, config row,
memory scope (`bot_memory` keyed by scope), tool grants and inbox.

## How work flows

1. **Triggers** — a schedule fires (`sched:{id}:{slot}` idempotency key, no
   overlapping runs), a human sends an objective (dashboard/Telegram/CLI), or a
   bot sends a message.
2. **Tasks** are rows. A runtime claims them with `BEGIN IMMEDIATE`, holds a
   lease and heartbeats. If the process dies, the scheduler reclaims the lease
   and the task retries. Dependencies (`depends_on`) keep tasks `pending` until
   their parents succeed; a dead parent cancels its dependents.
3. **Messages** carry the 18 typed message types (`workforce/contracts.py`).
   `TASK_REQUEST` becomes a task on the recipient (if it `accepts` that kind)
   and is answered with `TASK_ACCEPTED`/`TASK_REJECTED` and later `TASK_RESULT`.
   Messages from other bots are validated data, never authority.
4. **External effects** only go through `AgentContext.act` → `ActionGateway`:
   idempotency → approval state (from the DB only) → policy (tool grant, global
   pause, action rules, mandatory-approval flags) → dry-run → execute → record
   evidence in `external_actions` and `audit_log`.
5. **Models** are optional. `ctx.llm()` goes through the router, which picks a
   configured provider within the bot's tiers and budgets, caches results, and
   raises `BudgetExceeded` (task defers) or `ModelUnavailable` (task retries).
   Every bot has a deterministic path that works with no model at all.

### Opportunity workflow (implemented end to end)

```
Scout.check_source ─fetch+parse+dedupe─► opportunities row
   └─VERIFICATION_REQUEST─► Quality.verify (official domain, HTTPS, reachability, deadline, duplicates)
        └─VERIFICATION_RESULT─► Scout.process_verification
              ├─ score vs profile, write "09 Opportunities" note
              ├─ TASK_REQUEST evaluate_opportunity ─► Applications (eligibility from verified facts)
              │      └─ create_reminder ─► Personal Ops (deadline reminders)
              ├─ publish opportunity.verified
              └─ Telegram alert if score ≥ 0.7
```

## Decisions and why

- **SQLite instead of Postgres + broker.** One file, WAL mode, atomic claims with
  `BEGIN IMMEDIATE`; enough for one owner and ten bots on a small VPS, trivial
  to back up. Redis is optional and only used to wake runtimes instantly.
- **Static dashboard export.** No Node server in production; the API serves
  the files, so there is one origin, one auth cookie and no CORS.
- **Long-polling Telegram.** No inbound webhook port to secure.
- **No browser automation by default.** APIs first; web-form applications are
  prepared and handed to you, never pushed through a browser.
- **Hermes is not used.** Nothing depends on it.
