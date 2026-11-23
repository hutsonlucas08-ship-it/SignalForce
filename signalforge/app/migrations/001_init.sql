-- Observed market data: facts as reported by a source. Never mixed with simulated or live trades.
CREATE TABLE data_sources (
    name                  TEXT PRIMARY KEY,
    kind                  TEXT NOT NULL,
    last_success_at       TEXT,
    last_error_at         TEXT,
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE observations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL REFERENCES data_sources(name),
    asset_id     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    source_ref   TEXT,
    observed_at  TEXT NOT NULL,
    collected_at TEXT NOT NULL
);
CREATE INDEX idx_obs_asset_time ON observations(asset_id, observed_at);
CREATE INDEX idx_obs_collected ON observations(collected_at);

-- Shadow (paper) trades: hypothetical only. The CHECK makes it impossible to label one as live.
CREATE TABLE shadow_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    starting_balance  REAL NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'shadow' CHECK (mode = 'shadow')
);

CREATE TABLE shadow_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES shadow_sessions(id),
    observation_id  INTEGER REFERENCES observations(id),
    asset_id        TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('entry', 'exit')),
    reason          TEXT NOT NULL,
    price           REAL NOT NULL,
    market_cap      REAL,
    assumed_slippage_bps REAL NOT NULL DEFAULT 0,
    assumed_fee     REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'shadow' CHECK (mode = 'shadow')
);

CREATE TABLE system_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error', 'critical')),
    component  TEXT NOT NULL,
    message    TEXT NOT NULL,
    actor      TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_events_time ON system_events(created_at);

CREATE TABLE notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,
    subject    TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    error      TEXT,
    created_at TEXT NOT NULL,
    sent_at    TEXT
);

-- Single-row kill switch. Anything that could ever place a live order must check this first.
CREATE TABLE control_flags (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    trading_halted  INTEGER NOT NULL DEFAULT 1,
    reason          TEXT,
    changed_by      TEXT,
    changed_at      TEXT NOT NULL
);
INSERT INTO control_flags (id, trading_halted, reason, changed_by, changed_at)
VALUES (1, 1, 'initial state: halted until explicitly resumed', 'system', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
