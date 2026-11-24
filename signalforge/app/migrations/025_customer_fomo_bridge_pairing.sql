-- One-time pairing codes and revocable local bridge credentials for FOMO Copilot.
-- Only hashes are stored; the local bridge keeps the issued key in memory.
CREATE TABLE customer_fomo_bridge_pairs (
    account_id TEXT PRIMARY KEY REFERENCES customer_accounts(id) ON DELETE CASCADE,
    pairing_hash TEXT NOT NULL,
    pairing_expires_at TEXT NOT NULL,
    bridge_hash TEXT,
    bridge_expires_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_customer_fomo_bridge_hash
    ON customer_fomo_bridge_pairs(bridge_hash);
