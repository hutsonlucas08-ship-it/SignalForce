-- Smart-money tracking: append-only source observations and deduplicated alerts.
CREATE TABLE IF NOT EXISTS wallet_leaderboard_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    leaderboard TEXT NOT NULL CHECK (leaderboard IN ('daily_pnl', 'weekly_pnl', 'callout')),
    rank INTEGER,
    pnl_sol REAL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    FOREIGN KEY (wallet_address) REFERENCES tracked_wallets(wallet_address) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_wallet_rankings_observed
    ON wallet_leaderboard_snapshots(leaderboard, observed_at DESC, rank);

CREATE TABLE IF NOT EXISTS wallet_trade_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    signature TEXT NOT NULL,
    mint TEXT NOT NULL,
    token_name TEXT,
    token_symbol TEXT,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell', 'unknown')),
    token_amount REAL,
    sol_amount REAL,
    usd_amount REAL,
    price_usd REAL,
    market_cap_usd REAL,
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(wallet_address, signature, mint, side),
    FOREIGN KEY (wallet_address) REFERENCES tracked_wallets(wallet_address) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_wallet_trade_events_mint_time
    ON wallet_trade_events(mint, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_trade_events_wallet_time
    ON wallet_trade_events(wallet_address, occurred_at DESC);

CREATE TABLE IF NOT EXISTS token_price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    price_usd REAL,
    market_cap_usd REAL,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    CHECK (price_usd IS NULL OR price_usd > 0),
    CHECK (market_cap_usd IS NULL OR market_cap_usd >= 0)
);
CREATE INDEX IF NOT EXISTS idx_token_price_observations_mint_time
    ON token_price_observations(mint, observed_at DESC);

CREATE TABLE IF NOT EXISTS smart_money_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    alert_type TEXT NOT NULL,
    mint TEXT,
    wallet_address TEXT,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_smart_money_alerts_created
    ON smart_money_alerts(created_at DESC);
