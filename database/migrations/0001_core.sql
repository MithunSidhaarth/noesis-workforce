-- NOESIS Workforce core schema (SQLite, WAL mode).
-- Timestamps are UTC epoch seconds (REAL). JSON columns are TEXT.

CREATE TABLE IF NOT EXISTS bots (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  purpose TEXT NOT NULL DEFAULT '',
  config_json TEXT NOT NULL,            -- full validated BotConfig
  config_version TEXT NOT NULL,
  instructions_version TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL DEFAULT 'idle',   -- idle | busy | paused | error | offline
  current_task_id TEXT,
  last_heartbeat REAL,
  last_execution REAL,
  last_success REAL,
  tasks_succeeded INTEGER NOT NULL DEFAULT 0,
  tasks_failed INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,                 -- pending | queued | running | succeeded | failed | dead | cancelled | deferred | waiting_approval
  priority INTEGER NOT NULL DEFAULT 5,  -- lower = sooner
  workflow_id TEXT,
  parent_task_id TEXT,
  correlation_id TEXT,
  requested_by TEXT,                    -- bot id, 'user', 'scheduler', 'telegram', 'dashboard'
  delegation_depth INTEGER NOT NULL DEFAULT 0,
  depends_on_json TEXT NOT NULL DEFAULT '[]',
  idempotency_key TEXT UNIQUE,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  timeout_s REAL NOT NULL DEFAULT 300,
  run_after REAL NOT NULL,
  deadline REAL,
  lease_owner TEXT,
  lease_expires REAL,
  checkpoint_json TEXT,
  result_json TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks(bot_id, status, run_after, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_workflow ON tasks(workflow_id);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  sender TEXT NOT NULL,
  recipient TEXT,
  topic TEXT,
  type TEXT NOT NULL,
  workflow_id TEXT,
  correlation_id TEXT,
  parent_task_id TEXT,
  in_reply_to TEXT,
  priority INTEGER NOT NULL DEFAULT 5,
  payload_json TEXT NOT NULL DEFAULT '{}',
  expires_at REAL,
  response_type TEXT,
  hop_count INTEGER NOT NULL DEFAULT 0,
  dedupe_key TEXT UNIQUE,
  auth_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL,                 -- pending | delivered | processed | failed | dead | expired | rejected
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  lease_owner TEXT,
  lease_expires REAL,
  run_after REAL NOT NULL,
  error TEXT,
  created_at REAL NOT NULL,
  delivered_at REAL,
  processed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_messages_inbox ON messages(recipient, status, run_after, priority);
CREATE INDEX IF NOT EXISTS idx_messages_corr ON messages(correlation_id);
CREATE INDEX IF NOT EXISTS idx_messages_workflow ON messages(workflow_id);

CREATE TABLE IF NOT EXISTS subscriptions (
  bot_id TEXT NOT NULL,
  topic TEXT NOT NULL,
  PRIMARY KEY (bot_id, topic)
);

CREATE TABLE IF NOT EXISTS schedules (
  id TEXT PRIMARY KEY,                  -- '<bot_id>:<name>'
  bot_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,                   -- task kind to enqueue
  payload_json TEXT NOT NULL DEFAULT '{}',
  every_s REAL,                         -- interval schedule
  at_time TEXT,                         -- 'HH:MM' daily schedule in tz
  weekdays TEXT,                        -- e.g. 'mon,tue' (optional with at_time)
  tz TEXT NOT NULL DEFAULT 'Asia/Kolkata',
  allow_overlap INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run REAL,
  last_run REAL,
  last_task_id TEXT
);

CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL,                 -- running | succeeded | failed | cancelled
  requested_by TEXT,
  spec_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  action TEXT NOT NULL,
  summary TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  reasons_json TEXT NOT NULL DEFAULT '[]',
  task_id TEXT,
  status TEXT NOT NULL,                 -- pending | granted | denied | expired
  decided_by TEXT,
  decided_at REAL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS external_actions (
  id TEXT PRIMARY KEY,
  idempotency_key TEXT UNIQUE NOT NULL,
  bot_id TEXT NOT NULL,
  action TEXT NOT NULL,                 -- e.g. telegram.send, email.send, social.publish
  target TEXT,
  status TEXT NOT NULL,                 -- dry_run | succeeded | failed | blocked | approval_required
  dry_run INTEGER NOT NULL DEFAULT 0,
  request_json TEXT NOT NULL DEFAULT '{}',
  result_json TEXT,
  evidence TEXT,
  error TEXT,
  task_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  actor TEXT NOT NULL,
  event TEXT NOT NULL,
  correlation_id TEXT,
  details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

CREATE TABLE IF NOT EXISTS exec_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  level TEXT NOT NULL,
  bot_id TEXT,
  task_id TEXT,
  correlation_id TEXT,
  message TEXT NOT NULL,
  data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_bot ON exec_log(bot_id, ts);

CREATE TABLE IF NOT EXISTS system_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL,
  updated_by TEXT
);

CREATE TABLE IF NOT EXISTS model_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  bot_id TEXT NOT NULL,
  task_id TEXT,
  provider TEXT NOT NULL,
  model TEXT NOT NULL,
  capability TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  cached INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 1,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_ts ON model_usage(ts, bot_id);

CREATE TABLE IF NOT EXISTS model_cache (
  key TEXT PRIMARY KEY,
  response_json TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS integrations (
  name TEXT PRIMARY KEY,
  status TEXT NOT NULL,                 -- not_configured | ok | degraded | error | revoked | mock
  detail TEXT,
  last_ok REAL,
  last_error REAL,
  updated_at REAL NOT NULL
);

-- bot private key-value memory (scoped per bot)
CREATE TABLE IF NOT EXISTS bot_memory (
  bot_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value_json TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (bot_id, key)
);
