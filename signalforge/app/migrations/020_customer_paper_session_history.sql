-- Isolate automated customer paper trade history into explicit sessions.
ALTER TABLE customer_paper_accounts ADD COLUMN auto_session_id TEXT;
ALTER TABLE customer_paper_trades ADD COLUMN paper_session_id TEXT;
