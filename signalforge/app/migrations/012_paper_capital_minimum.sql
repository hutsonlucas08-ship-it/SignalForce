-- Rebuild paper_bot_state to lower the virtual capital floor from $100 to $50.
-- Preserve all runtime/session/lock fields on existing installations.
CREATE TABLE paper_bot_state_new (
    id INTEGER PRIMARY KEY CHECK(id=1),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    allocated_capital_usd REAL NOT NULL DEFAULT 1000 CHECK(allocated_capital_usd>=5),
    started_at TEXT,
    heartbeat_at TEXT,
    last_scan_at TEXT,
    last_error TEXT,
    lock_owner TEXT,
    lock_until_epoch REAL NOT NULL DEFAULT 0,
    run_started_at TEXT,
    run_until TEXT,
    session_seconds INTEGER,
    run_session_id TEXT,
    run_stopped_at TEXT,
    last_scan_summary TEXT
);
INSERT INTO paper_bot_state_new (
    id,enabled,allocated_capital_usd,started_at,heartbeat_at,last_scan_at,last_error,
    lock_owner,lock_until_epoch,run_started_at,run_until,session_seconds,
    run_session_id,run_stopped_at,last_scan_summary
)
SELECT
    id,enabled,allocated_capital_usd,started_at,heartbeat_at,last_scan_at,last_error,
    lock_owner,lock_until_epoch,run_started_at,run_until,session_seconds,
    run_session_id,run_stopped_at,last_scan_summary
FROM paper_bot_state;
DROP TABLE paper_bot_state;
ALTER TABLE paper_bot_state_new RENAME TO paper_bot_state;
