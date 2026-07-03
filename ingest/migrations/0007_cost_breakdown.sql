-- Per-capture LLM/API usage accounting (see src/llm_usage.py).
-- Aggregated token counts per (kind, model) + totals, written by the
-- worker after every capture attempt. Idempotent — applied on every
-- container start by src/migrate.py.
ALTER TABLE captures ADD COLUMN IF NOT EXISTS cost_breakdown JSONB;
