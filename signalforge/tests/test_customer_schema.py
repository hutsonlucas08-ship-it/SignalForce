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


def test_customer_wallets_and_paper_accounts_are_tenant_scoped():
    conn = connect(":memory:")
    run_migrations(conn)
    _account(conn, "acct-a", "a@example.test")
    _account(conn, "acct-b", "b@example.test")
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_tracked_wallets(account_id,wallet_address,label,created_at,updated_at) "
        "VALUES(?,?,?,?,?)",
        ("acct-a", "11111111111111111111111111111111", "A wallet", now, now),
    )
    conn.execute(
        "INSERT INTO customer_paper_accounts(id,account_id,name,starting_capital_usd,cash_balance_usd,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("paper-a", "acct-a", "A paper", 100, 100, now, now),
    )
    conn.execute(
        "INSERT INTO customer_paper_positions(paper_account_id,asset_id,quantity,average_entry_price,opened_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        ("paper-a", "mint-a", 10, 0.01, now, now),
    )
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM customer_tracked_wallets WHERE account_id=?", ("acct-b",)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM customer_paper_accounts WHERE account_id=?", ("acct-b",)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM customer_paper_positions p JOIN customer_paper_accounts a "
        "ON a.id=p.paper_account_id WHERE a.account_id=?", ("acct-b",)
    ).fetchone()[0] == 0


def test_customer_trading_data_cascades_when_account_is_deleted():
    conn = connect(":memory:")
    run_migrations(conn)
    _account(conn, "acct-a", "a@example.test")
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_paper_accounts(id,account_id,name,starting_capital_usd,cash_balance_usd,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("paper-a", "acct-a", "A paper", 100, 100, now, now),
    )
    conn.execute(
        "INSERT INTO customer_tracked_wallets(account_id,wallet_address,created_at,updated_at) "
        "VALUES(?,?,?,?)",
        ("acct-a", "11111111111111111111111111111111", now, now),
    )
    conn.execute("DELETE FROM customer_accounts WHERE id=?", ("acct-a",))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM customer_tracked_wallets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM customer_paper_accounts").fetchone()[0] == 0


def test_repair_migration_recovers_database_with_020_already_recorded(tmp_path):
    import hashlib
    from pathlib import Path

    from app.db import run_migrations

    conn = connect(":memory:")
    conn.execute(
        "CREATE TABLE customer_paper_accounts (id TEXT PRIMARY KEY, auto_started_at TEXT)"
    )
    conn.execute("CREATE TABLE customer_paper_trades (id TEXT PRIMARY KEY)")
    original_020 = (
        "-- Track the start of each automated paper session so its dashboard history is session-specific.\\n"
        "ALTER TABLE customer_paper_accounts ADD COLUMN auto_started_at TEXT;\\n"
    )
    migration_dir = tmp_path / "migrations"
    migration_dir.mkdir()
    (migration_dir / "020_customer_paper_session_history.sql").write_text(original_020)
    (migration_dir / "021_repair_customer_paper_session_columns.sql").write_text(
        "ALTER TABLE customer_paper_accounts ADD COLUMN IF NOT EXISTS auto_session_id TEXT;\\n"
        "ALTER TABLE customer_paper_trades ADD COLUMN IF NOT EXISTS paper_session_id TEXT;\\n"
    )
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations(name, checksum, applied_at) VALUES(?,?,?)",
        (
            "020_customer_paper_session_history.sql",
            hashlib.sha256(original_020.encode()).hexdigest(),
            utcnow(),
        ),
    )
    conn.commit()

    assert run_migrations(conn, directory=migration_dir) == [
        "021_repair_customer_paper_session_columns.sql"
    ]
    account_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(customer_paper_accounts)")
    }
    trade_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(customer_paper_trades)")
    }
    assert {"auto_started_at", "auto_session_id"} <= account_columns
    assert "paper_session_id" in trade_columns
