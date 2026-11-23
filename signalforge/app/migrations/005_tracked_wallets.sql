CREATE TABLE IF NOT EXISTS tracked_wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL UNIQUE,
    label TEXT,
    added_at TEXT NOT NULL,
    last_signature TEXT,
    last_seen_at TEXT
);
