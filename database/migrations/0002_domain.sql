-- Domain tables for specialist bots.

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,                   -- rss | html | json | fixture
  url TEXT NOT NULL,
  access_method TEXT NOT NULL,          -- documented access method
  interval_s REAL NOT NULL,
  parser_json TEXT NOT NULL DEFAULT '{}',
  official_domains_json TEXT NOT NULL DEFAULT '[]',
  default_type TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  reliability TEXT NOT NULL DEFAULT 'unknown', -- unknown | ok | degraded | failing
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  etag TEXT,
  last_modified TEXT,
  last_checked REAL,
  last_success REAL,
  last_error TEXT,
  next_check REAL,
  items_seen INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS opportunities (
  id TEXT PRIMARY KEY,
  fingerprint TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  organizer TEXT,
  official_url TEXT,
  discovery_source TEXT NOT NULL,
  discovery_url TEXT,
  type TEXT NOT NULL DEFAULT 'unknown',
  location TEXT,
  remote_status TEXT NOT NULL DEFAULT 'unknown',
  deadline REAL,                        -- UTC epoch; NULL = unknown
  deadline_text TEXT,
  deadline_tz TEXT,
  eligibility_json TEXT NOT NULL DEFAULT '[]',
  required_materials_json TEXT NOT NULL DEFAULT '[]',
  estimated_effort TEXT,
  relevance_score REAL,
  relevance_rationale TEXT,
  summary TEXT,
  discovered_at REAL NOT NULL,
  last_verified REAL,
  verification_status TEXT NOT NULL DEFAULT 'unverified', -- unverified | verified | failed | needs_review
  verification_notes_json TEXT NOT NULL DEFAULT '[]',
  eligibility_status TEXT NOT NULL DEFAULT 'unknown',     -- unknown | eligible | ineligible | needs_info
  application_status TEXT NOT NULL DEFAULT 'DISCOVERED',
  notified INTEGER NOT NULL DEFAULT 0,
  note_path TEXT,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_opp_deadline ON opportunities(deadline);

CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY,
  opportunity_id TEXT UNIQUE NOT NULL,
  state TEXT NOT NULL,                  -- DISCOVERED..CLOSED
  answers_json TEXT NOT NULL DEFAULT '{}',
  missing_json TEXT NOT NULL DEFAULT '[]',
  documents_json TEXT NOT NULL DEFAULT '[]',
  submission_method TEXT,
  confirmation_id TEXT,
  evidence TEXT,
  follow_up_at REAL,
  history_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS content_items (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  platform TEXT NOT NULL,               -- linkedin | instagram
  format TEXT NOT NULL,                 -- post | reel | carousel
  category TEXT NOT NULL,
  state TEXT NOT NULL,                  -- IDEA..PERFORMANCE_REVIEW, plus BLOCKED
  brief_json TEXT NOT NULL DEFAULT '{}',
  script_json TEXT NOT NULL DEFAULT '{}',
  body TEXT,
  assets_json TEXT NOT NULL DEFAULT '[]',
  claims_json TEXT NOT NULL DEFAULT '[]',
  sources_json TEXT NOT NULL DEFAULT '[]',
  content_hash TEXT,
  scheduled_for REAL,
  published_at REAL,
  platform_id TEXT,
  publication_url TEXT,
  metrics_json TEXT NOT NULL DEFAULT '{}',
  history_json TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS emails (
  id TEXT PRIMARY KEY,                  -- provider message id
  thread_id TEXT,
  sender TEXT,
  subject TEXT,
  snippet TEXT,
  received_at REAL,
  state TEXT NOT NULL,                  -- RECEIVED..TRACKED
  category TEXT,
  priority TEXT,
  actions_json TEXT NOT NULL DEFAULT '[]',
  draft TEXT,
  follow_up_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
  id TEXT PRIMARY KEY,
  email TEXT UNIQUE,
  name TEXT,
  organization TEXT,
  role TEXT,
  source_url TEXT,
  relevance REAL,
  relevance_rationale TEXT,
  status TEXT NOT NULL DEFAULT 'prospect', -- prospect | drafted | contacted | replied | declined | opted_out | bounced
  last_contacted REAL,
  follow_ups_sent INTEGER NOT NULL DEFAULT 0,
  next_follow_up REAL,
  notes TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach_messages (
  id TEXT PRIMARY KEY,
  contact_id TEXT NOT NULL,
  kind TEXT NOT NULL,                   -- initial | follow_up
  subject TEXT,
  body TEXT,
  status TEXT NOT NULL,                 -- draft | approved | sent | blocked
  sent_at REAL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS suppression_list (
  email TEXT PRIMARY KEY,
  reason TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  due_at REAL NOT NULL,
  source TEXT,
  ref_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending | sent | done | cancelled
  dedupe_key TEXT UNIQUE,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_captures (
  id TEXT PRIMARY KEY,
  url TEXT,
  url_hash TEXT UNIQUE,
  title TEXT,
  status TEXT NOT NULL,                 -- captured | processed | failed
  note_path TEXT,
  summary TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
