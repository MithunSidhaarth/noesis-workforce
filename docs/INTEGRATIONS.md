# Integrations and setup checklist

Every integration sits behind an interface in `integrations/`. When its
credentials are missing it shows **not configured**. When credentials are set
but no live call has worked yet, it shows **configured**. It only shows **ok**
after a real call succeeds, and a rejected credential shows **revoked**.
Tests use fakes and test doubles; none of these was exercised against the real
service from the build environment, whose network blocks them.

| Integration | Code | Live-tested? | Starts as | You need to |
|---|---|---|---|---|
| Telegram | `integrations/telegram/client.py`, `services/telegram/bot.py` | No (fake client in tests) | commands + alerts to you only | create a bot, set token + numeric owner id |
| Obsidian vault | `integrations/obsidian/vault.py` | Real vault inspected read-only; writes tested on a copy | writes only its own notes | sync a server copy (Syncthing) |
| Opportunity sources | `integrations/web/fetcher.py`, `config/sources/feeds.yaml` | No (all hosts blocked here) | read-only fetch | run `scripts/check_sources.py` on the VPS |
| Gmail | `integrations/gmail/client.py` | No | read + draft; send OFF in policy | Google Cloud OAuth client, run `scripts/google_oauth.py` |
| Google Calendar | same file (`CalendarClient`) | No | read busy times; writes OFF in policy | same OAuth |
| LinkedIn | `integrations/social/publishers.py` | No | publishing OFF in policy | LinkedIn developer app with `w_member_social` |
| Instagram | same file | No | publishing OFF; reels need a public video URL host (not built) | Business/Creator account + Meta app |
| Model providers | `workforce/model_router/router.py` | No | no provider configured, deterministic paths | optional API keys |
| FFmpeg reels | `integrations/video/render.py` | **Yes** (real 1080×1920 render + ffprobe check) | local render only | nothing |
| Browser automation | `integrations/browser/` (empty) | — | not used | nothing; APIs first by design |

## Checklist (do these in order, one at a time)

1. **Telegram.** Follow DEPLOYMENT.md §8. Confirm `noesisctl.py test-telegram`
   delivers a message and `/status` answers. The dashboard should then show *ok*.
   Then check the parts that only a real client can prove: run
   `noesisctl.py run orchestrator morning_brief` and confirm the brief arrives;
   send `/opportunities` and tap a *Prepare* or *Not for me* button on a card;
   approve something from `/inbox` with its button. Quiet hours
   (`NOESIS_QUIET_HOURS`, default 23:00–07:30) and the daily ping cap
   (`NOESIS_MAX_PINGS_PER_DAY`, default 8) hold non-urgent messages for the
   08:00 brief, so a silent phone at night is expected.
2. **Vault.** Follow DEPLOYMENT.md §9. Confirm a new note appears under
   `09 Opportunities/` on your computer after the first verified opportunity,
   and that `99 System/Workforce/changelog.md` lists it.
3. **Sources.** `docker compose exec api python scripts/check_sources.py`.
   Disable anything that fails in `config/sources/feeds.yaml` (`enabled:
   false`) or replace it. Add official sources you care about (college notice
   boards, company career RSS) using the same format.
4. **Profile.** Fill `config/profile.yaml` with facts you have checked and mark
   them `verified: true`, or send `/profile set <key> <value>` on Telegram
   (facts you type count as verified; sensitive ones are masked in replies and
   never written to the audit log). `/missing` shows what drafts are waiting on.
   The Applications Agent uses nothing else.
5. **Content guidelines.** Put your real creative rules and reel instructions
   in `prompts/content/guidelines.md`. Until you do, the Content Studio uses only
   neutral templates; none of your guidelines were invented.
6. **Gmail and Calendar (read + draft).**
   - Google Cloud Console → new project → enable the *Gmail API* and *Google
     Calendar API* → OAuth consent screen (External, add yourself as a test
     user) → Credentials → *OAuth client ID* → *Desktop app*.
   - On **your computer**: `GOOGLE_CLIENT_ID=… GOOGLE_CLIENT_SECRET=… python scripts/google_oauth.py`
   - Put the three values in the server `.env`, `docker compose up -d`.
   - Within 10 minutes the Email page fills and the integration shows *ok*.
     Drafts appear in your Gmail Drafts; nothing is sent.
   - Refresh tokens for apps in *Testing* status expire after 7 days. Publish
     the consent screen (no verification is needed for personal use with these
     scopes, but Google shows an "unverified app" warning you can accept) to keep
     the token.
7. **Model provider (optional).** Add `GEMINI_API_KEY` (Google AI Studio free
   tier) and/or `GROQ_API_KEY`. `ANTHROPIC_API_KEY` is pay-as-you-go and is
   **not** included in Claude Pro. Budgets in `config/models.yaml` and each
   bot's `model:` block cap spend.
8. **LinkedIn (optional).** Create an app at developer.linkedin.com, add the
   *Share on LinkedIn* product, obtain a member access token with
   `w_member_social`, and your `urn:li:person:…`. Set both in `.env`. Then add
   `linkedin:<urn>` to `accounts_allowlist` and set `enabled: true` in
   `policies/publishing.yaml` **only after** a dry-run post looks right.
9. **Instagram (optional).** Requires a Business or Creator account linked to a
   Facebook Page and a Meta app with `instagram_content_publish`. Text-only
   posting does not exist on Instagram; reels need a public HTTPS URL for the
   video, and no media host is built yet (see [ROADMAP](ROADMAP.md)).
10. **Turn things on gradually.** Keep dry-run on for a few days and read the
    *External actions* tab in Logs. Then turn dry-run off. Email sending,
    outreach sending and calendar writes stay off until you flip them in
    `policies/email.yaml` and `policies/outreach.yaml`, and each send still
    needs your approval unless you add an explicit auto rule.
