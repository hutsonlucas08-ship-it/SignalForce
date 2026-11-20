"""Small data-access layer. All writes go through here so rules live in one place."""
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .db import utcnow


def _parse(ts: Optional[str]):
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def log_event(conn, level: str, component: str, message: str, actor: Optional[str] = None) -> None:
    conn.execute(
        "INSERT INTO system_events (level, component, message, actor, created_at) VALUES (?,?,?,?,?)",
        (level, component, message, actor, utcnow()),
    )
    conn.commit()


def recent_events(conn, limit: int = 50) -> list:
    limit = max(1, min(limit, 200))
    rows = conn.execute(
        "SELECT id, level, component, message, actor, created_at FROM system_events ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]


def get_halt(conn) -> dict:
    return dict(conn.execute(
        "SELECT trading_halted, reason, changed_by, changed_at FROM control_flags WHERE id = 1"
    ).fetchone())


def set_halt(conn, halted: bool, reason: str, actor: str) -> dict:
    conn.execute(
        "UPDATE control_flags SET trading_halted=?, reason=?, changed_by=?, changed_at=? WHERE id=1",
        (1 if halted else 0, reason, actor, utcnow()),
    )
    conn.commit()
    log_event(conn, "warning" if halted else "info", "control",
              f"trading {'HALTED' if halted else 'RESUMED'}: {reason}", actor)
    return get_halt(conn)


def register_source(conn, name: str, kind: str) -> None:
    conn.execute("INSERT OR IGNORE INTO data_sources (name, kind) VALUES (?, ?)", (name, kind))
    conn.commit()


def record_source_success(conn, name: str) -> None:
    conn.execute(
        "UPDATE data_sources SET last_success_at=?, last_error_at=NULL, last_error=NULL, "
        "consecutive_failures=0 WHERE name=?", (utcnow(), name)
    )
    conn.commit()


def record_source_failure(conn, name: str, error: str) -> None:
    conn.execute(
        "UPDATE data_sources SET last_error_at=?, last_error=?, consecutive_failures=consecutive_failures+1 "
        "WHERE name=?", (utcnow(), error[:500], name),
    )
    conn.commit()


def source_status(row: sqlite3.Row, stale_after_seconds: int, now: Optional[datetime] = None) -> str:
    """unknown (never succeeded) | failing (3+ failures in a row) | stale (too old) | ok."""
    now = now or datetime.now(timezone.utc)
    if row["consecutive_failures"] >= 3:
        return "failing"
    last = _parse(row["last_success_at"])
    if last is None:
        return "unknown"
    return "stale" if (now - last).total_seconds() > stale_after_seconds else "ok"


def list_sources(conn, stale_after_seconds: int, now: Optional[datetime] = None) -> list:
    out = []
    for r in conn.execute("SELECT * FROM data_sources ORDER BY name"):
        d = dict(r)
        d["status"] = source_status(r, stale_after_seconds, now)
        out.append(d)
    return out


def last_observation_time(conn) -> Optional[str]:
    row = conn.execute("SELECT MAX(collected_at) AS t FROM observations").fetchone()
    return row["t"]


def add_observation(
    conn,
    source: str,
    asset_id: str,
    kind: str,
    payload: str,
    source_ref: Optional[str] = None,
    observed_at: Optional[str] = None,
) -> int:
    """Store a read-only research observation and return its ID."""
    collected_at = utcnow()
    observed_at = observed_at or collected_at

    cursor = conn.execute(
        """
        INSERT INTO observations (
            source,
            asset_id,
            kind,
            payload,
            source_ref,
            observed_at,
            collected_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            asset_id,
            kind,
            payload,
            source_ref,
            observed_at,
            collected_at,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_observations(
    conn,
    asset_id: str,
    limit: int = 50,
    source: Optional[str] = None,
    kind: Optional[str] = None,
    offset: int = 0,
) -> list:
    """Return filtered, paginated research observations for one asset."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    conditions = ["asset_id = ?"]
    params = [asset_id]

    if source:
        conditions.append("source = ?")
        params.append(source)

    if kind:
        conditions.append("kind = ?")
        params.append(kind)

    params.extend([limit, offset])

    rows = conn.execute(
        f"""
        SELECT id, source, asset_id, kind, payload,
               source_ref, observed_at, collected_at
        FROM observations
        WHERE {" AND ".join(conditions)}
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        params,
    )

    return [dict(row) for row in rows]
