-- v2.0: custom Telegram agents (one bot per BotFather token) and the extra
-- features: referrals, interview question bank, offers, drive radar, phone calls,
-- calendar sync, daily quota.

CREATE TABLE IF NOT EXISTS custom_agents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  emoji TEXT NOT NULL DEFAULT '🤖',
  template TEXT,                          -- scout | apply | assistant | coach | inbox | NULL (blank)
  telegram_token_enc TEXT,                -- Fernet(NOESIS_SECRET_KEY); NULL for the built-in assistant (main bot)
  telegram_username TEXT,
  token_hint TEXT,                        -- "123456:••••" for display; the token itself is never returned
  task TEXT NOT NULL,                     -- the job description the owner wrote
  tools_json TEXT NOT NULL DEFAULT '[]',  -- subset of the fixed allow-list
  schedule TEXT,                          -- "every 6h" | "at 09:00" | NULL
  model_tier TEXT NOT NULL DEFAULT 'standard',
  enabled INTEGER NOT NULL DEFAULT 1,
  builtin INTEGER NOT NULL DEFAULT 0,
  updates_offset INTEGER,
  next_run REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  last_active REAL,
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS custom_agent_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  role TEXT NOT NULL,                     -- owner | agent
  text TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cam_agent ON custom_agent_messages(agent_id, id);

-- people the owner found to ask for a referral (the owner looks them up and messages them)
CREATE TABLE IF NOT EXISTS referrals (
  id TEXT PRIMARY KEY,
  opportunity_id TEXT,
  company TEXT NOT NULL,
  name TEXT,
  profile_url TEXT,
  relation TEXT,                          -- alumni | employee | friend
  status TEXT NOT NULL DEFAULT 'to_contact', -- to_contact | messaged | replied | referred | declined
  note_draft TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

-- post-interview debriefs: the owner's personal question bank
CREATE TABLE IF NOT EXISTS interview_questions (
  id TEXT PRIMARY KEY,
  opportunity_id TEXT,
  company TEXT,
  stage TEXT,
  question TEXT NOT NULL,
  topic TEXT,
  my_answer_notes TEXT,
  went_well INTEGER,
  asked_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_iq_company ON interview_questions(company);

CREATE TABLE IF NOT EXISTS offers (
  id TEXT PRIMARY KEY,
  opportunity_id TEXT,
  company TEXT NOT NULL,
  role TEXT,
  stipend_month_inr INTEGER,
  duration_months REAL,
  location TEXT,
  mode TEXT,                              -- onsite | hybrid | remote
  ppo TEXT,                               -- yes | likely | unknown | no
  learning INTEGER,                       -- 1..5, the owner's own rating
  deadline_to_accept REAL,
  notes TEXT,
  created_at REAL NOT NULL
);

-- yearly hiring programmes (config/drives.yaml): what was announced when
CREATE TABLE IF NOT EXISTS drive_state (
  id TEXT PRIMARY KEY,                    -- drive id from config/drives.yaml
  last_alert_key TEXT,
  page_hash TEXT,
  page_checked_at REAL,
  page_changed_at REAL
);

-- owner phone calls (policies/phone.yaml): every attempt, for caps and evidence
CREATE TABLE IF NOT EXISTS phone_calls (
  id TEXT PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  reason TEXT NOT NULL,
  message TEXT NOT NULL,
  status TEXT NOT NULL,                   -- queued | held | placed | failed | blocked
  provider_id TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  placed_at REAL
);

-- calendar events NOESIS created on the owner's own calendar (deadlines, OAs, interviews)
CREATE TABLE IF NOT EXISTS calendar_sync (
  key TEXT PRIMARY KEY,                   -- opp:<id>:deadline | stage:<id>:<stage>:<ts>
  event_id TEXT,
  start_at REAL NOT NULL,
  title TEXT NOT NULL,
  synced_at REAL NOT NULL
);
