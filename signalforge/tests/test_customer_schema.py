import sqlite3

import pytest

from app.db import connect, run_migrations, utcnow


def _account(conn, account_id, email):
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_accounts(id,email,password_hash,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        (account_id, email, "test-hash-not-a-real-password", now, now),
    )


def test_customer_accounts_and_integrations_are_isolated_by_account():
    conn = connect(":memory:")
    run_migrations(conn)
    _account(conn, "acct-a", "a@example.test")
    _account(conn, "acct-b", "b@example.test")
    conn.execute(
        "INSERT INTO customer_integrations(account_id,provider,encrypted_payload,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        ("acct-a", "helius", "ciphertext-a", utcnow(), utcnow()),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT account_id,provider FROM customer_integrations WHERE account_id=?",
        ("acct-b",),
    ).fetchall()
    assert rows == []
    assert conn.execute("SELECT COUNT(*) FROM customer_integrations").fetchone()[0] == 1


def test_customer_email_is_unique_case_insensitively():
    conn = connect(":memory:")
    run_migrations(conn)
    _account(conn, "acct-a", "person@example.test")
    with pytest.raises(sqlite3.IntegrityError):
        _account(conn, "acct-b", "PERSON@example.test")


def test_customer_session_and_integration_rows_cascade_on_account_delete():
    conn = connect(":memory:")
    run_migrations(conn)
    _account(conn, "acct-a", "a@example.test")
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_sessions(token_hash,account_id,created_at,expires_at,last_seen_at,csrf_token_hash) "
        "VALUES(?,?,?,?,?,?)",
        ("session-hash", "acct-a", now, now, now, "csrf-hash"),
    )
    conn.execute(
        "INSERT INTO customer_integrations(account_id,provider,encrypted_payload,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        ("acct-a", "telegram", "encrypted-payload", now, now),
    )
    conn.execute("DELETE FROM customer_accounts WHERE id=?", ("acct-a",))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM customer_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM customer_integrations").fetchone()[0] == 0
