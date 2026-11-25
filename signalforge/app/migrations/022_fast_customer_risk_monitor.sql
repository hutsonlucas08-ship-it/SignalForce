-- Fast shared risk-monitor state for customer paper positions.
ALTER TABLE customer_paper_positions ADD COLUMN IF NOT EXISTS entry_liquidity_usd REAL NOT NULL DEFAULT 0;
ALTER TABLE customer_paper_positions ADD COLUMN IF NOT EXISTS peak_price REAL NOT NULL DEFAULT 0;
ALTER TABLE customer_paper_positions ADD COLUMN IF NOT EXISTS partial_profit_taken INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_customer_paper_positions_asset
    ON customer_paper_positions(asset_id);
