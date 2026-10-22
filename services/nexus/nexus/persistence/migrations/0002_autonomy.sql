-- Autonomy: connected services, real actions, objective discovery, agent-created tools.
-- Rebuilds `tasks` to admit the new kinds 'act' and 'discover' (SQLite cannot alter a CHECK);
-- foreign keys are disabled by the migration runner around this script and verified afterwards.

CREATE TABLE tasks_new (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('plan','research','write','code','review','integrate','check','act','discover')),
  description TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','ready','leased','running','submitted','verifying','accepted','blocked','failed','canceled','superseded')),
  status_reason TEXT,
  mission_revision INTEGER NOT NULL,
  role TEXT NOT NULL,
  agent_id TEXT REFERENCES agent_identities(id),
  acceptance_json TEXT NOT NULL DEFAULT '[]',
  verification_plan_json TEXT NOT NULL DEFAULT '[]',
  strategy_json TEXT NOT NULL DEFAULT '{}',
  cause_json TEXT NOT NULL DEFAULT '{}',
  priority REAL NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  fencing_seq INTEGER NOT NULL DEFAULT 0,
  epoch INTEGER NOT NULL DEFAULT 1,
  is_stale INTEGER NOT NULL DEFAULT 0,
  stale_reason TEXT,
  current_artifact_id TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (mission_id, key)
);
INSERT INTO tasks_new SELECT * FROM tasks;
DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;
CREATE INDEX idx_tasks_mission_status ON tasks(mission_id, status);

-- Owner-configured connections to external services. Credentials live in the OS store.
CREATE TABLE connectors (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('http','github','mcp')),
  label TEXT NOT NULL,
  config_json TEXT NOT NULL,
  secret_ref TEXT,
  status TEXT NOT NULL DEFAULT 'unvalidated',
  status_detail TEXT,
  validated_at TEXT,
  created_at TEXT NOT NULL
);

-- Discovered/declared actions with per-action autonomy chosen by the owner.
CREATE TABLE connector_actions (
  id TEXT PRIMARY KEY,
  connector_id TEXT NOT NULL REFERENCES connectors(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  input_schema_json TEXT NOT NULL,
  effect_class TEXT NOT NULL CHECK (effect_class IN ('read_only','local_write','external_write','destructive')),
  autonomy TEXT NOT NULL CHECK (autonomy IN ('deny','ask','auto')),
  hints_json TEXT NOT NULL DEFAULT '{}',
  max_auto_per_day INTEGER NOT NULL DEFAULT 50,
  enabled INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL,
  UNIQUE (connector_id, name)
);

-- Every real action an agent asks for. NEXUS (not the agent) executes it.
CREATE TABLE action_requests (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  mission_id TEXT,
  task_id TEXT,
  attempt_id TEXT,
  requested_by TEXT NOT NULL,
  connector_id TEXT NOT NULL REFERENCES connectors(id),
  action TEXT NOT NULL,
  args_json TEXT NOT NULL,
  rationale TEXT NOT NULL,
  effect_class TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','approved','denied','executing','succeeded','failed','uncertain','expired','canceled')),
  approval TEXT,                      -- 'auto:policy' | 'owner' | null
  decided_by TEXT,
  decision_note TEXT,
  idempotency_key TEXT NOT NULL,
  result_json TEXT,
  error TEXT,
  evidence_id TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT,
  executed_at TEXT
);
CREATE INDEX idx_action_requests_task ON action_requests(task_id);
CREATE INDEX idx_action_requests_status ON action_requests(status);

-- Objectives discovered by agents; each must serve an existing owner goal.
CREATE TABLE objective_proposals (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_mission_id TEXT,
  source_task_id TEXT,
  proposed_by TEXT NOT NULL,
  title TEXT NOT NULL,
  objective TEXT NOT NULL,
  success_conditions_json TEXT NOT NULL,
  constraints_json TEXT NOT NULL DEFAULT '[]',
  rationale TEXT NOT NULL,
  serves_goal TEXT NOT NULL,          -- id of an owner goal in projects.settings_json.goals
  evidence_json TEXT NOT NULL DEFAULT '[]',
  est_cost_usd REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('proposed','adopted','rejected','expired')),
  decided_by TEXT,
  decision_note TEXT,
  mission_id TEXT,
  dedup_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  decided_at TEXT
);

-- Tools created by agents. Candidates until tested and approved.
CREATE TABLE tool_definitions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('composite','python')),
  description TEXT NOT NULL,
  input_schema_json TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  tests_json TEXT NOT NULL,
  effect_class TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('candidate','test_failed','tested','active','rejected','retired')),
  status_detail TEXT,
  test_report_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL,
  approved_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (project_id, name, version)
);
