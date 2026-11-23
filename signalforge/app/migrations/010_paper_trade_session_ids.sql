ALTER TABLE paper_bot_state ADD COLUMN run_session_id TEXT;
ALTER TABLE paper_trades ADD COLUMN session_id TEXT;
CREATE INDEX IF NOT EXISTS idx_paper_trades_session ON paper_trades(account_id, session_id, created_at);
