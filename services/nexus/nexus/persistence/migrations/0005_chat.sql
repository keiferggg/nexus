-- Chat: the default way to use NEXUS. A conversation is a thread of owner messages and NEXUS replies; replies can
-- carry a mission card (proposed or started), progress, approval requests and results for missions started from it.
CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE chat_messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('owner', 'nexus')),
  kind TEXT NOT NULL CHECK (kind IN ('text', 'mission', 'progress', 'attention', 'result', 'error', 'setup')),
  content TEXT NOT NULL DEFAULT '',
  data_json TEXT NOT NULL DEFAULT '{}',
  mission_id TEXT REFERENCES missions(id) ON DELETE SET NULL,
  dedup TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX chat_messages_conv ON chat_messages(conversation_id, created_at);
CREATE UNIQUE INDEX chat_messages_dedup ON chat_messages(conversation_id, dedup) WHERE dedup IS NOT NULL;

-- Which conversation a mission belongs to, so progress is posted back into the right thread.
CREATE TABLE conversation_missions (
  mission_id TEXT PRIMARY KEY REFERENCES missions(id) ON DELETE CASCADE,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);
