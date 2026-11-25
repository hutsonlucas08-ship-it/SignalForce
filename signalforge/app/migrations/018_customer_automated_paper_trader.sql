-- Customer-specific automated paper trader configuration.
-- The shared tracked_wallets and wallet_trade_events tables are intentionally untouched.
ALTER TABLE customer_paper_accounts ADD COLUMN strategy_mode TEXT NOT NULL DEFAULT 'normal'
    CHECK(strategy_mode IN ('gradual','normal','aggressive'));
ALTER TABLE customer_paper_accounts ADD COLUMN profit_target_usd REAL NOT NULL DEFAULT 500
    CHECK(profit_target_usd >= 1 AND profit_target_usd <= 100000000);
ALTER TABLE customer_paper_accounts ADD COLUMN auto_status TEXT NOT NULL DEFAULT 'setup'
    CHECK(auto_status IN ('setup','running','paused','target_reached'));
ALTER TABLE customer_paper_accounts ADD COLUMN last_scan_at TEXT;
ALTER TABLE customer_paper_positions ADD COLUMN last_price REAL NOT NULL DEFAULT 0
    CHECK(last_price >= 0);
CREATE INDEX idx_customer_paper_accounts_running
    ON customer_paper_accounts(enabled, auto_status, updated_at);
