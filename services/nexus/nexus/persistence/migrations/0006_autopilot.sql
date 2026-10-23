-- Autopilot: standing missions that start themselves (on a schedule, when a watched page changes, or continuously),
-- each run bounded by a per-run cap and a per-day spend limit. Runs are ordinary missions.
CREATE TABLE standing_missions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
  title TEXT NOT NULL,
  objective TEXT NOT NULL,
  success_conditions_json TEXT NOT NULL,
  constraints_json TEXT NOT NULL DEFAULT '[]',
  trigger_json TEXT NOT NULL,
  run_cap_usd REAL NOT NULL CHECK (run_cap_usd > 0),
  daily_cap_usd REAL NOT NULL CHECK (daily_cap_usd > 0),
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run_at TEXT,
  last_run_at TEXT,
  last_check_at TEXT,
  watch_state TEXT,
  status_note TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE standing_runs (
  mission_id TEXT PRIMARY KEY REFERENCES missions(id) ON DELETE CASCADE,
  standing_id TEXT NOT NULL REFERENCES standing_missions(id) ON DELETE CASCADE,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX standing_runs_by_standing ON standing_runs(standing_id, created_at);

-- chat messages gain the 'autopilot' kind (SQLite cannot alter a CHECK constraint: rebuild the table).
CREATE TABLE chat_messages_new (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('owner', 'nexus')),
  kind TEXT NOT NULL CHECK (kind IN ('text', 'mission', 'progress', 'attention', 'result', 'error', 'setup', 'autopilot')),
  content TEXT NOT NULL DEFAULT '',
  data_json TEXT NOT NULL DEFAULT '{}',
  mission_id TEXT REFERENCES missions(id) ON DELETE SET NULL,
  dedup TEXT,
  created_at TEXT NOT NULL
);
INSERT INTO chat_messages_new SELECT id, conversation_id, role, kind, content, data_json, mission_id, dedup, created_at
  FROM chat_messages;
DROP TABLE chat_messages;
ALTER TABLE chat_messages_new RENAME TO chat_messages;
CREATE INDEX chat_messages_conv ON chat_messages(conversation_id, created_at);
CREATE UNIQUE INDEX chat_messages_dedup ON chat_messages(conversation_id, dedup) WHERE dedup IS NOT NULL;
