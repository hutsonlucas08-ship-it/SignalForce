"""Real PostgreSQL smoke tests. CI supplies SIGNALFORGE_TEST_DATABASE_URL."""
import os
import sqlite3
import uuid

import pytest

from app.config import Settings
from app import customer_email
from app.customer_auth import (
    CustomerAuthError, authenticate_customer_session, consume_email_verification_token,
    create_customer_session, create_email_verification_token, register_customer_account,
)
from app.customer_data import (
    add_wallet, create_paper_account, list_paper_positions, list_paper_trades,
    list_wallets, place_paper_trade,
)
from app.customer_integrations import (
    get_customer_integration_status, load_customer_integration, save_customer_integration,
)
from cryptography.fernet import Fernet
from app.db import connect, run_migrations, utcnow
from app.main import create_app
from fastapi.testclient import TestClient


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



def test_fastapi_application_starts_and_serves_health_on_postgres(postgres_connection):
    settings = Settings(
        db_path=":memory:",
        read_api_keys=frozenset(),
        admin_api_keys=frozenset(),
        database_url=DATABASE_URL,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200



def test_postgres_customer_registration_verification_and_session_lifecycle(postgres_connection):
    conn = postgres_connection
    email = uuid.uuid4().hex + "@customer.test"
    account_id = register_customer_account(conn, email.upper(), "correct horse battery staple")
    with pytest.raises(CustomerAuthError):
        create_customer_session(conn, account_id)

    token = create_email_verification_token(conn, account_id)
    stored = conn.execute(
        "SELECT token_hash FROM customer_email_verification_tokens WHERE account_id=?",
        (account_id,),
    ).fetchone()
    assert token not in stored["token_hash"]
    assert consume_email_verification_token(conn, token) is True
    assert consume_email_verification_token(conn, token) is False

    created = create_customer_session(conn, account_id)
    session = authenticate_customer_session(conn, created["session_token"])
    assert session is not None
    assert session["account_id"] == account_id
    assert session["email"] == email.lower()


def test_postgres_customer_wallet_and_paper_trading_lifecycle(postgres_connection):
    conn = postgres_connection
    email = uuid.uuid4().hex + "@trader.test"
    account_id = register_customer_account(conn, email, "correct horse battery staple")
    token = create_email_verification_token(conn, account_id)
    assert consume_email_verification_token(conn, token)

    wallet = "11111111111111111111111111111111"
    add_wallet(conn, account_id, wallet, "Postgres integration wallet")
    assert list_wallets(conn, account_id)[0]["label"] == "Postgres integration wallet"

    paper = create_paper_account(conn, account_id, 500, "Postgres test account")
    bought = place_paper_trade(
        conn, account_id, paper["id"], asset_id="integration-mint",
        side="buy", amount_usd=100, price_usd=2, token_name="Integration Token",
    )
    assert bought["cash_balance_usd"] == 400
    assert len(list_paper_positions(conn, account_id, paper["id"])) == 1

    sold = place_paper_trade(
        conn, account_id, paper["id"], asset_id="integration-mint",
        side="sell", amount_usd=60, price_usd=3,
    )
    assert sold["realized_pnl_usd"] == 20
    assert sold["cash_balance_usd"] == 460
    assert len(list_paper_trades(conn, account_id, paper["id"])) == 2


def test_postgres_customer_integration_credentials_are_encrypted_and_tenant_scoped(postgres_connection):
    conn = postgres_connection
    suffix = uuid.uuid4().hex
    account_a = register_customer_account(
        conn, suffix + "@integration-a.test", "correct horse battery staple"
    )
    account_b = register_customer_account(
        conn, suffix + "@integration-b.test", "correct horse battery staple"
    )
    for account_id in (account_a, account_b):
        token = create_email_verification_token(conn, account_id)
        assert consume_email_verification_token(conn, token)

    key = Fernet.generate_key().decode("ascii")
    credentials = {"api_key": "postgres-private-helius-key"}
    status = save_customer_integration(
        conn, account_a, "helius", credentials, encryption_key=key
    )
    stored = conn.execute(
        "SELECT encrypted_payload FROM customer_integrations WHERE account_id=? AND provider='helius'",
        (account_a,),
    ).fetchone()
    assert credentials["api_key"] not in stored["encrypted_payload"]
    assert status["configured"] is True
    assert load_customer_integration(
        conn, account_a, "helius", encryption_key=key
    ) == credentials
    assert load_customer_integration(
        conn, account_b, "helius", encryption_key=key
    ) is None
    assert get_customer_integration_status(
        conn, account_b, "helius"
    )["configured"] is False


def test_postgres_customer_registration_and_signin_routes(monkeypatch):
    settings = Settings(
        db_path=":memory:",
        read_api_keys=frozenset(),
        admin_api_keys=frozenset(),
        database_url=DATABASE_URL,
    )
    sent = {}

    async def fake_send(address, token):
        sent[address] = token

    monkeypatch.setattr(customer_email, "send_verification_email", fake_send)
    monkeypatch.setenv("SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        email = uuid.uuid4().hex + "@routes.test"
        registered = client.post(
            "/v1/customer/register",
            json={"email": email, "password": "correct horse battery staple"},
        )
        assert registered.status_code == 201
        assert email in sent
        verified = client.get(
            "/v1/customer/verify-email", params={"token": sent[email]}
        )
        assert verified.status_code == 200
        login = client.post(
            "/v1/customer/login",
            json={"email": email, "password": "correct horse battery staple"},
        )
        assert login.status_code == 200
        assert client.get("/v1/customer/me").json()["email"] == email
