-- Explicit opt-in for using recent FOMO buys as paper-trader discovery hints.
-- This never authorizes real-money execution or copies FOMO sell orders.
ALTER TABLE customer_paper_accounts ADD COLUMN fomo_signals_enabled INTEGER NOT NULL DEFAULT 0
    CHECK(fomo_signals_enabled IN (0,1));
