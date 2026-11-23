"""SQLite access and forward-only migrations. History is never dropped by a migration run."""
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # FastAPI runs sync dependencies and endpoints on different worker threads; each connection
    # is still used by only one request at a time, so this is safe.
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class MigrationError(RuntimeError):
    pass


def run_migrations(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> list:
    """Apply any unapplied *.sql files in order. Refuses to run if an applied file was edited."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    applied = {r["name"]: r["checksum"] for r in conn.execute("SELECT name, checksum FROM schema_migrations")}
    newly = []
    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text()
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        if path.name in applied:
            if applied[path.name] != checksum:
                raise MigrationError(f"{path.name} was modified after being applied; add a new migration instead")
            continue
        try:
            conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
            conn.execute(
                "INSERT INTO schema_migrations (name, checksum, applied_at) VALUES (?, ?, ?)",
                (path.name, checksum, utcnow()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        newly.append(path.name)
    return newly
