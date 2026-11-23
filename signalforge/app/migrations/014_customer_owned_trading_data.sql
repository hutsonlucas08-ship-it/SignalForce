-- Tenant-owned wallet watchlists and paper trading records.
-- Existing single-operator tables remain untouched for backwards compatibility.
CREATE TABLE customer_tracked_wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    wallet_address TEXT NOT NULL,
    label TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, wallet_address)
);
CREATE INDEX idx_customer_tracked_wallets_account
    ON customer_tracked_wallets(account_id, status, created_at DESC);

CREATE TABLE customer_paper_accounts (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES customer_accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT 'Main Paper Account',
    starting_capital_usd REAL NOT NULL CHECK(starting_capital_usd >= 5 AND starting_capital_usd <= 100000000),
    cash_balance_usd REAL NOT NULL CHECK(cash_balance_usd >= 0),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_customer_paper_accounts_owner
    ON customer_paper_accounts(account_id, created_at);

CREATE TABLE customer_paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_account_id TEXT NOT NULL REFERENCES customer_paper_accounts(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL,
    token_name TEXT,
    quantity REAL NOT NULL CHECK(quantity >= 0),
    average_entry_price REAL NOT NULL CHECK(average_entry_price >= 0),
    opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(paper_account_id, asset_id)
);
CREATE INDEX idx_customer_paper_positions_account
    ON customer_paper_positions(paper_account_id, opened_at DESC);

CREATE TABLE customer_paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_account_id TEXT NOT NULL REFERENCES customer_paper_accounts(id) ON DELETE CASCADE,
    asset_id TEXT NOT NULL,
    token_name TEXT,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    quantity REAL NOT NULL CHECK(quantity > 0),
    price_usd REAL NOT NULL CHECK(price_usd > 0),
    amount_usd REAL NOT NULL CHECK(amount_usd > 0),
    realized_pnl_usd REAL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_customer_paper_trades_account
    ON customer_paper_trades(paper_account_id, created_at DESC);

CREATE TABLE customer_paper_sessions (
    id TEXT PRIMARY KEY,
    paper_account_id TEXT NOT NULL REFERENCES customer_paper_accounts(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','stopped')),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_customer_paper_sessions_account
    ON customer_paper_sessions(paper_account_id, started_at DESC);
