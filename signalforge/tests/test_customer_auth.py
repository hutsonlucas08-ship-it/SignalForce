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
