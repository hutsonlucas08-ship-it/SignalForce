-- Persistent state required for trailing stops and one-time partial profit-taking.
CREATE TABLE IF NOT EXISTS position_tracking (
    asset_id TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    entry_liquidity_usd REAL,
    peak_price REAL NOT NULL,
    partial_taken INTEGER NOT NULL DEFAULT 0,
    unpriced_since TEXT
);
