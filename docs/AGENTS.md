# Custom agents

A custom agent is your own Telegram bot with one job: a morning internship scout,
an interview coach that drills you every evening, an inbox summariser. You create
it in the dashboard (**Agents → New agent**) in about a minute.

## How it works

- **One BotFather bot per agent.** Create it with `/newbot` in @BotFather and paste
  the token. NOESIS checks it with Telegram (`getMe`) and stores it encrypted with
  `NOESIS_SECRET_KEY` (Fernet). The token is never shown again, in the dashboard,
  the API, logs or anywhere else.
- **Only you.** Each bot answers only your numeric `TELEGRAM_OWNER_ID` in a private
  chat. Anyone else who finds the bot gets silence (and an audit row).
- **One job, your words.** The task you write is its standing instruction. It can't
  change its task, tools, schedule or token; only you can, in the dashboard.
- **Only the tools you tick**, from a fixed list. Every tool goes through the same
  policies as the rest of NOESIS. None can message anyone but you: email tools make
  Gmail **drafts**, stage changes are **suggestions** you confirm with a tap.
- **Schedules.** `every 6h`, `at 08:30`, `at 19:00 on mon,thu` (shortest every 15m),
  or none (it only answers you). A scheduled run that has nothing worth saying stays quiet.
- **Memory.** It remembers your last few exchanges. `/forget` in its chat (or *Clear
  its memory* in the dashboard) wipes them.
- **Voice notes.** Send a voice note (up to 5 minutes): it is transcribed with Groq's
  Whisper (`GROQ_API_KEY`) and deleted. Made for the Interview Coach's mock interviews.
- **Changes apply within 30 seconds**, no restart: the `agents` service reloads the
  list, and every message reads the agent's current settings.

The main NOESIS bot has a built-in **Assistant** on the same engine: anything you
type there that isn't a command or a known job goes to it. Turn it off or change
its job and tools on the Agents page.

## Templates

| Template | Job | Schedule |
|---|---|---|
| 🔭 Scout | the 5 best new Bengaluru / remote-India internships each morning, with a one-line pitch | at 08:30 |
| 📝 Apply | pick, tailor, prepare from verified facts, keep stages current | none |
| 🧑‍💼 Assistant | reminders, email drafts, notes, quick questions | none |
| 🎤 Interview Coach | one DSA / system-design question each evening, graded; `mock <company>` | at 19:00 |
| 📬 Inbox | summarise recruiter mail, draft replies, flag stage changes and deadlines | every 6h |

## Tools

| Tool | What it does |
|---|---|
| create_reminder | sets a reminder for you |
| search_opportunities / today_shortlist | reads your internship list, quota and streak |
| add_opportunity_url | adds a posting from a link (LinkedIn needs the pasted text; it's never opened) |
| draft_email | a Gmail draft you review and send yourself |
| save_note | a note in your vault (`00 Inbox/Agents/…`) |
| set_stage | suggests a stage change; you confirm with a button in the main chat |
| company_dossier | your data + Wikipedia + Google News headlines, summarised |
| tailor_resume | starts resume tailoring for a listing (results in the main chat) |
| find_referrals | LinkedIn / Google search links and a note you send yourself |
| log_debrief / question_bank | your interview question bank |
| call_me | rings your phone (policies/phone.yaml decides; see [PHONE](PHONE.md)) |
| ask_main_agent | hands a request to the Chief of Staff (never bounces back) |

## Troubleshooting

- **The bot doesn't answer.** Agents page: is it on, and is there a red error? Check
  that the `agents` service runs (`systemctl status noesis-agents`, or
  `docker compose ps agents`). Press Start in the bot's chat once.
- **"Telegram rejected this bot's token".** It was revoked in @BotFather: paste the new one (Edit → Replace bot token).
- **"can't be decrypted".** `NOESIS_SECRET_KEY` changed since the token was saved. Restore the old key or paste the tokens again.
- **"That's the main NOESIS bot's token".** Each agent needs its own bot; two pollers on one token would steal each other's messages.
