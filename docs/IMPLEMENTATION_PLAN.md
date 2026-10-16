# Implementation plan

The build followed the phases in the original brief. Each phase ended with
tests passing and a commit on `main`.

| Phase | Delivered | Evidence |
|---|---|---|
| 1. Inspect | Read the NOESIS vault structure read-only (frontmatter schema, folders, `noesis.py` conventions); never touched `.system/` | `integrations/obsidian/vault.py` follows that schema |
| 2. Design | ARCHITECTURE, SECURITY, this plan, STATUS, INTEGRATIONS | `docs/` |
| 3. Core runtime | SQLite schema + migrations, queue, bus, scheduler, registry, runtime, supervisor | `tests/unit/test_core.py` |
| 4. Policy and safety | Policy engine, action gateway, approvals, dry-run, pause, audit, secret redaction | `tests/security/test_policies.py` |
| 5. Model router | Tiers, capabilities, budgets, cache, deterministic fallbacks | budget and outage tests |
| 6. Bots | Ten agents with configs and prompts | `tests/integration/test_specialists.py`, `test_opportunity_scout.py` |
| 7. Interfaces | FastAPI control plane, 17-page dashboard, Telegram command centre, operator CLI | `tests/integration/test_api.py` |
| 8. Deployment | Dockerfile, Compose, Caddy, backups/restore, OAuth helper, source checker; guide walked through from a clean clone | DEPLOYMENT.md, TEST_REPORT #24 |
| 9. Review and hardening | Founder, customer-proxy and engineering reviews; all findings implemented: notification store with quiet hours and brief, buttons, confirm/skip/remind/outcome loop, profile facts from Telegram, pause/cap deferral, in-flight protection, zombie-thread limits, watchdog, housekeeping, retention, indexes | `tests/integration/test_owner_loop.py` |
| 10. Handover | README, CLAUDE.md, status, test report, bot guide, manual steps, costs, roadmap | this folder |

## Principles kept throughout

- Every external effect goes through one gateway with an audit row.
- Start safe: dry-run on, publishing and sending off.
- No fabricated facts: applications use verified profile data only.
- Nothing claimed as working without a test; live integrations are marked
  untested until you run them.
