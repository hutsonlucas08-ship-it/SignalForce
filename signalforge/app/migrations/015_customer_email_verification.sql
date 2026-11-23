-- Email verification tokens are stored as hashes and expire after one hour.
CREATE TABLE customer_email_verification_tokens (
    token_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX idx_customer_email_verification_account
    ON customer_email_verification_tokens(account_id, created_at DESC);
