-- Repair session identifiers if an earlier deployment recorded migration 020
-- without successfully adding the columns. Safe to run when either column exists.
ALTER TABLE customer_paper_accounts ADD COLUMN IF NOT EXISTS auto_session_id TEXT;
ALTER TABLE customer_paper_trades ADD COLUMN IF NOT EXISTS paper_session_id TEXT;
