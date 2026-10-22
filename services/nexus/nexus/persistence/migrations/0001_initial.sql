-- NEXUS initial schema. Canonical state for missions, tasks, acceptance, evidence, budgets.
-- Search indexes (memory_fts) and summaries are rebuildable projections.

CREATE TABLE settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  workspace_root TEXT NOT NULL,
  is_demo INTEGER NOT NULL DEFAULT 0,
  settings_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE providers (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('anthropic','openai','ollama','fake')),
  label TEXT NOT NULL,
  base_url TEXT,
  secret_ref TEXT,                 -- opaque reference into the OS credential store; never the key
  status TEXT NOT NULL DEFAULT 'unvalidated',
  status_detail TEXT,
  validated_at TEXT,
  models_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

-- Versioned, owner-configured pricing. Provider APIs do not return prices.
CREATE TABLE model_prices (
  id TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
  model TEXT NOT NULL,
  input_per_mtok_usd REAL NOT NULL,
  output_per_mtok_usd REAL NOT NULL,
  source TEXT NOT NULL,            -- e.g. 'owner-configured 2026-10-21'
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (provider_id, model, version)
);

CREATE TABLE agent_identities (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = owner-wide shared identity
  name TEXT NOT NULL,
  charter TEXT NOT NULL,           -- stable identity description
  current_version_id TEXT,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE TABLE agent_versions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL REFERENCES agent_identities(id) ON DELETE CASCADE,
  version INTEGER NOT NULL,
  role_default TEXT NOT NULL,
  model_policy_json TEXT NOT NULL,     -- {"provider_id":..., "model":..., "max_output_tokens":...}
  tool_permissions_json TEXT NOT NULL, -- list of capability names
  memory_scope TEXT NOT NULL DEFAULT 'private',
  created_at TEXT NOT NULL,
  UNIQUE (agent_id, version)
);

CREATE TABLE missions (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','planning','running','paused','canceling','canceled','completed','failed','blocked')),
  current_revision INTEGER NOT NULL DEFAULT 1,
  execution_mode TEXT NOT NULL CHECK (execution_mode IN ('offline_fake','live')),
  spend_cap_usd REAL,
  max_active_workers INTEGER NOT NULL DEFAULT 4,
  max_branches INTEGER NOT NULL DEFAULT 2,
  max_revision_rounds INTEGER NOT NULL DEFAULT 3,
  strategy TEXT NOT NULL DEFAULT 'direct',
  status_reason TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE mission_revisions (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL,
  objective TEXT NOT NULL,
  success_conditions_json TEXT NOT NULL,
  constraints_json TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (mission_id, revision)
);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  title TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('plan','research','write','code','review','integrate','check')),
  description TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','ready','leased','running','submitted','verifying','accepted','blocked','failed','canceled','superseded')),
  status_reason TEXT,
  mission_revision INTEGER NOT NULL,
  role TEXT NOT NULL,
  agent_id TEXT REFERENCES agent_identities(id),
  acceptance_json TEXT NOT NULL DEFAULT '[]',        -- acceptance criteria
  verification_plan_json TEXT NOT NULL DEFAULT '[]', -- per-criterion check, evidence, verifier, independence
  strategy_json TEXT NOT NULL DEFAULT '{}',          -- selected mode, rationale, switch trigger
  cause_json TEXT NOT NULL DEFAULT '{}',             -- why this task exists (objective/uncertainty/failure ref)
  priority REAL NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  fencing_seq INTEGER NOT NULL DEFAULT 0,
  epoch INTEGER NOT NULL DEFAULT 1,                  -- bumps when a task is reopened (stale/repair)
  is_stale INTEGER NOT NULL DEFAULT 0,
  stale_reason TEXT,
  current_artifact_id TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (mission_id, key)
);
CREATE INDEX idx_tasks_mission_status ON tasks(mission_id, status);

CREATE TABLE dependencies (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  depends_on_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  waived INTEGER NOT NULL DEFAULT 0,
  waived_by TEXT,
  PRIMARY KEY (task_id, depends_on_task_id)
);

CREATE TABLE task_attempts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL,
  fencing_token INTEGER NOT NULL,
  epoch INTEGER NOT NULL,
  mission_revision INTEGER NOT NULL,
  agent_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('running','submitted','completed','failed','abandoned','canceled','superseded','fenced')),
  workflow_name TEXT NOT NULL,
  resumed_from_checkpoint TEXT,
  error TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  UNIQUE (task_id, attempt_no)
);

CREATE TABLE leases (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
  holder TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL,   -- wall clock epoch ms
  heartbeat_at TEXT NOT NULL
);

CREATE TABLE checkpoint_refs (
  id TEXT PRIMARY KEY,
  attempt_id TEXT NOT NULL REFERENCES task_attempts(id) ON DELETE CASCADE,
  workflow_name TEXT NOT NULL,
  checkpoint_id TEXT NOT NULL,
  pending_request_ids_json TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL,              -- 'recorded' | 'reconciled'
  recorded_at TEXT NOT NULL,
  UNIQUE (attempt_id, checkpoint_id)
);

-- Recorded intents bridge the framework checkpoint store and the domain DB.
CREATE TABLE intents (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  attempt_id TEXT REFERENCES task_attempts(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('recorded','applied','rejected')),
  created_at TEXT NOT NULL,
  applied_at TEXT
);

CREATE TABLE artifacts (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  mission_id TEXT REFERENCES missions(id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  kind TEXT NOT NULL,                 -- deliverable | verification_result | context_packet | source | diff | plan
  title TEXT NOT NULL,
  media_type TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  mission_revision INTEGER,
  input_versions_json TEXT NOT NULL DEFAULT '{}',
  lineage_json TEXT NOT NULL DEFAULT '{}',
  supersedes_id TEXT REFERENCES artifacts(id),
  access TEXT NOT NULL DEFAULT 'project',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_artifacts_task ON artifacts(task_id);

CREATE TABLE submissions (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  attempt_id TEXT NOT NULL REFERENCES task_attempts(id),
  fencing_token INTEGER NOT NULL,
  epoch INTEGER NOT NULL,
  mission_revision INTEGER NOT NULL,
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  input_versions_json TEXT NOT NULL DEFAULT '{}',
  meta_json TEXT NOT NULL DEFAULT '{}',
  round INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL CHECK (status IN ('received','verifying','accepted','rejected','stale','inconclusive')),
  status_reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE reviews (
  id TEXT PRIMARY KEY,
  submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  criterion TEXT,
  check_kind TEXT NOT NULL,
  reviewer TEXT NOT NULL,            -- agent id or 'nexus:deterministic' or 'owner'
  independence TEXT NOT NULL,        -- deterministic | independent_agent | same_lineage | owner
  verdict TEXT NOT NULL CHECK (verdict IN ('pass','fail','inconclusive')),
  blocking INTEGER NOT NULL DEFAULT 1,
  findings_json TEXT NOT NULL DEFAULT '{}',
  raw_artifact_id TEXT REFERENCES artifacts(id),
  limitations TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE acceptances (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  epoch INTEGER NOT NULL,
  submission_id TEXT NOT NULL UNIQUE REFERENCES submissions(id),
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  sha256 TEXT NOT NULL,
  mission_revision INTEGER NOT NULL,
  input_versions_json TEXT NOT NULL,
  review_ids_json TEXT NOT NULL,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  accepted_by TEXT NOT NULL,         -- always application code / owner, never the worker
  is_current INTEGER NOT NULL DEFAULT 1,
  noncurrent_reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (task_id, epoch)            -- at most one acceptance per task epoch: no double acceptance
);

-- Durable application event log; also the transactional outbox (rows written in the same
-- transaction as the state change they describe). seq is the resumable stream cursor.
CREATE TABLE events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT NOT NULL UNIQUE,
  project_id TEXT,
  mission_id TEXT,
  task_id TEXT,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  causal_parent TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_events_mission ON events(mission_id, seq);

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,  -- dedup key for at-least-once delivery
  project_id TEXT NOT NULL,
  mission_id TEXT,
  task_id TEXT,
  thread_id TEXT NOT NULL,
  sender TEXT NOT NULL,
  recipients_json TEXT NOT NULL,
  causal_parent TEXT,
  type TEXT NOT NULL CHECK (type IN ('question','proposal','evidence','objection','task_offer','task_claim','handoff','review_request','decision','blocker','status')),
  schema_version INTEGER NOT NULL DEFAULT 1,
  body TEXT NOT NULL,
  refs_json TEXT NOT NULL DEFAULT '{}',
  expects_response TEXT,             -- e.g. 'ack' | 'decision' | null
  deadline TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_messages_thread ON messages(thread_id, created_at);

CREATE TABLE message_deliveries (
  message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  recipient TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','delivered','acked','expired')),
  attempts INTEGER NOT NULL DEFAULT 0,
  delivered_at TEXT,
  acked_at TEXT,
  PRIMARY KEY (message_id, recipient)
);

-- ---- shared understanding ----------------------------------------------------------------
CREATE TABLE evidence (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  mission_id TEXT REFERENCES missions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('source','observation','test_result','calculation','owner_statement')),
  title TEXT NOT NULL,
  locator TEXT,                      -- url / path / description
  artifact_id TEXT REFERENCES artifacts(id),
  excerpt TEXT,
  lineage_root TEXT NOT NULL,        -- canonical source path; same root = not independent
  status TEXT NOT NULL CHECK (status IN ('active','withdrawn','changed')),
  status_reason TEXT,
  version INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE claims (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  mission_id TEXT REFERENCES missions(id) ON DELETE CASCADE,
  proposition TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('proposed','supported','disputed','superseded','withdrawn')),
  confidence_basis TEXT NOT NULL,    -- textual evidence basis, never a model decimal
  observed_at TEXT NOT NULL,
  valid_until TEXT,
  is_stale INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE claim_evidence (
  claim_id TEXT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
  evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  relation TEXT NOT NULL CHECK (relation IN ('supports','contradicts')),
  PRIMARY KEY (claim_id, evidence_id)
);

CREATE TABLE assumptions (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  statement TEXT NOT NULL,
  basis TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','confirmed','falsified','retired')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE uncertainties (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('missing_evidence','conflicting_evidence','ambiguous_intent','execution','explanation')),
  question TEXT NOT NULL,
  would_change TEXT NOT NULL,        -- what could change if resolved
  affects_json TEXT NOT NULL DEFAULT '[]',
  cost_hint TEXT NOT NULL DEFAULT 'medium',
  urgency TEXT NOT NULL DEFAULT 'normal',
  status TEXT NOT NULL CHECK (status IN ('open','resolved','accepted_risk')),
  resolution TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE decisions (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  choice TEXT NOT NULL,
  rationale TEXT NOT NULL,
  owner TEXT NOT NULL,
  alternatives_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL CHECK (status IN ('active','superseded','stale')),
  created_at TEXT NOT NULL
);

CREATE TABLE proposals (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('proposal','objection')),
  target_ref TEXT,                   -- proposal/task/artifact the objection is about
  summary TEXT NOT NULL,
  mechanism TEXT,                    -- for objections: why it would fail
  evidence_or_test TEXT,
  resolution_condition TEXT NOT NULL,
  decision_owner TEXT NOT NULL,
  blocking INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('open','resolved','rejected','escalated')),
  resolution TEXT,
  addresses_ref TEXT,                -- objective/uncertainty/assumption/prediction this addresses
  created_by TEXT NOT NULL,
  message_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE predictions (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  statement TEXT NOT NULL,
  observable TEXT NOT NULL,          -- machine-checkable observation key
  expected_json TEXT NOT NULL,
  observed_json TEXT,
  status TEXT NOT NULL CHECK (status IN ('open','confirmed','surprised','void')),
  consequential INTEGER NOT NULL DEFAULT 1,
  diagnosis TEXT,                    -- 'execution_failure' | 'false_assumption' | null
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Provenance / dependency edges for targeted invalidation.
CREATE TABLE dep_edges (
  src_type TEXT NOT NULL,
  src_id TEXT NOT NULL,
  dst_type TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  PRIMARY KEY (src_type, src_id, dst_type, dst_id)
);
CREATE INDEX idx_dep_edges_src ON dep_edges(src_type, src_id);

CREATE TABLE recheck_requests (
  id TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  cause TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','scheduled','done')),
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_recheck_pending ON recheck_requests(target_type, target_id) WHERE status = 'pending';

-- ---- tools, providers, budgets ------------------------------------------------------------
CREATE TABLE tool_operations (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  project_id TEXT NOT NULL,
  mission_id TEXT,
  task_id TEXT,
  attempt_id TEXT,
  actor TEXT NOT NULL,
  tool TEXT NOT NULL,
  effect_class TEXT NOT NULL CHECK (effect_class IN ('read_only','local_write','external_write','destructive')),
  args_json TEXT NOT NULL,           -- redacted
  status TEXT NOT NULL CHECK (status IN ('requested','denied','running','succeeded','failed','uncertain','reconciled')),
  decision_reason TEXT,
  result_json TEXT,
  idempotency_key TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE model_calls (
  id TEXT PRIMARY KEY,
  operation_id TEXT NOT NULL UNIQUE,
  project_id TEXT,
  mission_id TEXT,
  task_id TEXT,
  attempt_id TEXT,
  agent_id TEXT,
  provider_id TEXT NOT NULL,
  provider_kind TEXT NOT NULL,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('reserved','running','succeeded','failed','uncertain','canceled')),
  error_kind TEXT,
  error_detail TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  usage_reported INTEGER NOT NULL DEFAULT 0,
  reservation_id TEXT,
  actual_usd REAL,
  context_artifact_id TEXT,
  output_artifact_id TEXT,           -- recorded output; replays reuse it instead of re-calling
  started_at TEXT NOT NULL,
  ended_at TEXT
);

CREATE TABLE budget_reservations (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  category TEXT NOT NULL CHECK (category IN ('model','tool','retrieval','verification','learning')),
  reserved_usd REAL NOT NULL,
  actual_usd REAL,
  status TEXT NOT NULL CHECK (status IN ('reserved','reconciled','released','unresolved')),
  basis TEXT NOT NULL,               -- how the estimate was computed
  ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX idx_budget_mission ON budget_reservations(mission_id, status);

CREATE TABLE permission_grants (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  subject TEXT NOT NULL,             -- agent id or '*'
  capability TEXT NOT NULL,
  scope_json TEXT NOT NULL DEFAULT '{}',
  granted_by TEXT NOT NULL,          -- must be 'owner'
  created_at TEXT NOT NULL,
  revoked_at TEXT
);

-- ---- memory & learning -------------------------------------------------------------------
CREATE TABLE memory_items (
  id TEXT PRIMARY KEY,
  project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = owner-wide
  layer TEXT NOT NULL CHECK (layer IN ('working','episodic','semantic','procedural','mission','identity')),
  access TEXT NOT NULL CHECK (access IN ('private','team','project','owner')),
  owner_agent_id TEXT,
  mission_id TEXT,
  content TEXT NOT NULL,
  source_refs_json TEXT NOT NULL DEFAULT '[]',
  circumstances TEXT,
  status TEXT NOT NULL CHECK (status IN ('active','disputed','superseded','withdrawn')),
  version INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE memory_fts USING fts5(content, item_id UNINDEXED, tokenize='porter unicode61');

CREATE TABLE memory_patches (
  id TEXT PRIMARY KEY,
  target_id TEXT,
  base_version INTEGER,
  project_id TEXT,
  proposal_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('proposed','committed','rejected')),
  validation_json TEXT NOT NULL DEFAULT '{}',
  proposer TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE skill_versions (
  id TEXT PRIMARY KEY,
  skill_name TEXT NOT NULL,
  version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('candidate','evaluating','rejected','canary','promoted','rolled_back','retired')),
  target TEXT NOT NULL,              -- procedure | retrieval | routing | coordination | verification | planning
  hypothesis TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  conditions TEXT NOT NULL,
  content_json TEXT NOT NULL,
  parent_version_id TEXT,
  eval_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (skill_name, version)
);

CREATE TABLE opportunities (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  mission_id TEXT REFERENCES missions(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('unblocked_work','stale_evidence','repeated_procedure','recurring_failure')),
  goal_ref TEXT NOT NULL,            -- must reference an existing owner goal
  rationale TEXT NOT NULL,
  cost_estimate_usd REAL NOT NULL,
  scope TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('open','approved','executed','dismissed','expired')),
  dedup_key TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
