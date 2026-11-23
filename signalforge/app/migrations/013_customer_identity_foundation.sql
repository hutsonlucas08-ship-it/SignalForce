-- Customer identity and private integration storage foundation.
-- Existing shared tables are intentionally unchanged until tenant-scoped migrations are ready.
CREATE TABLE customer_accounts (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','suspended')),
    email_verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_customer_accounts_status ON customer_accounts(status);

CREATE TABLE customer_sessions (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    csrf_token_hash TEXT NOT NULL
);
CREATE INDEX idx_customer_sessions_account ON customer_sessions(account_id);
CREATE INDEX idx_customer_sessions_expiry ON customer_sessions(expires_at);

CREATE TABLE customer_integrations (
    account_id TEXT NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK(provider IN ('helius','telegram')),
    encrypted_payload TEXT NOT NULL,
    key_version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'unverified' CHECK(status IN ('unverified','connected','error','disabled')),
    validated_at TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, provider)
);
CREATE INDEX idx_customer_integrations_provider_status
    ON customer_integrations(provider, status);
