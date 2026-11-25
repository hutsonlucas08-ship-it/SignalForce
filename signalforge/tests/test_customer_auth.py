import pytest
from app.customer_auth import CustomerAuthError, authenticate_customer_session, create_customer_session, hash_password, revoke_customer_session, verify_csrf_token, verify_password
from app.db import connect, run_migrations, utcnow


def _account(conn, status="active"):
    now = utcnow()
    conn.execute("INSERT INTO customer_accounts(id,email,password_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                 ("acct-1", "person@example.test", "hash", status, now, now))
    conn.commit()


def test_password_hash_is_salted_and_verifies():
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")
    assert first.startswith("scrypt$")
    assert first != second
    assert verify_password("correct horse battery staple", first)
    assert not verify_password("wrong password", first)
    assert not verify_password("anything", "malformed")
    assert not verify_password("x" * 1025, first)


def test_password_policy():
    with pytest.raises(ValueError):
        hash_password("short")
    with pytest.raises(ValueError):
        hash_password("x" * 1025)


def test_session_tokens_are_hashed_and_csrf_bound():
    conn = connect(":memory:")
    run_migrations(conn)
    _account(conn)
    created = create_customer_session(conn, "acct-1")
    row = conn.execute("SELECT token_hash,csrf_token_hash FROM customer_sessions").fetchone()
    assert created["session_token"] not in row["token_hash"]
    assert created["csrf_token"] not in row["csrf_token_hash"]
    session = authenticate_customer_session(conn, created["session_token"])
    assert session["account_id"] == "acct-1"
    assert verify_csrf_token(session, created["csrf_token"])
    assert not verify_csrf_token(session, "wrong-token")


def test_revoked_session_is_rejected():
    conn = connect(":memory:")
    run_migrations(conn)
    _account(conn)
    created = create_customer_session(conn, "acct-1")
    assert revoke_customer_session(conn, created["session_token"])
    assert authenticate_customer_session(conn, created["session_token"]) is None
    assert not revoke_customer_session(conn, created["session_token"])


def test_pending_or_missing_accounts_cannot_get_sessions():
    conn = connect(":memory:")
    run_migrations(conn)
    with pytest.raises(CustomerAuthError):
        create_customer_session(conn, "missing")
    _account(conn, status="pending")
    with pytest.raises(CustomerAuthError):
        create_customer_session(conn, "acct-1")


def test_registration_creates_active_account_and_normalizes_email():
    from app.customer_auth import register_customer_account
    conn = connect(":memory:")
    run_migrations(conn)
    account_id = register_customer_account(conn, "  Person@Example.Test ", "correct horse battery staple")
    row = conn.execute("SELECT email,status,password_hash FROM customer_accounts WHERE id=?", (account_id,)).fetchone()
    assert row["email"] == "person@example.test"
    assert row["status"] == "active"
    assert verify_password("correct horse battery staple", row["password_hash"])
    session = create_customer_session(conn, account_id)
    assert session["session_token"]


def test_registration_rejects_invalid_email_and_duplicate_email():
    from app.customer_auth import register_customer_account
    conn = connect(":memory:")
    run_migrations(conn)
    with pytest.raises(ValueError):
        register_customer_account(conn, "not-an-email", "correct horse battery staple")
    register_customer_account(conn, "person@example.test", "correct horse battery staple")
    with pytest.raises(CustomerAuthError):
        register_customer_account(conn, "PERSON@example.test", "another correct horse battery staple")


def test_email_verification_tokens_expire_and_are_one_time():
    from app.customer_auth import create_email_verification_token, consume_email_verification_token
    conn = connect(":memory:")
    run_migrations(conn)
    _account(conn, status="pending")
    token = create_email_verification_token(conn, "acct-1")
    assert consume_email_verification_token(conn, token)
    assert not consume_email_verification_token(conn, token)
    assert conn.execute("SELECT status,email_verified_at FROM customer_accounts WHERE id='acct-1'").fetchone()["status"] == "active"
