# Configuration guide

All behaviour is in files you can read and version. Bots can read their own
config but cannot change any of it.

| File | What it controls |
|---|---|
| `.env` | secrets, domain, timezone, first-boot dry-run, vault path (see `.env.example`) |
| `config/bots/*.yaml` | one file per bot: tools, who it can message, schedules, model tiers and budget, concurrency, timeouts, retries, delegation depth |
| `config/models.yaml` | providers, model list per tier, prices for budgeting, global daily USD cap, cache TTL |
| `config/sources/*.yaml` | opportunity sources: URL, kind (rss/html/json), interval, parser, official domains |
| `config/profile.yaml` | your verified facts, reusable answers, documents, interests, notification preferences (git-ignored). Facts you add with `/profile set` are stored in the database and override this file |
| `policies/global.yaml` | known actions, dry-run and pause exemptions, actions never blindly retried after a crash, always-require-approval flags |
| `policies/publishing.yaml` | publishing switch, account allowlist, auto/approval categories, frequency caps, checks |
| `policies/email.yaml` | email send switch, auto-reply categories, recipient allowlist, daily cap, calendar write switches |
| `policies/outreach.yaml` | send switch, daily cap, follow-up limits, relevance threshold, sender identity |
| `policies/applications.yaml` | auto-submit rules (empty), allowed submission methods, requirements that force approval |
| `prompts/<bot>.md` | each bot's instructions (versioned by hash in the `bots` table) |
| `prompts/content/guidelines.md` | your content and reel guidelines |

Changes to `config/` and `policies/` take effect on restart:
`docker compose restart worker scheduler api`. Disabling a bot from the
dashboard persists across restarts even if its YAML says `enabled: true`.

## Common changes

**Check a source more or less often.** In the source's YAML set `interval:
6h` (accepted units: `s`, `m`, `h`, `d`). The Scout's 15-minute sweep only
fetches sources that are due.

**Change a schedule.** In the bot's YAML:
```yaml
schedules:
  - {name: morning_brief, kind: morning_brief, at: "08:00"}                # daily, local time
  - {name: weekly_plan, kind: plan_week, at: "10:00", weekdays: [mon]}
  - {name: source_sweep, kind: sweep_sources, every: 30m}
```

**Allow one kind of email to send automatically** (after you trust drafts):
```yaml
# policies/email.yaml
allow_send: true
auto_reply_categories: [meeting_confirmation]
recipient_allowlist_domains: [youruniversity.edu]
```
Everything else still waits for your approval.

**Allow auto-submission for one narrow case:**
```yaml
# policies/applications.yaml
auto_submit_rules:
  - {types: [hackathon], methods: [email], max_per_week: 2}
```
Submissions still require a verified opportunity, an eligibility check, complete
truthful answers, and none of the forbidden requirements.

**Hear from it less (or more).** Only urgent things ping you immediately:
approvals, reminders, broken credentials, a verified match closing within 72
hours, and replies to what you asked. Everything else waits for the 08:00
morning brief. In `config/profile.yaml`:
```yaml
notifications:
  quiet_hours: "23:00-07:30"     # held until the window ends; "off" to disable
  max_immediate_per_day: 8       # over this, urgent items wait too
```
`.env` overrides: `NOESIS_QUIET_HOURS`, `NOESIS_MAX_PINGS_PER_DAY`. Reminders you
set yourself with `/remind` arrive at the time you chose, even in quiet hours.
Move the brief by changing `morning_brief` in `config/bots/orchestrator.yaml`.

**Other runtime knobs (`.env`).** `NOESIS_WORKER_MODE=shared` runs all bots in
one process (for a 1 GB VPS); `NOESIS_APPROVAL_TTL_H=72` sets how long an
unanswered approval waits before it expires.

**Raise the model budget.** `config/models.yaml` → `global_daily_budget_usd`,
and the bot's `model.daily_budget_usd` / `daily_call_limit`.
