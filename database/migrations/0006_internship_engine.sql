-- v2.0: Bengaluru internship engine. Company job boards, AI fit scores, and the
-- application pipeline from "found" to "selected".

CREATE TABLE IF NOT EXISTS company_boards (
  id TEXT PRIMARY KEY,                   -- board_<ats>_<slug>, also the sources.id it expands into
  company TEXT NOT NULL,
  ats TEXT NOT NULL,                     -- greenhouse | lever | ashby
  slug TEXT NOT NULL,
  domain TEXT,                           -- company website domain, counted as official
  interval TEXT NOT NULL DEFAULT '6h',
  enabled INTEGER NOT NULL DEFAULT 1,
  origin TEXT NOT NULL DEFAULT 'yaml',   -- yaml | dashboard
  last_test_items INTEGER,
  last_test_at REAL,
  created_at REAL NOT NULL,
  UNIQUE (ats, slug)
);

-- pipeline stage of each listing: found -> shortlisted -> preparing -> applied ->
-- online_assessment -> interview_1 -> interview_2 -> hr -> offer | rejected | ghosted | withdrawn | skipped
ALTER TABLE opportunities ADD COLUMN stage TEXT NOT NULL DEFAULT 'found';
ALTER TABLE opportunities ADD COLUMN stage_updated_at REAL;
ALTER TABLE opportunities ADD COLUMN next_event_at REAL;          -- scheduled OA / interview time, if known
ALTER TABLE opportunities ADD COLUMN ai_fit REAL;
ALTER TABLE opportunities ADD COLUMN ai_reasons_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE opportunities ADD COLUMN ai_red_flags_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE opportunities ADD COLUMN ai_eligible TEXT;            -- true | false | unknown
ALTER TABLE opportunities ADD COLUMN stipend TEXT;                -- as stated in the listing, never guessed
ALTER TABLE opportunities ADD COLUMN published_at REAL;
ALTER TABLE opportunities ADD COLUMN ghost_risk REAL;
ALTER TABLE opportunities ADD COLUMN ghost_reasons_json TEXT NOT NULL DEFAULT '[]';
CREATE INDEX IF NOT EXISTS idx_opp_stage ON opportunities(stage, stage_updated_at);
CREATE INDEX IF NOT EXISTS idx_opp_org ON opportunities(organizer);

CREATE TABLE IF NOT EXISTS application_stages (
  id TEXT PRIMARY KEY,
  opportunity_id TEXT NOT NULL,
  from_stage TEXT,
  stage TEXT NOT NULL,
  at REAL NOT NULL,
  event_at REAL,                         -- when the OA / interview happens, if given
  note TEXT,
  by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stages_opp ON application_stages(opportunity_id, at);

-- stage changes detected in recruiter email, waiting for the owner's one-tap confirmation
CREATE TABLE IF NOT EXISTS stage_proposals (
  id TEXT PRIMARY KEY,
  opportunity_id TEXT NOT NULL,
  email_id TEXT,
  stage TEXT NOT NULL,
  evidence TEXT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending | confirmed | dismissed
  created_at REAL NOT NULL,
  decided_at REAL,
  UNIQUE (opportunity_id, email_id, stage)
);

ALTER TABLE applications ADD COLUMN drafts_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE applications ADD COLUMN resume_version TEXT;

-- existing rows: put them where they already are in the pipeline
UPDATE opportunities SET stage = CASE
  WHEN skip_reason IS NOT NULL THEN 'skipped'
  WHEN application_status IN ('SUBMITTED','FOLLOW_UP') THEN 'applied'
  WHEN confirmed_by IS NOT NULL THEN 'preparing'
  ELSE 'found' END;
UPDATE opportunities SET stage = CASE a.outcome
  WHEN 'accepted' THEN 'offer' WHEN 'rejected' THEN 'rejected' WHEN 'no_response' THEN 'ghosted'
  WHEN 'withdrawn' THEN 'withdrawn' ELSE opportunities.stage END
  FROM applications a WHERE a.opportunity_id = opportunities.id AND a.outcome IS NOT NULL;
