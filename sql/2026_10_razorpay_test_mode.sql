-- Real Razorpay Test Mode integration (Payment Links registry + card mandates
-- we debit ourselves). New TABLES (payment_links, mandates) come from
-- create_all on startup; this file only adds the one new column on an existing
-- table. Idempotent; run once against an existing database:
--
--     psql revenue_recovery -f sql/2026_10_razorpay_test_mode.sql
--
-- New scheduled_actions kinds (order_link_reminder, mandate_pdn, mandate_debit)
-- need nothing: scheduled_actions.kind is a plain string column.

-- A MandateRun that belongs to a card Mandate (app/autopay.py). NULL = a
-- Razorpay Subscription run, exactly as before.
ALTER TABLE mandate_runs ADD COLUMN IF NOT EXISTS mandate_id UUID;
CREATE INDEX IF NOT EXISTS ix_mandate_runs_mandate_id ON mandate_runs (mandate_id);
