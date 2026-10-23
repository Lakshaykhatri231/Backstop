-- The approach the agent takes with a customer on a call (app/customer_profile.py), stored on the call at its start.
-- Idempotent: safe to run more than once, before or after the first start of the new code.
ALTER TABLE voice_calls ADD COLUMN IF NOT EXISTS profile_json TEXT;
