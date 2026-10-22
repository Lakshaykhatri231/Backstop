-- Manual schema changes for the recovery extensions (scheduler, mandate retry,
-- promise-to-pay, B2B receivables, voice). The project has no Alembic:
-- Base.metadata.create_all() creates NEW TABLES on startup but never adds
-- columns to existing tables or values to existing enum types, so those live
-- here. Run once against an existing database:
--
--     psql revenue_recovery -f sql/2026_10_recovery_extensions.sql
--
-- Every statement is idempotent. A fresh database needs none of the ALTERs
-- below that touch tables, because create_all builds them with the new columns.
-- (ALTER TYPE ... ADD VALUE cannot run inside a transaction block on older
-- Postgres, so this file deliberately has no BEGIN/COMMIT.)

-- ── Phase 0: channel actions on the agentaction enum ────────────────────────
ALTER TYPE agentaction ADD VALUE IF NOT EXISTS 'send_pre_debit_notice';
ALTER TYPE agentaction ADD VALUE IF NOT EXISTS 'send_payment_link';
ALTER TYPE agentaction ADD VALUE IF NOT EXISTS 'send_remandate_link';
ALTER TYPE agentaction ADD VALUE IF NOT EXISTS 'voice_call';

-- ── Phase 0: contact + consent on customers ─────────────────────────────────
ALTER TABLE customers ADD COLUMN IF NOT EXISTS phone VARCHAR;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS preferred_language VARCHAR NOT NULL DEFAULT 'hinglish';
-- (if an earlier run of this file already added the column with the old 'en' default)
ALTER TABLE customers ALTER COLUMN preferred_language SET DEFAULT 'hinglish';
ALTER TABLE customers ADD COLUMN IF NOT EXISTS voice_consent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE customers ADD COLUMN IF NOT EXISTS consent_at TIMESTAMP;

-- ── Phase 1: mandate retry sequencer ────────────────────────────────────────
-- New at-risk bucket on the single-row revenue ledger. (mandate_runs and
-- scheduled_actions are NEW tables - create_all builds them on startup.)
ALTER TABLE merchant_revenue_state ADD COLUMN IF NOT EXISTS at_risk_mandate DOUBLE PRECISION NOT NULL DEFAULT 0;

-- ── Phase 3: B2B receivables ────────────────────────────────────────────────
-- (business_accounts, invoices, payment_promises are NEW tables - create_all.)
ALTER TABLE merchant_revenue_state ADD COLUMN IF NOT EXISTS receivables_overdue DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE merchant_revenue_state ADD COLUMN IF NOT EXISTS receivables_collected DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE merchant_revenue_state ADD COLUMN IF NOT EXISTS receivables_written_off DOUBLE PRECISION NOT NULL DEFAULT 0;

-- ── Phase 4: voice recovery ─────────────────────────────────────────────────
-- (voice_calls is a NEW table - create_all. Customer contact/consent columns
-- were added in Phase 0 above.)

-- ── Phase 5: contact points, OTP, voice for mandate/orders ──────────────────
-- (contact_points and otp_challenges are NEW tables - create_all builds them
-- the first time the app starts.)
-- Back-fill: existing storefront customers that already have a phone from the
-- Phase-0 columns get a contact point, UNVERIFIED and WITHOUT consent (a phone
-- typed before the OTP rule proves nothing; consent can only be re-given by the
-- customer). Guarded so this file can be run before OR after the first start.
DO $$
BEGIN
  IF to_regclass('public.contact_points') IS NOT NULL THEN
    INSERT INTO contact_points (id, customer_id, phone, preferred_language, voice_consent, created_at, updated_at)
    SELECT gen_random_uuid(), c.id, c.phone, COALESCE(c.preferred_language, 'hinglish'), false, now(), now()
    FROM customers c
    WHERE c.phone IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM contact_points cp WHERE cp.customer_id = c.id);
  END IF;
END $$;
UPDATE customers SET voice_consent = false, consent_at = NULL WHERE voice_consent = true;
