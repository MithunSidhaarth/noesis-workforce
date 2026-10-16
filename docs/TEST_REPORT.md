# Test report

Run: `python -m pytest -q` → **94 passed** (2026-10-16, Python 3.12, SQLite WAL).
Lint: `ruff check --select F .` → clean. Dashboard: `npm run build` → passes.

Automated tests use fakes for every outside service (`tests/helpers.py`:
`FakeTelegram`, fake fetcher, fake Gmail, fake publishers, fake model provider).
A pass here proves the logic, policies and state handling; it does **not**
prove that a real provider accepts the call.

| Suite | Tests |
|---|---|
| `tests/unit/test_core.py` (queue, bus, runtime, scheduler) | 18 |
| specialists (each bot's handlers) | 12 |
| API | 11 |
| opportunity scout end-to-end | 2 |
| owner loop (notifications, buttons, confirm/skip, profile, outcomes, housekeeping, reliability) | 21 |
| security and policies | 18 |
| specialists: LinkedIn video, Instagram reel upload | 2 |
| brands, Postiz, idea scout, email applications, source filter, Zernio (`test_brands_and_ideas.py`) | 10 |

## The 24 acceptance criteria

| # | Criterion | Evidence |
|---|---|---|
| 1 | Each bot runs independently | `test_each_bot_runs_independently` |
| 2 | Bots exchange structured messages | `test_structured_message_exchange_and_delegation`, `test_message_schema_and_permissions` |
| 3 | Delegate a task and receive the result | `test_structured_message_exchange_and_delegation` |
| 4 | Multi-step workflow respects dependencies | `test_dependencies_respected`, `test_dead_dependency_cancels_dependents`, `test_orchestrator_builds_dependency_graph` |
| 5 | Independent jobs run concurrently within limits | `test_concurrency_within_limits`, `test_timed_out_task_still_counts_against_concurrency` |
| 6 | Restart does not lose jobs | `test_restart_does_not_lose_jobs`, `test_restarted_worker_hands_back_its_tasks` |
| 7 | Duplicate messages do not duplicate external actions | `test_duplicate_messages_do_not_duplicate_external_actions`, `test_message_dedupe`, `test_interrupted_send_is_never_blindly_repeated` |
| 8 | Failed integration: visible failure, bounded retry | `test_bounded_retry_then_dead_letter_visible`, `test_model_outage_defers_not_fakes` |
| 9 | Disabled bot gets no new work | `test_disabled_bot_gets_no_new_work`, `test_bot_disable_enable` |
| 10 | Revoked credential gives a clear error | `test_revoked_credential_is_clear_and_not_retried` |
| 11 | Unauthorized Telegram users cannot run commands | `test_telegram_rejects_unauthorized_users`, `test_approval_is_pushed_with_buttons_and_decided_by_tap` (button taps pass the same owner check) |
| 12 | Prompt injection cannot override permissions | `test_prompt_injection_cannot_override_permissions`, `test_agents_cannot_self_assert_approval` |
| 13 | Scout discovers, verifies, deduplicates | `test_scout_end_to_end`, `test_duplicates_are_caught_across_types` |
| 14 | Applications use only verified profile data | `test_applications_use_only_verified_profile_data`, `test_profile_facts_typed_by_owner_are_used_and_refresh_applications` |
| 15 | Dry-run prevents all real side effects | `test_dry_run_prevents_real_side_effects`, `test_dry_run_rehearsal_does_not_block_the_real_run_later` |
| 16 | Publishing policies prevent disallowed posts | `test_publishing_policy_prevents_disallowed_posts`, `test_content_pipeline_renders_real_reel_and_gates_publication` |
| 17 | Email policies prevent unauthorized sending | `test_email_policy_blocks_unauthorized_send_and_requires_approval`, `test_communications_classifies_drafts_and_never_sends_without_policy` |
| 18 | Pause blocks new external actions | `test_emergency_pause_blocks_external_actions`, `test_paused_work_waits_and_resumes_instead_of_being_dropped` |
| 19 | Obsidian writes preserve content, avoid duplicates | `test_obsidian_preserves_user_notes_and_avoids_duplicates` |
| 20 | Dashboard shows real DB state | `test_overview_reflects_real_db`; dashboard also rendered against a live stack |
| 21 | Recovers after a worker crash | `test_worker_crash_recovery`; also a real `docker kill` of the worker container, tasks resumed |
| 22 | Every external action has an auditable result | `test_every_external_action_has_auditable_result` |
| 23 | Usage budgets prevent unbounded model calls | `test_usage_budgets_prevent_unbounded_model_calls`, `test_daily_cap_defers_until_tomorrow` |
| 24 | Deployment guide works from a clean environment | Manual: fresh clone → DEPLOYMENT.md steps 3–7 in Docker; first attempt found two `.env.example` bugs (fixed), second attempt passed; repeated after the latest changes: 4 services healthy plus Caddy running, worker ≈380 MB |

(Exact test names: `grep -rn "def test_" tests/`.)

## Manual checks done in the build environment

- Docker Compose stack built and started; `api`, `worker`, `scheduler` and
  `telegram` healthchecks green, `caddy` running (it has no healthcheck); `noesisctl.py doctor` ran.
- A real FFmpeg reel (1080×1920) rendered and verified with ffprobe on the
  host. (The sandbox image used for the smoke test has no FFmpeg; the
  production `Dockerfile` installs it.)
- The morning brief task ran in the live stack and succeeded.
- The smoke test found false "bot offline" alerts right after startup; fixed
  with a startup grace period and covered by `test_offline_alert_only_for_a_bot_that_died_after_startup`.

- ARM64 (Oracle Ampere A1 target): the image was built for `linux/arm64` under
  emulation; the suite ran inside it with 81 passed and 1 skipped (the reel
  test, because that build had no ffmpeg). The API and dashboard started.
- Private mode (`docker-compose.private.yml`): the stack ran with no Caddy; the
  API was bound to 127.0.0.1 only and unreachable from outside; `doctor`
  reported private mode with 0 problems.

- Own-PC setup (docs/WINDOWS.md): the vault was mounted from a path with
  spaces through `NOESIS_VAULT_HOST`; it was writable from the containers and
  its `.system/` folder was empty inside them (tmpfs overlay). Docker Desktop on
  Windows itself was not available here.

## Not tested live (network blocked from the build environment)

Telegram (messages and buttons), Gmail, Google Calendar, LinkedIn, Instagram,
Postiz, the idea-scout APIs (GitHub, Hugging Face, HN Algolia, arXiv),
all six opportunity sources, all model providers, Syncthing, Let's Encrypt
certificates, optional Redis wake-ups. See [INTEGRATIONS](INTEGRATIONS.md) for
the first-run checklist. None of these should be considered working until that
checklist passes on your server.
