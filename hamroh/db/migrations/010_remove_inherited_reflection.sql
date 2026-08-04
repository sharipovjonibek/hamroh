-- Retire scheduled rows left by the removed inherited reflection workflow.
-- Historical sent/cancelled rows remain intact as audit history.
UPDATE reminders
SET status = 'cancelled'
WHERE auto_seed_key = 'self-reflection-default'
  AND status IN ('pending', 'processing');
