-- Extend the tenant-scoped FOMO snapshot type constraint for passive trader leaderboards.
-- Rebuild the table portably so both SQLite and PostgreSQL retain existing snapshots.
CREATE TABLE customer_fomo_snapshots_new (
    account_id TEXT NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    snapshot_type TEXT NOT NULL CHECK(snapshot_type IN (
        'spot_swaps',
        'clans_24h',
        'clans_7d',
        'clans_30d',
        'trader_leaderboard',
        'trader_leaderboard_24h',
        'trader_leaderboard_7d',
        'trader_leaderboard_30d'
    )),
    payload_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'fomo-browser-bridge',
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, snapshot_type)
);
INSERT INTO customer_fomo_snapshots_new
    (account_id, snapshot_type, payload_json, source, observed_at, created_at)
SELECT account_id, snapshot_type, payload_json, source, observed_at, created_at
FROM customer_fomo_snapshots;
DROP TABLE customer_fomo_snapshots;
ALTER TABLE customer_fomo_snapshots_new RENAME TO customer_fomo_snapshots;
CREATE INDEX idx_customer_fomo_snapshots_updated
    ON customer_fomo_snapshots(account_id, created_at DESC);
