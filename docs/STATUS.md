# Status

Last updated: 2026-10-26. Branch `v2.0` (PR to `main`).

## v2.0 (this branch)

175 automated tests pass, `ruff check --select F .` is clean and the dashboard
builds. Everything below is tested with fakes only; nothing new was called live.

- **Claude Code as a model provider** (`claude -p`, all tools off, one call at a
  time); falls back to Gemini/Groq when logged out or at the plan limit.
- **Bengaluru internships**: Greenhouse/Lever/Ashby company boards (official
  APIs), SimplifyJobs India, Bengaluru news queries, `/add <link>` (never
  LinkedIn). Internshala/Unstop stay disabled (no scraping).
- **Pipeline** found → shortlisted → preparing → applied → OA → interviews →
  offer/rejected/ghosted/withdrawn (migration 0006) with history, recruiter-email
  stage proposals you confirm, OA/interview reminders and a company prep plan,
  14-day nudges, weekly funnel and what's-working insights.
- **Fit scores** from the model on a verified profile, India-student eligibility,
  a ghost-listing detector, daily quota shortlist and streak in the brief.
- **Tailored drafts**: cover letter, why-me, resume bullets traceable to your
  resume, an ATS keyword score and a one-page resume PDF.
- **Custom Telegram agents** (migration 0007): one BotFather bot each, tokens
  encrypted with `NOESIS_SECRET_KEY`, owner-only, fixed tool allow-list,
  schedules, memory, voice notes (Groq Whisper). Templates include an Interview
  Coach for voice-note mock interviews. The built-in Assistant answers unmapped
  main-bot text. See [AGENTS](AGENTS.md).
- **Phone calls** (`phone.call_owner`, the call-me settings): off by default,
  quiet hours 22:00-08:00, 3 a day, 30 min apart, only for interview-in-1h,
  last-day deadlines, agent requests and `/call test`; Telegram fallback. See
  [PHONE](PHONE.md).
- **Google Calendar sync** (`calendar.owner_event`): off by default, events on
  your own calendar only, never guests.
- **Extras**: referral finder (search links and a drafted note; NOESIS never
  messages anyone), drive radar (Google STEP, Engage, WoW, GRiD, Code for Good,
  GS India), company dossier, debrief question bank, offer comparison.
- **Dashboard** rebuilt around five pages (Home, Internships, Applications,
  Agents, Settings) plus Prep & people; light/dark; phone tab bar.
- **GCP e2-micro deploy** (`deploy/gcp/`): systemd with memory caps, 2 GB swap,
  Tailscale-only dashboard, nightly backup to Cloud Storage, a health check that
  messages you on Telegram. See [GCP](GCP.md).
- Fix: a 6-character ref made only of digits was read as a list number.

**Not live-tested yet**: Claude Code on a VM, Twilio/Telnyx calls, Groq speech
to text, Wikipedia and Google News for dossiers, the company-board APIs and
their slugs, Google Calendar writes, a real GCE VM, Tailscale serve and
`gcloud storage`. Telnyx support follows its TeXML docs and has had the least testing.

## Summary (v1)

All ten bots, the runtime, dashboard, Telegram command centre, policies and
deployment are implemented and pass 103 automated tests. The Docker stack builds
and runs healthy. **No outside service has been called live yet**, so every
integration is "implemented, tested with fakes, waiting for your first real run".

## Works and is tested (automated)

- Vault: two opportunities with the same title in the same month get separate notes (`… (2).md`) instead of failing `process_verification`.

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
- Brands: `config/brands.yaml` (cue.notch → LinkedIn, msthinks → Instagram).
  `/work <what you did>` and the Monday plan make one post per brand per
  channel, in that brand's voice and the platform's format; publishing through
  Zernio (free for 2 accounts), Postiz or the direct LinkedIn/Instagram APIs
  (LinkedIn video upload, Instagram reel upload without a public media host);
  post caps per account.
- Cue reels: `config/brands.yaml` brand `cue` posts on @msthinks's Instagram
  with cue.notch invited as a collaborator (Zernio `platformSpecificData.collaborators`,
  per the Zernio docs; not yet seen working live). `noesisctl.py reels FOLDER`
  queues finished reels 3 hours apart (09:00-23:00), skipping `posted: true`;
  `refill_reels` (hourly) renders a new reel from an unused script (starter bank,
  then the model) using recorded Cue/Tock footage, Motion-kit SFX and fonts.
  @msthinks has its own caps (5/day, 30/week) in `account_caps`.
- Reel renderer v2: blurred full-frame backdrop, word-by-word captions, xfade
  cuts (the old fade-from-black flashed at every cut), voiceover from
  ElevenLabs or Gemini TTS (`integrations/video/voice.py`; both tested with
  fakes only; the owner's ElevenLabs free tier is out of credits).
- Template drafts (written without the model) carry the `template_draft` flag
  and always need your approval, after two went out live on 16 Oct.
- ntfy: every owner notification also pushes its headline to `NTFY_TOPIC`
  (headline only on public ntfy.sh); `noesisctl.py test-ntfy`. Tested with a fake
  server only.
- Daily project-idea scout (07:30): GitHub new repos, Hugging Face trending,
  Show HN, arXiv → vault note `02 Knowledge/Project Ideas` and the morning
  brief; `/ideas` on demand. Failed sources are listed in the note.
- Applications: email submission through Gmail with verified answers and the
  resume attached; an opportunity you confirmed is sent as soon as it is
  complete (rule *approved-by-me*, max 5 a day). Web forms stay manual.
- Communications: inbox triage, drafts; sending off by policy.
- Knowledge: article capture into the vault, social links queued for your
  local NOESIS pipeline.
- Dashboard (17 pages) reading real DB state; API auth with CSRF header.

## Implemented, not yet live-tested

Telegram, Gmail, Google Calendar, LinkedIn, Instagram, Postiz, the idea-scout APIs, the six opportunity
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
