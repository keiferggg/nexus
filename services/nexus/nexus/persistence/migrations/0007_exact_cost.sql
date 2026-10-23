-- Exact cost: providers report how many input tokens were served from their prompt cache (billed at a lower rate);
-- prices gain that rate, calls record the cached count. Provider balance snapshots allow checking NEXUS's computed
-- spend against what the provider actually deducted.
ALTER TABLE model_prices ADD COLUMN cached_input_per_mtok_usd REAL;
ALTER TABLE model_calls ADD COLUMN cached_input_tokens INTEGER;

CREATE TABLE provider_balances (
  id TEXT PRIMARY KEY,
  provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
  currency TEXT NOT NULL,
  total_balance REAL NOT NULL,
  raw_json TEXT NOT NULL,
  taken_at TEXT NOT NULL
);
CREATE INDEX provider_balances_by_provider ON provider_balances(provider_id, taken_at);
