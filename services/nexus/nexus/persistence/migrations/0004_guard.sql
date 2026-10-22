-- Unattended-operation guard: record when an attempt ingested content that looks like
-- instructions aimed at AI agents (possible prompt injection). Writes from an exposed attempt
-- fall back to owner approval even in unattended mode.
CREATE TABLE exposures (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  mission_id TEXT,
  task_id TEXT,
  attempt_id TEXT,
  source TEXT NOT NULL,          -- url / path / connector action
  evidence_id TEXT,
  score INTEGER NOT NULL,
  matches_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_exposures_attempt ON exposures(attempt_id);

-- Guard decisions for every action request evaluated in unattended mode (allowed, escalated or denied).
CREATE TABLE guard_decisions (
  id TEXT PRIMARY KEY,
  action_request_id TEXT,
  project_id TEXT NOT NULL,
  mission_id TEXT,
  task_id TEXT,
  attempt_id TEXT,
  decision TEXT NOT NULL CHECK (decision IN ('allow','escalate','deny')),
  checks_json TEXT NOT NULL,     -- each check: name, passed, detail
  created_at TEXT NOT NULL
);
CREATE INDEX idx_guard_attempt ON guard_decisions(attempt_id, decision);
