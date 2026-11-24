-- Tenant-scoped, sanitized FOMO read-only snapshots. No session credentials are stored.
CREATE TABLE customer_fomo_snapshots (
    account_id TEXT NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    snapshot_type TEXT NOT NULL CHECK(snapshot_type IN ('spot_swaps','clans_24h','clans_7d','clans_30d')),
    payload_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'fomo-browser-bridge',
    observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (account_id, snapshot_type)
);
CREATE INDEX idx_customer_fomo_snapshots_updated
    ON customer_fomo_snapshots(account_id, created_at DESC);
