-- Customer autopay page: who started a mandate and for which service. Idempotent.
-- (The new app_users table comes from create_all.)
ALTER TABLE mandates ADD COLUMN IF NOT EXISTS customer_id UUID;
ALTER TABLE mandates ADD COLUMN IF NOT EXISTS service_key VARCHAR;
ALTER TABLE mandates ADD COLUMN IF NOT EXISTS service_name VARCHAR;
CREATE INDEX IF NOT EXISTS ix_mandates_customer_id ON mandates (customer_id);
