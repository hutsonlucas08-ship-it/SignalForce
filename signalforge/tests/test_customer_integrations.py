import sqlite3

import pytest
from cryptography.fernet import Fernet

from app.customer_integrations import (
    CustomerIntegrationError,
    disconnect_customer_integration,
    get_customer_integration_status,
    load_customer_integration,
    save_customer_integration,
    set_customer_integration_status,
)
from app.db import connect, run_migrations, utcnow


def _account(conn, account_id, status="active"):
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_accounts(id,email,password_hash,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (account_id, account_id + "@example.test", "test-hash", status, now, now),
    )
    conn.commit()


def _db():
    conn = connect(":memory:")
    run_migrations(conn)
    _account(conn, "acct-a")
    _account(conn, "acct-b")
    return conn


def test_credentials_are_encrypted_and_never_returned_in_status():
    conn = _db()
    key = Fernet.generate_key().decode("ascii")
    status = save_customer_integration(
        conn, "acct-a", "helius", {"api_key": "private-helius-key"}, encryption_key=key
    )
    stored = conn.execute(
        "SELECT encrypted_payload FROM customer_integrations WHERE account_id='acct-a'"
    ).fetchone()["encrypted_payload"]
    assert "private-helius-key" not in stored
    assert "private-helius-key" not in str(status)
    assert status["configured"] is True
    assert status["status"] == "unverified"
    assert load_customer_integration(conn, "acct-a", "helius", encryption_key=key) == {
        "api_key": "private-helius-key"
    }


def test_customer_cannot_read_another_customers_integration():
    conn = _db()
    key = Fernet.generate_key().decode("ascii")
    save_customer_integration(
        conn, "acct-a", "telegram",
        {"bot_token": "secret-token", "chat_id": "12345"}, encryption_key=key
    )
    assert get_customer_integration_status(conn, "acct-b", "telegram") == {
        "provider": "telegram", "configured": False, "status": "not_configured"
    }
    assert load_customer_integration(conn, "acct-b", "telegram", encryption_key=key) is None


def test_integration_can_be_validated_replaced_and_disconnected():
    conn = _db()
    key = Fernet.generate_key().decode("ascii")
    save_customer_integration(conn, "acct-a", "helius", {"api_key": "old"}, encryption_key=key)
    connected = set_customer_integration_status(conn, "acct-a", "helius", "connected")
    assert connected["status"] == "connected"
    assert connected["validated_at"]
    replaced = save_customer_integration(
        conn, "acct-a", "helius", {"api_key": "new"}, encryption_key=key
    )
    assert replaced["status"] == "unverified"
    assert replaced["validated_at"] is None
    assert load_customer_integration(conn, "acct-a", "helius", encryption_key=key) == {
        "api_key": "new"
    }
    assert disconnect_customer_integration(conn, "acct-a", "helius") is True
    assert disconnect_customer_integration(conn, "acct-a", "helius") is False


def test_inactive_account_cannot_save_credentials():
    conn = _db()
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_accounts(id,email,password_hash,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        ("pending", "pending@example.test", "hash", "pending", now, now),
    )
    conn.commit()
    key = Fernet.generate_key().decode("ascii")
    with pytest.raises(CustomerIntegrationError):
        save_customer_integration(
            conn, "pending", "helius", {"api_key": "secret"}, encryption_key=key
        )


def test_unsupported_provider_and_non_string_secret_are_rejected():
    conn = _db()
    key = Fernet.generate_key().decode("ascii")
    with pytest.raises(CustomerIntegrationError):
        save_customer_integration(conn, "acct-a", "unknown", {"secret": "x"}, encryption_key=key)
    with pytest.raises(CustomerIntegrationError):
        save_customer_integration(conn, "acct-a", "helius", {"api_key": 123}, encryption_key=key)


def test_provider_error_text_cannot_be_stored_as_an_error_code():
    conn = _db()
    key = Fernet.generate_key().decode("ascii")
    save_customer_integration(conn, "acct-a", "telegram", {"token": "secret"}, encryption_key=key)
    with pytest.raises(CustomerIntegrationError):
        set_customer_integration_status(
            conn, "acct-a", "telegram", "error", error_code="bot token secret leaked"
        )
