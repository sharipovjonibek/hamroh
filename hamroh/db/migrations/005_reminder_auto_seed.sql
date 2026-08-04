-- Track reminders created from operator-owned configuration rather than
-- through the agent's reminder tools. The key identifies the declaration
-- that the startup reconciler should keep in sync.
ALTER TABLE reminders ADD COLUMN auto_seed_key TEXT;

CREATE INDEX IF NOT EXISTS idx_reminders_auto_seed_key
    ON reminders(auto_seed_key);
