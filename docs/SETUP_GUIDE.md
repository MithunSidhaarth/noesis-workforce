# Your setup guide

Everything NOESIS needs from you, in the order that unlocks the most. Each step
says what you get, what to do, and how to check it worked. Keys and tokens go
into `~/noesis-workforce/.env` on your PC, typed by you. Never paste them in a
chat, a note or a screenshot.

To edit `.env`, open Ubuntu and run `cd ~/noesis-workforce && nano .env`.
After any change to `.env`, run `docker compose up -d`.

| # | Step | Time | Cost | What it unlocks |
|---|---|---|---|---|
| 1 | Your profile and resume | 10 min | free | applications, eligibility checks |
| 2 | Gemini key | 3 min | free | real writing for posts, answers and ideas |
| 3 | Telegram bot | 5 min | free | your phone remote: approvals, `/work`, morning brief |
| 4 | Gmail and Calendar | 15 min | free | mail watching, replies flagged, email applications |
| 5 | Posting accounts | 10–40 min | free (Zernio) | auto-posting for cue.notch and msthinks |
| 6 | Go live | 2 min | free | real sends and posts |

---

## 1. Your profile and resume

**You get:** applications prepared from facts you confirmed. NOESIS never invents
anything, so without this it can only find opportunities, not apply.

Send these in the NOESIS thread (or put them in `config/profile.yaml` yourself):

- full name and the email you apply with
- degree and branch, college, graduation year, current year of study
- citizenship and location (city)
- a 2–3 sentence bio, in your own words
- why you want to work in AI (2–3 sentences)
- main skills and 2–3 projects (name, one line, link)
- links: GitHub, LinkedIn, portfolio

**Resume:** in Ubuntu, from wherever it is on Windows:

```bash
cd ~/noesis-workforce
docker compose exec api mkdir -p /data/documents
docker compose cp "/mnt/c/Users/Mithun Sidhaarth AM/Downloads/resume.pdf" api:/data/documents/resume.pdf
```

Then in `config/profile.yaml` under `documents:` add
`resume: {path: "documents/resume.pdf", verified: true}`.

**Check:** `/profile` on Telegram lists your facts; `/missing` shows nothing basic missing.

## 2. Gemini key (the writer)

**You get:** posts in each brand's voice, application answers drafted for your
review, and the daily project ideas written up. Without it you get plain templates.

1. Go to aistudio.google.com, sign in, click **Get API key → Create API key**.
2. In `.env`: `GEMINI_API_KEY=` followed by the key.

**Check:** `docker compose exec api python scripts/noesisctl.py doctor` no longer
warns about a model provider.

## 3. Telegram bot (your phone remote)

**You get:** approve/deny buttons, `/work`, `/ideas`, `/opportunities`, urgent
alerts and one morning brief.

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name. Copy the token.
2. Open **@userinfobot**, send anything, copy your numeric **Id**.
3. In `.env`: `TELEGRAM_BOT_TOKEN=` the token, `TELEGRAM_OWNER_ID=` the number.
4. Open your new bot and press **Start**.

**Check:** `docker compose exec api python scripts/noesisctl.py test-telegram`
messages you, and `/status` answers. Only your numeric id can command it.

## 4. Gmail and Calendar (mail watching + email applications)

**You get:** inbox checked every 10 minutes; replies from companies, interview
requests and anything important flagged to you; reply drafts in your Gmail
Drafts; applications that ask for an email sent from your Gmail.

1. console.cloud.google.com → create a project called `noesis`.
2. **APIs & Services → Library**: enable **Gmail API** and **Google Calendar API**.
3. **OAuth consent screen**: External, app name `NOESIS`, your email as support
   and developer contact. Add yourself under **Test users**.
4. Then click **Publish app** (it will say unverified; that's fine for your own
   account). In testing mode Google expires the sign-in every 7 days.
5. **Credentials → Create credentials → OAuth client ID → Desktop app**. Copy the
   client ID and secret into `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
6. Run the sign-in in Ubuntu:

   ```bash
   cd ~/noesis-workforce && docker compose up -d
   docker compose run --rm -p 127.0.0.1:8765:8765 -e OAUTH_BIND=0.0.0.0 -e OAUTH_PORT=8765 api python scripts/google_oauth.py
   ```

   Open the printed link in your browser, choose your account, click through the
   "unverified app" warning (Advanced → Go to NOESIS), and allow.
7. Copy the printed `GOOGLE_REFRESH_TOKEN=...` line into `.env`, then `docker compose up -d`.

**Check:** the dashboard's *Integrations* page shows Gmail; within 10 minutes your
inbox appears on the *Email* page. Sending stays off: replies are drafts until you
switch on `allow_send` in `policies/email.yaml`. Email applications are separate
(step 6).

## 5. Posting accounts (cue.notch on LinkedIn, msthinks on Instagram)

**You get:** every `/work <what you did>` and the Monday plan turns into a
LinkedIn post for cue.notch and an Instagram reel for msthinks, each in its own
voice, checked by the Quality bot, capped at 1 post a day per account.

First, write one line each in `config/brands.yaml`: what the brand is about
(`about`), who it's for (`audience`), and adjust `voice` if you like.

Then pick one way to connect the accounts:

### Option A: Zernio (free for 2 accounts, recommended)

Zernio holds the logins; NOESIS sends it the finished post and reel. Free for the
first two connected accounts with unlimited posts, which is exactly cue.notch
and msthinks. No developer apps.

1. Sign up at zernio.com.
2. Connect **LinkedIn** as cue.notch (personal profile or company page).
3. Connect **Instagram** as msthinks. It must be a Professional account
   (Instagram app → Settings → Account type → Switch to professional); no
   Facebook Page needed.
4. Copy each account's id into `config/brands.yaml` (`zernio_account:`).
5. Dashboard → **API keys** → create a read & write key; put it in `.env` as
   `ZERNIO_API_KEY`.

### Option A2: Postiz ($29/month)

Same idea, paid: connect the channels on postiz.com, copy each channel id into
`postiz_integration:`, and put `POSTIZ_API_KEY` in `.env`.

### Option B: direct APIs (free, more setup)

- **LinkedIn (personal profile only):** developer.linkedin.com → Create app (it
  needs a LinkedIn Page to attach to) → Products: add **Share on LinkedIn** →
  Auth tab: generate a token with `w_member_social` and `openid profile`.
  `.env`: `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_AUTHOR_URN=urn:li:person:<your id>`.
  The token expires every 60 days. Posting as a company *page* needs LinkedIn's
  Community Management approval, which is slow; use Option A for that.
- **Instagram:** msthinks must be a Professional account linked to a Facebook
  Page (Instagram → Settings → Account type; then link the Page in Meta Business
  Suite). developers.facebook.com → Create app (type Business) → add **Facebook
  Login for Business**. In **Graph API Explorer**, pick the app and generate a
  user token with `instagram_basic`, `instagram_content_publish`,
  `pages_show_list`, `pages_read_engagement`; extend it to a long-lived token
  (Access Token Debugger → Extend). Get the account id with
  `me/accounts?fields=instagram_business_account`.
  `.env`: `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`. The token lasts about 60 days.

Option B posts only to the one LinkedIn profile and one Instagram account in
`.env`, so it works for your split but not for more accounts later.

**Check:** dry-run is still on, so send `/work tested the setup guide` and watch
`/content` and the dashboard's *External actions*: you should see "would have
published" entries for both brands.

## 6. Go live

When the dry-run log looks right for a few days:

1. **Posting:** in `policies/publishing.yaml` set `enabled: true` and put your
   accounts in `accounts_allowlist` (`zernio:<account id>` for Option A, `postiz:<channel id>` for A2, or
   `linkedin:urn:li:person:…` / `instagram:<user id>` for Option B).
2. **Auto-apply:** already set up as *approved-by-me*: when you tap **Prepare**
   or send `/confirm <n>`, an application that goes by email is sent as soon as
   every answer comes from your verified profile (max 5 a day). Website forms
   (Unstop, Devpost, company portals) come back to you with the answers filled
   in: you submit, then `/submitted <ref> <confirmation>`. NOESIS never logs into
   those sites or gets past CAPTCHAs.
3. **Dry-run off:** dashboard *Settings*, or
   `docker compose exec api python scripts/noesisctl.py dry-run off`.
   `/pause` on Telegram stops everything instantly.

## Your daily rhythm

- **Morning:** brief on Telegram: new opportunities, deadlines, mail that
  matters, and today's trending project ideas (full note in your vault under
  `02 Knowledge/Project Ideas`).
- **When you do something:** `/work shipped X, learned Y` → posts for both brands.
- **When something looks good:** tap **Prepare** (or `/confirm <n>`) → it applies
  or hands you a ready form.
- **Optional:** `/ideas` for a fresh idea scan, `/opportunities` for the shortlist.

## Optional extras

- `GITHUB_TOKEN` (github.com → Settings → Developer settings → fine-grained token,
  no permissions): avoids GitHub's rate limit for the idea scout.
- A Claude API key (`ANTHROPIC_API_KEY`, pay-as-you-go, separate from Claude
  Pro) for noticeably better LinkedIn posts and application answers.
