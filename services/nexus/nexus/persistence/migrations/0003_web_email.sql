-- Built-in web connector (writes to any public URL) and email (SMTP) connectors.
-- Rebuilds `connectors` to widen the kind CHECK; foreign keys are toggled by the migration runner.

CREATE TABLE connectors_new (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('http','github','mcp','web','email')),
  label TEXT NOT NULL,
  config_json TEXT NOT NULL,
  secret_ref TEXT,
  status TEXT NOT NULL DEFAULT 'unvalidated',
  status_detail TEXT,
  validated_at TEXT,
  created_at TEXT NOT NULL
);
INSERT INTO connectors_new SELECT * FROM connectors;
DROP TABLE connectors;
ALTER TABLE connectors_new RENAME TO connectors;
CREATE UNIQUE INDEX idx_connectors_builtin_web ON connectors(project_id) WHERE kind = 'web';
