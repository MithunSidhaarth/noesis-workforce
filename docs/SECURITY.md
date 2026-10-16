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
| Emergency pause | `/pause`, dashboard or CLI; blocks every external action at the gateway. Bots keep doing internal work and queued work is preserved | `test_emergency_pause_blocks_external_actions` |
| No duplicate sends/posts | idempotency key per action; duplicate returns the earlier result | `test_duplicate_messages_do_not_duplicate_external_actions` |
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
| A bot process crashes | supervisor restarts it with exponential backoff (2 s → 5 min); its leased task is reclaimed by the scheduler and retried. Verified by killing a bot process in the running stack. |
| Server reboot | `restart: unless-stopped`; all state is in `data/noesis.db`; queued work resumes. |
| Provider outage | task retries with backoff and dead-letters after `max_attempts`; you get one Telegram alert; nothing is marked successful. |
| Credential revoked | task fails without retry, integration shows **revoked** in the dashboard, you get an alert. Re-authorize, then retry the dead task from the Tasks page. |
| Something is misbehaving | `/pause` in Telegram (or Settings → Pause all bots). Queued work is kept. Investigate in Logs & audit, then `/resume`. |
| Bad deploy | roll back (docs/DEPLOYMENT.md §12). |
| Data loss | restore the nightly backup (docs/DEPLOYMENT.md §10). |
| Leaked secret | rotate it at the provider, update `.env`, `docker compose up -d`. For the dashboard: new password hash and a new `NOESIS_SESSION_SECRET` (this signs out every session). |

## Known limitations

- Single password, no second factor. Put the dashboard behind Tailscale or
  Cloudflare Access if you want an extra layer (see roadmap).
- SQLite is shared by all containers on one host; this is not a multi-host design.
- `style-src`/`script-src` allow `'unsafe-inline'` because the static Next.js
  export uses inline bootstrap scripts.
