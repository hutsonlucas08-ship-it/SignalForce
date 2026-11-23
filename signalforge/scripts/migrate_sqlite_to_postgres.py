"""Conservative, one-time SQLite-to-PostgreSQL data transfer for SignalForce.

Run from signalforge/ with DATABASE_URL set. Default mode is a read-only dry run.
Use --apply only against a brand-new, empty PostgreSQL database.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

import psycopg
from psycopg import sql


def dependency_order(table_names: list[str], foreign_keys: list[tuple[str, str]]) -> list[str]:
    """Return parent-before-child table order; fail closed on FK cycles."""
    names = set(table_names)
    parents = {name: set() for name in names}
    children = {name: set() for name in names}
    for child, parent in foreign_keys:
        if child in names and parent in names and child != parent:
            parents[child].add(parent)
            children[parent].add(child)

    ready = sorted(name for name in names if not parents[name])
    ordered = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for child in sorted(children[current]):
            parents[child].discard(current)
            if not parents[child] and child not in ordered and child not in ready:
                ready.append(child)
        ready.sort()

    if len(ordered) != len(names):
        cyclic = sorted(names - set(ordered))
        raise RuntimeError(
            "Foreign-key cycle detected; automatic import is unsafe for: "
            + ", ".join(cyclic)
        )
    return ordered


def open_source(path: Path):
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def source_inventory(conn: sqlite3.Connection):
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity check failed: {integrity}")
    violations = conn.execute("PRAGMA foreign_key_check").fetchmany(10)
    if violations:
        raise RuntimeError(
            "SQLite foreign-key check failed; first violations: "
            + repr([tuple(row) for row in violations])
        )
    names = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name <> 'schema_migrations' "
            "ORDER BY name"
        )
    ]
    foreign_keys = []
    counts = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        for fk in conn.execute(f"PRAGMA foreign_key_list({quoted})"):
            foreign_keys.append((name, fk[2]))
        counts[name] = conn.execute(
            f"SELECT COUNT(*) FROM {quoted}"
        ).fetchone()[0]
    return names, foreign_keys, counts


def target_tables(conn):
    return [
        row[0]
        for row in conn.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname=current_schema() "
            "AND tablename NOT LIKE 'pg_%' "
            "AND tablename <> 'schema_migrations' ORDER BY tablename"
        ).fetchall()
    ]


def assert_target_empty(conn):
    nonempty = []
    for name in target_tables(conn):
        count = conn.execute(
            sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(name))
        ).fetchone()[0]
        if count:
            nonempty.append(f"{name} ({count} rows)")
    if nonempty:
        raise RuntimeError(
            "Destination database is not empty. Refusing to merge or overwrite "
            "existing data: " + ", ".join(nonempty)
        )


def clear_migration_seed_rows(conn):
    """Remove only the two known singleton rows inserted by schema migrations."""
    seed_tables = {"control_flags", "paper_bot_state"}
    for name in target_tables(conn):
        count = conn.execute(
            sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(name))
        ).fetchone()[0]
        if not count:
            continue
        if name not in seed_tables or count != 1:
            raise RuntimeError(
                f"Destination table {name} contains unexpected rows after schema creation."
            )
        seed_id = conn.execute(
            sql.SQL("SELECT id FROM {}").format(sql.Identifier(name))
        ).fetchone()[0]
        if seed_id != 1:
            raise RuntimeError(
                f"Destination table {name} contains an unexpected bootstrap row."
            )
        conn.execute(
            sql.SQL("DELETE FROM {}").format(sql.Identifier(name))
        )
    conn.commit()
    assert_target_empty(conn)


def backup_source(source_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = source_path.with_name(source_path.name + f".pre-postgres-{stamp}.bak")
    if backup_path.exists():
        raise RuntimeError(f"Backup already exists: {backup_path}")
    source = sqlite3.connect(source_path)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    return backup_path


def copy_data(source, target, names, foreign_keys):
    ordered = dependency_order(names, foreign_keys)
    summary = {}
    with target.transaction():
        with target.cursor() as cursor:
            for table in ordered:
                source_columns = [
                    row[1]
                    for row in source.execute(
                        f'PRAGMA table_info("{table.replace(chr(34), chr(34)*2)}")'
                    )
                ]
                metadata = cursor.execute(
                    "SELECT column_name,is_nullable,column_default,is_identity,is_generated "
                    "FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name=%s",
                    (table,),
                ).fetchall()
                target_columns = {row[0]: row for row in metadata}
                missing = sorted(set(source_columns) - set(target_columns))
                if missing:
                    raise RuntimeError(
                        f"Destination table {table} is missing source columns: {missing}"
                    )
                required_missing = [
                    column for column, row in target_columns.items()
                    if column not in source_columns
                    and row[1] == "NO"
                    and row[2] is None
                    and row[3] == "NO"
                    and row[4] == "NEVER"
                ]
                if required_missing:
                    raise RuntimeError(
                        f"Destination table {table} has new required columns without "
                        f"defaults: {required_missing}"
                    )

                quoted_table = sql.Identifier(table)
                quoted_columns = sql.SQL(", ").join(
                    sql.Identifier(column) for column in source_columns
                )
                placeholders = sql.SQL(", ").join(
                    sql.Placeholder() for _ in source_columns
                )
                insert = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    quoted_table, quoted_columns, placeholders
                )

                source_cursor = source.execute(
                    f'SELECT * FROM "{table.replace(chr(34), chr(34)*2)}"'
                )
                copied = 0
                while True:
                    batch = source_cursor.fetchmany(500)
                    if not batch:
                        break
                    cursor.executemany(insert, [tuple(row) for row in batch])
                    copied += len(batch)

                target_count = cursor.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}").format(quoted_table)
                ).fetchone()[0]
                if target_count != copied:
                    raise RuntimeError(
                        f"Row-count mismatch for {table}: source={copied}, destination={target_count}"
                    )
                summary[table] = copied

            # Explicitly inserted identity values must advance their sequences.
            identity_rows = cursor.execute(
                "SELECT table_name,column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND is_identity='YES'"
            ).fetchall()
            for table, column in identity_rows:
                maximum = cursor.execute(
                    sql.SQL("SELECT MAX({}) FROM {}").format(
                        sql.Identifier(column), sql.Identifier(table)
                    )
                ).fetchone()[0]
                if maximum is not None:
                    sequence = cursor.execute(
                        "SELECT pg_get_serial_sequence(%s,%s)", (table, column)
                    ).fetchone()[0]
                    if sequence:
                        cursor.execute(
                            "SELECT setval(%s::regclass,%s,true)",
                            (sequence, maximum),
                        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-path", required=True, type=Path)
    parser.add_argument(
        "--apply", action="store_true",
        help="Create the current schema and import data. Requires an empty PostgreSQL database.",
    )
    args = parser.parse_args()

    if not args.sqlite_path.is_file():
        parser.error(f"SQLite database file not found: {args.sqlite_path}")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url.startswith(("postgres://", "postgresql://")):
        parser.error("Set DATABASE_URL to the destination PostgreSQL connection string.")

    source = open_source(args.sqlite_path)
    try:
        names, foreign_keys, counts = source_inventory(source)
        if not names:
            raise RuntimeError("The SQLite database contains no application tables.")
        print(json.dumps({
            "mode": "apply" if args.apply else "dry-run",
            "source": str(args.sqlite_path.resolve()),
            "source_tables": len(names),
            "source_rows": sum(counts.values()),
            "tables": counts,
            "destination": "PostgreSQL DATABASE_URL (value hidden)",
        }, indent=2))

        with psycopg.connect(database_url) as target:
            if not args.apply:
                existing = target_tables(target)
                print(json.dumps({
                    "destination_existing_tables": existing,
                    "next_step": (
                        "Use --apply only after confirming this is a new, empty database."
                        if not existing else
                        "Destination already has application tables; inspect it before proceeding."
                    ),
                    "no_data_was_written": True,
                }, indent=2))
                return 0

            assert_target_empty(target)
            # Apply the checked-in schema to the empty destination before copying.
            from app.db import PostgresConnection, run_migrations
            migrated = run_migrations(PostgresConnection(target))
            # The checked-in migrations seed two singleton control rows. Replace
            # those known defaults with the source values; reject every other row.
            clear_migration_seed_rows(target)

            backup_path = backup_source(args.sqlite_path)
            copied = copy_data(source, target, names, foreign_keys)
            print(json.dumps({
                "migrations_applied": migrated,
                "backup": str(backup_path),
                "copied_rows": copied,
                "total_rows": sum(copied.values()),
                "result": "success",
            }, indent=2))
        return 0
    finally:
        source.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Migration stopped safely: {exc}", file=sys.stderr)
        raise SystemExit(1)
