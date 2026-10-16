-- Owner feedback loop: batched notifications, owner-entered profile facts,
-- skip reasons and application outcomes.

CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,              -- dedupe: one notification per key
  bot_id TEXT NOT NULL,
  text TEXT NOT NULL,
  buttons_json TEXT,                     -- Telegram inline keyboard rows
  urgent INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,                  -- held | sent | digested | failed
  created_at REAL NOT NULL,
  sent_at REAL
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status, created_at);

CREATE TABLE IF NOT EXISTS profile_facts (
  key TEXT PRIMARY KEY,                  -- typed by the owner in Telegram/dashboard; verified because the owner stated it
  value TEXT NOT NULL,
  section TEXT NOT NULL DEFAULT 'facts', -- facts | answers
  sensitive INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  updated_by TEXT NOT NULL
);

ALTER TABLE opportunities ADD COLUMN skip_reason TEXT;
ALTER TABLE opportunities ADD COLUMN confirmed_by TEXT;
ALTER TABLE applications ADD COLUMN outcome TEXT;
