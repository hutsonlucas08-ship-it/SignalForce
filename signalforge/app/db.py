"""SQLite and PostgreSQL access with forward-only, checksummed migrations."""
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .postgres_compat import translate_migration_sql

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MigrationError(RuntimeError):
    pass


class HybridRow(dict):
    """Mapping row that also supports SQLite-style integer column indexing."""

    def __init__(self, columns, values):
        values = tuple(values)
        super().__init__(zip(columns, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _hybrid_row_factory(cursor):
    # Psycopg invokes the row factory for command-only statements too.
    if cursor.description is None:
        return lambda values: values
    columns = [column.name for column in cursor.description]

    def make_row(values):
        return HybridRow(columns, values)

    return make_row


def _qmark_to_pyformat(sql: str) -> str:
    """Replace DB-API qmark placeholders without touching quoted SQL strings."""
    output = []
    quote = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in ("'", '"'):
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _postgres_runtime_sql(sql: str) -> str:
    statement = sql.strip()
    if statement.upper() == "BEGIN IMMEDIATE":
        return "BEGIN"
    translated = _qmark_to_pyformat(sql)
    # The paper-bot lease is claimed inside a transaction. Locking this row
    # prevents two workers from both believing they own the same lease.
    if re.match(
        r"^\s*SELECT\s+lock_owner\s*,\s*lock_until_epoch\s+FROM\s+paper_bot_state\s+WHERE\s+id\s*=\s*1\s*;?\s*$",
        translated,
        re.IGNORECASE,
    ):
        translated = translated.rstrip().rstrip(";") + " FOR UPDATE"
    # Serialize customer paper trades on the owning account row so concurrent
    # buys cannot both spend the same cash balance.
    if re.match(
        r"^\s*SELECT\s+id\s*,\s*cash_balance_usd(?:\s*,\s*auto_session_id)?\s+FROM\s+customer_paper_accounts\s+WHERE\s+id\s*=\s*%s\s+AND\s+account_id\s*=\s*%s\s*;?\s*$",
        translated,
        re.IGNORECASE,
    ):
        translated = translated.rstrip().rstrip(";") + " FOR UPDATE"
    return translated


class PostgresCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)


class PostgresConnection:
    """Small adapter for the sqlite3 connection surface used by this application."""

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql: str, parameters=()):
        import psycopg

        begins_immediate = sql.strip().upper() == "BEGIN IMMEDIATE"
        query = _postgres_runtime_sql(sql)
        try:
            if begins_immediate:
                # _claim() calls this after committing its previous state read.
                self.raw.commit()
                cursor = self.raw.execute(query, prepare=False)
            elif parameters:
                cursor = self.raw.execute(query, tuple(parameters), prepare=False)
            else:
                cursor = self.raw.execute(query, prepare=False)
        except psycopg.IntegrityError as exc:
            # Existing domain services intentionally catch sqlite3.IntegrityError.
            # Roll back the failed PostgreSQL transaction before translating it.
            self.raw.rollback()
            raise sqlite3.IntegrityError(str(exc)) from exc
        return PostgresCursor(cursor)

    def executescript(self, sql: str):
        self.raw.execute(sql, prepare=False)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def connect(db_path: str, database_url: str | None = None):
    """Open PostgreSQL when DATABASE_URL is configured; otherwise use local SQLite."""
    database_url = database_url or os.getenv("DATABASE_URL", "").strip() or None
    if database_url:
        import psycopg

        raw = psycopg.connect(database_url, row_factory=_hybrid_row_factory)
        return PostgresConnection(raw)

    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # FastAPI sync dependencies run on worker threads; each connection is used by
    # one request at a time, so disabling SQLite's same-thread guard is safe.
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def run_migrations(conn, directory: Path = MIGRATIONS_DIR) -> list:
    """Apply unapplied migrations; reject edits to any migration already applied."""
    is_postgres = isinstance(conn, PostgresConnection)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "name TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    applied = {
        row["name"]: row["checksum"]
        for row in conn.execute("SELECT name, checksum FROM schema_migrations")
    }
    # End the read transaction before opening one transaction per migration.
    conn.commit()
    newly = []
    for path in sorted(directory.glob("*.sql")):
        source_sql = path.read_text()
        checksum = hashlib.sha256(source_sql.encode()).hexdigest()
        if path.name in applied:
            if applied[path.name] != checksum:
                raise MigrationError(
                    f"{path.name} was modified after being applied; add a new migration instead"
                )
            continue
        migration_sql = (
            translate_migration_sql(source_sql, path.name) if is_postgres else source_sql
        )
        try:
            if is_postgres:
                conn.executescript("BEGIN;\n" + migration_sql + "\nCOMMIT;")
            else:
                conn.executescript("BEGIN;\n" + migration_sql + "\nCOMMIT;")
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
