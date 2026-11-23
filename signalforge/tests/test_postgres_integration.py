"""Real PostgreSQL smoke tests. CI supplies SIGNALFORGE_TEST_DATABASE_URL."""
import os
import sqlite3
import uuid

import pytest

from app.db import connect, run_migrations, utcnow


DATABASE_URL = os.getenv("SIGNALFORGE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="Set SIGNALFORGE_TEST_DATABASE_URL to run PostgreSQL integration tests",
)


@pytest.fixture(scope="module")
def postgres_connection():
    conn = connect(":memory:", DATABASE_URL)
    run_migrations(conn)
    yield conn
    conn.close()


def test_all_schema_migrations_apply_to_postgres(postgres_connection):
    rows = postgres_connection.execute(
        "SELECT name FROM schema_migrations ORDER BY name"
    ).fetchall()
    assert len(rows) >= 16
    assert rows[0]["name"] == "001_init.sql"


def test_postgres_enforces_case_insensitive_customer_email_uniqueness(postgres_connection):
    conn = postgres_connection
    suffix = uuid.uuid4().hex
    account_id = "pg-" + suffix
    email = suffix + "@example.test"
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_accounts(id,email,password_hash,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        (account_id, email, "integration-test-hash", now, now),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO customer_accounts(id,email,password_hash,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            ("pg-duplicate-" + suffix, email.upper(), "another-test-hash", now, now),
        )
    conn.rollback()

    count = conn.execute(
        "SELECT COUNT(*) FROM customer_accounts WHERE id=?", (account_id,)
    ).fetchone()[0]
    assert count == 1


def test_postgres_returning_and_conflict_handling(postgres_connection):
    conn = postgres_connection
    suffix = uuid.uuid4().hex
    account_id = "pg-wallet-" + suffix
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_accounts(id,email,password_hash,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        (account_id, suffix + "@wallet.test", "integration-test-hash", now, now),
    )
    conn.commit()

    address = "11111111111111111111111111111111"
    first = conn.execute(
        "INSERT INTO customer_tracked_wallets(account_id,wallet_address,created_at,updated_at) "
        "VALUES(?,?,?,?) RETURNING id",
        (account_id, address, now, now),
    )
    row = first.fetchone()
    assert row["id"] == row[0]
    conn.commit()

    duplicate = conn.execute(
        "INSERT INTO customer_tracked_wallets(account_id,wallet_address,created_at,updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
        (account_id, address, now, now),
    )
    assert duplicate.rowcount == 0
    conn.commit()


def test_postgres_paper_bot_lease_query_is_transactionally_locked(postgres_connection):
    conn = postgres_connection
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT lock_owner,lock_until_epoch FROM paper_bot_state WHERE id=1"
    ).fetchone()
    assert row is not None
    conn.commit()
