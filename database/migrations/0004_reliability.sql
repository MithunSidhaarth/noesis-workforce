-- Reliability: indexes for hot paths, indexed approval lookups, a task
-- dependency table (replaces LIKE scans), and approval expiry.

CREATE INDEX IF NOT EXISTS idx_tasks_lease ON tasks(status, lease_expires);
CREATE INDEX IF NOT EXISTS idx_tasks_corr ON tasks(correlation_id);
CREATE INDEX IF NOT EXISTS idx_tasks_finished ON tasks(status, finished_at);
CREATE INDEX IF NOT EXISTS idx_messages_lease ON messages(status, lease_expires);
CREATE INDEX IF NOT EXISTS idx_messages_expiry ON messages(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_actions_count ON external_actions(action, created_at, status);
CREATE INDEX IF NOT EXISTS idx_exec_ts ON exec_log(ts);

ALTER TABLE approvals ADD COLUMN idempotency_key TEXT;
UPDATE approvals SET idempotency_key = json_extract(payload_json, '$.idempotency_key');
CREATE INDEX IF NOT EXISTS idx_approvals_key ON approvals(idempotency_key, status);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);

CREATE TABLE IF NOT EXISTS task_deps (
  task_id TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY (task_id, depends_on)
);
CREATE INDEX IF NOT EXISTS idx_task_deps_dep ON task_deps(depends_on);
INSERT OR IGNORE INTO task_deps(task_id, depends_on)
  SELECT t.id, j.value FROM tasks t, json_each(t.depends_on_json) j;

ALTER TABLE schedules ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0;
