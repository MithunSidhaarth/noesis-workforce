# Security and recovery guide

Security is enforced in code. Prompts tell bots how to behave; the controls
below hold even if a model ignores its prompt.

## Controls and where they are enforced

| Control | Enforcement | Test |
|---|---|---|
| Owner-only dashboard, no signup | PBKDF2-SHA256 (390k iterations) password hash in env; HMAC-signed, 7-day, `HttpOnly` + `Secure` + `SameSite=Strict` cookie (`services/api/auth.py`) | `test_everything_else_requires_login`, `test_session_cookie_flags_and_tamper` |
| Brute-force protection | 5 failed logins per IP per 15 min → 429 | `test_wrong_password_rate_limited` |
| CSRF | every non-GET needs `X-Noesis-Request: 1` on top of SameSite=Strict | `test_mutations_need_csrf_header` |
| HTTPS | Caddy with automatic certificates, HSTS; only Caddy publishes ports | verified with the compose stack (internal cert on `localhost`) |
| Security headers | CSP, `X-Frame-Options: DENY`, `nosniff`, `no-referrer` | `test_security_headers` |
| Telegram owner only | numeric `TELEGRAM_OWNER_ID` **and** private chat; usernames ignored | `test_telegram_rejects_unauthorized_users` |
| Per-bot tool grants | `ActionGateway` blocks any action not in the bot's `tools` | `test_bot_without_grant_cannot_send` |
| Bots cannot approve | approval state is read only from the `approvals` table; `approval_granted` in a request is stripped; bots cannot send `APPROVAL_GRANTED/DENIED` | `test_agents_cannot_self_assert_approval` |
| Bots cannot change policy | policies are YAML files in an image directory owned by root and read-only to the runtime user; containers run with `read_only: true`, `cap_drop: ALL`, `no-new-privileges`; no shell tool exists | by construction |
| Prompt injection | external text is wrapped in `<untrusted>` with a preamble; model output can only *propose*, the gateway decides | `test_prompt_injection_cannot_override_permissions` |
| Mandatory approvals | payments, purchases, legal declarations, sensitive disclosures, unusual/irreversible commitments, unverified facts (`policies/global.yaml`) | `test_email_policy_blocks_unauthorized_send_and_requires_approval` |
| Dry-run | global switch, on by default; only Telegram messages to you and vault writes are exempt | `test_dry_run_prevents_real_side_effects` |
| Emergency pause | `/pause`, dashboard or CLI; blocks every external action at the gateway except messages to you. The task that hit the pause waits and resumes after `/resume`; nothing is marked done | `test_emergency_pause_blocks_external_actions`, `test_paused_work_waits_and_resumes_instead_of_being_dropped` |
| Daily caps | a cap (posts, emails, outreach) defers the task to the next day instead of dropping it | `test_daily_cap_defers_until_tomorrow` |
| No duplicate sends/posts | idempotency key per action; only a real success is final (a dry-run rehearsal does not block the real run later) | `test_duplicate_messages_do_not_duplicate_external_actions`, `test_dry_run_rehearsal_does_not_block_the_real_run_later` |
| No blind re-sends after a crash | the key is reserved as `executing` before the provider is called. A concurrent attempt waits; an attempt interrupted mid-call (crash, timeout) becomes `unknown` for email, outreach, publishing, submissions and invites, and you are asked to check instead of it being sent again | `test_interrupted_send_is_never_blindly_repeated` |
| Owner actions by button | Telegram inline buttons (approve/deny, prepare/remind/skip) pass the same numeric-owner + private-chat check as typed commands; every tap is audited | `test_approval_is_pushed_with_buttons_and_decided_by_tap` |
| Owner-typed facts | `/profile set` facts count as verified because you typed them; sensitive keys are masked in replies and their values never go to the audit log | `test_profile_facts_typed_by_owner_are_used_and_refresh_applications` |
| Messaging abuse | sender permissions, topic ACLs, hop limit 12, same edge ≤ 6 times per chain, 256 KB payload cap | `test_message_schema_and_permissions`, `test_loop_detection_stops_ping_pong` |
| Runaway delegation | `max_delegation_depth` per bot | `test_delegation_depth_limit` |
| Runaway cost | per-bot call and USD limits, global daily USD cap | `test_usage_budgets_prevent_unbounded_model_calls` |
| Audit trail | every action outcome in `external_actions` + `audit_log` | `test_every_external_action_has_auditable_result` |
| Respectful fetching | robots.txt, per-host throttle, conditional GET, 5 MB cap, honest User-Agent; 401/403/451 stops rather than evades | `integrations/web/fetcher.py` |

## Secrets

- Live only in `.env` on the server (`chmod 600`), never in Git (`.gitignore`)
  or the dashboard bundle (it calls same-origin `/api` and holds no keys).
- Logs and database error text pass through `redact()` (Telegram bot tokens,
  bearer tokens, `key=`/`token=`/`client_secret=` parameters, Anthropic, Groq,
  Google keys and refresh tokens), and HTTP client libraries log at WARNING so
  request URLs are not written (`test_secrets_are_redacted_from_logs_and_errors`).
- Notes the bots write to Obsidian contain opportunity, knowledge and summary
  content only; no code path writes credentials there.
- OAuth scopes are minimal: Gmail `readonly` + `compose` (drafts), Calendar
  `readonly`. Sending and calendar writes are additionally off in policy.
- The vault's `.system/mcp_token` was never read or copied during the build.
- At runtime `.system/` is covered by an empty tmpfs in every container, and the
  vault adapter refuses any path with a hidden (dot) folder, so no bot can read
  or write the vault's tokens even when the real vault is mounted directly.

## Your vault

The Windows folder is never exposed. The server works on its own copy of the
vault, kept in sync by Syncthing (device-ID authenticated, end-to-end encrypted;
the admin GUI is bound to `127.0.0.1` and reached through an SSH tunnel). The
adapter refuses to edit any note it did not create (`generated_by:
noesis-workforce`), writes atomically, never leaves the vault root, and logs
every write to `99 System/Workforce/changelog.md`.

## Recovery

| Situation | What happens / what to do |
|---|---|
| A bot process crashes | supervisor restarts it with exponential backoff (2 s → 5 min); the restarted process hands back its unfinished tasks at once (stable worker id). Verified by killing a bot process in the running stack. |
| A bot hangs | the supervisor kills a bot process whose heartbeat is older than 3 minutes and restarts it; the container healthcheck uses the *oldest* bot heartbeat. A task that times out keeps its concurrency slot until its thread really ends; too many of those restarts the process. |
| Work gets stuck | approvals expire after 72 h (`NOESIS_APPROVAL_TTL_H`) and the waiting task is cancelled; a schedule skipped 3 times in a row alerts you; a bot that dies after startup alerts you once a day. |
| Database grows | nightly retention: logs 30 days, processed messages 14, finished tasks 90, notifications 60, model usage 400; `audit_log` and `external_actions` are kept as the evidence trail. `noesisctl.py prune` runs it now. |
| An action's outcome is unknown | the dashboard shows a red banner and the morning brief lists it. Check the platform (sent folder, post, form confirmation), then decide; it is never retried automatically. |
| Server reboot | `restart: unless-stopped`; all state is in `data/noesis.db`; queued work resumes. |
| Provider outage | task retries with backoff and dead-letters after `max_attempts`; you get one Telegram alert; nothing is marked successful. |
| Credential revoked | task fails without retry, integration shows **revoked** in the dashboard, you get an alert. Re-authorize, then retry the dead task from the Tasks page. |
| Something is misbehaving | `/pause` in Telegram (or Settings → Pause all bots). Work that needs an external action waits; you still get alerts. Investigate in Logs & audit, then `/resume`. |
| Not sure what's wrong | `docker compose exec api python scripts/noesisctl.py doctor` prints every check with what to fix. |
| Bad deploy | roll back (docs/DEPLOYMENT.md §12). |
| Data loss | restore the nightly backup (docs/DEPLOYMENT.md §10). |
| Leaked secret | rotate it at the provider, update `.env`, `docker compose up -d`. For the dashboard: new password hash and a new `NOESIS_SESSION_SECRET` (this signs out every session). |

## Known limitations

- Single password, no second factor. Put the dashboard behind Tailscale or
  Cloudflare Access if you want an extra layer (see roadmap).
- SQLite is shared by all containers on one host; this is not a multi-host design.
- `style-src`/`script-src` allow `'unsafe-inline'` because the static Next.js
  export uses inline bootstrap scripts.
