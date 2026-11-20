CREATE TABLE paper_bot_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    allocated_capital_usd REAL NOT NULL DEFAULT 1000 CHECK(allocated_capital_usd>=100),
    started_at TEXT,
    heartbeat_at TEXT,
    last_scan_at TEXT,
    last_error TEXT,
    lock_owner TEXT,
    lock_until_epoch REAL NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO paper_bot_state(id, enabled, allocated_capital_usd) VALUES(1, 1, 1000);
