import pytest

from app.customer_data import (
    CustomerDataError, add_wallet, create_paper_account, get_paper_account,
    list_paper_accounts, list_wallets, remove_wallet,
)
from app.db import connect, run_migrations, utcnow


def _active_account(conn, account_id, email):
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_accounts(id,email,password_hash,status,created_at,updated_at) "
        "VALUES(?,?,?,'active',?,?)", (account_id, email, "test-hash", now, now)
    )
    conn.commit()


def test_wallet_crud_is_scoped_to_customer():
    conn = connect(":memory:")
    run_migrations(conn)
    _active_account(conn, "a", "a@example.test")
    _active_account(conn, "b", "b@example.test")
    wallet = "11111111111111111111111111111111"
    add_wallet(conn, "a", wallet, "My wallet")
    assert list_wallets(conn, "a")[0]["label"] == "My wallet"
    assert list_wallets(conn, "b") == []
    assert remove_wallet(conn, "b", wallet) is False
    assert len(list_wallets(conn, "a")) == 1
    assert remove_wallet(conn, "a", wallet) is True
    assert list_wallets(conn, "a") == []


def test_wallet_validation_and_duplicate_rejection():
    conn = connect(":memory:")
    run_migrations(conn)
    _active_account(conn, "a", "a@example.test")
    with pytest.raises(CustomerDataError):
        add_wallet(conn, "a", "not-a-wallet")
    wallet = "11111111111111111111111111111111"
    add_wallet(conn, "a", wallet)
    with pytest.raises(CustomerDataError):
        add_wallet(conn, "a", wallet)


def test_paper_accounts_are_private_and_capital_is_validated():
    conn = connect(":memory:")
    run_migrations(conn)
    _active_account(conn, "a", "a@example.test")
    _active_account(conn, "b", "b@example.test")
    paper = create_paper_account(conn, "a", 250)
    assert paper["cash_balance_usd"] == 250
    assert len(list_paper_accounts(conn, "a")) == 1
    assert list_paper_accounts(conn, "b") == []
    assert get_paper_account(conn, "b", paper["id"]) is None
    with pytest.raises(CustomerDataError):
        create_paper_account(conn, "a", 4)
