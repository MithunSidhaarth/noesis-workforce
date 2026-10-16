# Manual steps (only you can do these)

These need your accounts, your identity or your judgement. Do them in order;
each one has a check so you know it worked. Details are in
[DEPLOYMENT](DEPLOYMENT.md) and [INTEGRATIONS](INTEGRATIONS.md).

## Required

1. **Get a machine.** Your own PC to start with ([WINDOWS](WINDOWS.md)), or the free Oracle Cloud ARM machine in private mode
   (no domain needed, follow [ORACLE](ORACLE.md)), or rent a VPS (Ubuntu 24.04,
   4 GB RAM recommended) and point a domain's A record at it.
   *Check:* you can SSH in.
2. **Clone the private repo on the VPS** (deploy key or a fine-grained token
   with read access to this one repo).
3. **Create `.env`** from `.env.example`. Generate the dashboard password hash
   with `python scripts/hash_password.py` and a random `NOESIS_SESSION_SECRET`.
   *Check:* `docker compose run --rm api python scripts/noesisctl.py doctor`.
4. **Create the Telegram bot** with @BotFather, put the token in `.env`, and
   put your **numeric** user id (from @userinfobot) in `TELEGRAM_OWNER_ID`.
   *Check:* `noesisctl.py test-telegram` reaches you, `/status` answers.
5. **Start the stack**: `docker compose up -d --build`.
   *Check:* `https://your.domain` shows the login page with a valid certificate.
6. **Fill your profile.** Copy `config/profile.example.yaml` to
   `config/profile.yaml` and enter only facts you have checked, marked
   `verified: true`, or send `/profile set <key> <value>` on Telegram later.
   The Applications bot uses nothing else and will never invent a fact.
7. **Run the source check**: `docker compose exec api python scripts/check_sources.py`,
   and disable sources that fail in `config/sources/feeds.yaml`.

## Recommended

8. **Vault sync.** Install Syncthing on your Windows PC and on the server,
   share only the NOESIS vault folder, and set `NOESIS_VAULT_PATH` on the
   server to the synced copy. Do not expose your Windows filesystem in any
   other way. Do not sync `.system/`.
9. **Content guidelines.** Write your own rules in
   `prompts/content/guidelines.md`. Until you do, content stays neutral.
10. **A free model key** (Gemini or Groq) in `.env` if you want better drafts.
11. **Copy backups off the server** now and then (`data/backups/`).

## Optional, when you are ready

12. **Gmail + Calendar**: Google Cloud OAuth client, then
    `scripts/google_oauth.py` on your own computer. Read and draft only.
13. **Turn off dry-run** after a few days of reading the *External actions*
    log: dashboard *Settings* or `noesisctl.py dry-run off`.
14. **LinkedIn / Instagram publishing**: developer apps, then the account
    allowlist and `enabled: true` in `policies/publishing.yaml`.
15. **Email sending or outreach**: flip the switches in `policies/email.yaml`
    and `policies/outreach.yaml`. Each send still asks for your approval.

## Things the system will always hand back to you

- Submitting web-form applications (it prepares, you submit, then
  `/submitted <ref> <confirmation>`).
- Anything involving payment, legal declarations, sensitive personal data,
  or a fact it cannot verify.
- Confirming that an opportunity found on an aggregator is real
  (*Prepare* button or `/confirm`).
- CAPTCHAs and logins: it never tries to get past them.
