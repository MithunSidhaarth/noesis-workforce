# Status

Last updated: 2026-10-16. Branch `main`.

## Summary

All ten bots, the runtime, dashboard, Telegram command centre, policies and
deployment are implemented and pass 82 automated tests. The Docker stack builds
and runs healthy. **No outside service has been called live yet**, so every
integration is "implemented, tested with fakes, waiting for your first real run".

## Works and is tested (automated)

- Durable queue: leases, heartbeats, retries with backoff, dead-letter,
  idempotency, dependencies (`task_deps`), checkpoints, cancellation, crash and
  restart recovery, worker hand-back on shutdown.
- Message bus: 18 typed messages, permissions, dedupe, loop detection, expiry.
- Policy engine and gateway: tool grants, pause (work waits and resumes),
  dry-run, approvals from the DB only, daily caps that defer to tomorrow,
  in-flight reservation so an interrupted send is never repeated blindly.
- Owner loop: notification store with quiet hours and a daily ping cap,
  morning brief, Telegram buttons for approvals and opportunity cards, numbered
  inbox, `/confirm`, `/skip`, `/remind`, `/profile set`, `/submitted`, `/outcome`,
  `/missing`; the same actions on the dashboard and in `noesisctl.py`.
- Opportunity pipeline: fetch → parse → dedupe across types → Quality check →
  score → card → deadline reminders → your confirmation → draft from verified
  facts → submission record → follow-up → outcome → weekly funnel.
- Housekeeping: stale lease reclaim, approval expiry (72 h), workflow reports,
  stuck-schedule alerts, data retention prune with WAL checkpoint.
- Content: weekly plan, briefs, real FFmpeg reel render (on a host with FFmpeg),
  publication gated by policy.
- Communications: inbox triage, drafts; sending off by policy.
- Knowledge: article capture into the vault, social links queued for your
  local NOESIS pipeline.
- Dashboard (17 pages) reading real DB state; API auth with CSRF header.

## Implemented, not yet live-tested

Telegram, Gmail, Google Calendar, LinkedIn, Instagram, the six opportunity
sources, model providers (Gemini, Groq, Anthropic, Ollama), Syncthing vault
sync, Caddy's Let's Encrypt certificate, Redis wake-ups. The build environment's
network blocks all of them. See [INTEGRATIONS](INTEGRATIONS.md).

## Known gaps

- Instagram reels need a public HTTPS URL for the video; no media host is built.
- Web-form applications are prepared, not submitted (by design: no browser
  automation, no CAPTCHA handling).
- Social video (Instagram/TikTok/YouTube) knowledge capture depends on the
  NOESIS pipeline on your PC.
- Token prices in `config/models.yaml` are estimates; verify them.
- The six default sources are aggregators. Their listings stay "needs review"
  until you confirm them; official sources you add yourself verify directly.
- Single-server design. No high availability; recovery is backup + restore.

## Next steps for you

See [MANUAL_STEPS](MANUAL_STEPS.md). The first real milestone is: Telegram
`/status` answers on your phone, and the first morning brief arrives.
