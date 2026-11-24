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


def test_customer_paper_buy_sell_updates_cash_positions_and_realized_pnl():
    from app.customer_data import list_paper_positions, list_paper_trades, place_paper_trade
    conn = connect(":memory:")
    run_migrations(conn)
    _active_account(conn, "a", "a@example.test")
    paper = create_paper_account(conn, "a", 500)
    bought = place_paper_trade(
        conn, "a", paper["id"], asset_id="mint-a", side="buy",
        amount_usd=100, price_usd=2, token_name="Token A",
    )
    assert bought["quantity"] == 50
    assert bought["cash_balance_usd"] == 400
    assert len(list_paper_positions(conn, "a", paper["id"])) == 1
    sold = place_paper_trade(
        conn, "a", paper["id"], asset_id="mint-a", side="sell",
        amount_usd=60, price_usd=3,
    )
    assert sold["quantity"] == 20
    assert sold["realized_pnl_usd"] == 20
    assert sold["cash_balance_usd"] == 460
    assert list_paper_positions(conn, "a", paper["id"])[0]["quantity"] == 30
    assert len(list_paper_trades(conn, "a", paper["id"])) == 2


def test_customer_paper_trades_reject_overspending_and_cross_tenant_access():
    from app.customer_data import place_paper_trade
    conn = connect(":memory:")
    run_migrations(conn)
    _active_account(conn, "a", "a@example.test")
    _active_account(conn, "b", "b@example.test")
    paper = create_paper_account(conn, "a", 50)
    with pytest.raises(CustomerDataError):
        place_paper_trade(conn, "a", paper["id"], asset_id="mint-a", side="buy",
                          amount_usd=51, price_usd=1)
    with pytest.raises(CustomerDataError):
        place_paper_trade(conn, "b", paper["id"], asset_id="mint-a", side="buy",
                          amount_usd=10, price_usd=1)


def test_customer_automated_paper_traders_are_independent_and_pause_at_profit_target():
    from app.customer_data import (
        get_customer_paper_trader, pause_customer_paper_trader,
        start_customer_paper_trader,
    )
    from app.customer_paper_bot import process_customer_accounts

    conn = connect(":memory:")
    run_migrations(conn)
    _active_account(conn, "a", "a@example.test")
    _active_account(conn, "b", "b@example.test")
    trader_a = start_customer_paper_trader(conn, "a", 1000, 100, "gradual")
    assert trader_a["is_running"] is True
    assert trader_a["strategy_mode"] == "gradual"
    assert trader_a["profit_target_usd"] == 100
    assert get_customer_paper_trader(conn, "b") is None

    # A real simulated position doubles in value, is closed by the mode's
    # take-profit rule, and reaches only tenant A's dollar profit target.
    from app.customer_data import place_paper_trade
    place_paper_trade(conn, "a", trader_a["id"], asset_id="mint-a",
                      side="buy", amount_usd=100, price_usd=1)
    pair = {"priceUsd": "2", "baseToken": {"name": "Token A"}}
    result = process_customer_accounts(conn, {"mint-a": pair}, {})
    assert result["sells"] == 1
    assert result["targets_reached"] == 1
    reached = get_customer_paper_trader(conn, "a")
    assert reached["auto_status"] == "target_reached"
    assert reached["is_running"] is False
    assert get_customer_paper_trader(conn, "b") is None

    paused = pause_customer_paper_trader(conn, "a")
    assert paused["auto_status"] == "target_reached"

    restarted = start_customer_paper_trader(conn, "a", 500, 200, "aggressive")
    assert restarted["auto_status"] == "running"
    assert restarted["strategy_mode"] == "aggressive"
    assert restarted["starting_capital_usd"] == 500
    assert restarted["cash_balance_usd"] == 500
    assert restarted["positions"] == []



def test_first_automated_setup_resets_legacy_manual_simulation_state():
    from app.customer_data import list_paper_trades, place_paper_trade, start_customer_paper_trader
    conn = connect(":memory:")
    run_migrations(conn)
    _active_account(conn, "legacy", "legacy@example.test")
    old = create_paper_account(conn, "legacy", 250)
    place_paper_trade(conn, "legacy", old["id"], asset_id="old-mint",
                      side="buy", amount_usd=50, price_usd=1)
    trader = start_customer_paper_trader(conn, "legacy", 1000, 500, "normal")
    assert trader["starting_capital_usd"] == 1000
    assert trader["cash_balance_usd"] == 1000
    assert trader["positions"] == []
    assert trader["auto_status"] == "running"
    assert list_paper_trades(conn, "legacy", trader["id"]) == []
    place_paper_trade(conn, "legacy", trader["id"], asset_id="new-mint",
                      side="buy", amount_usd=25, price_usd=1)
    visible = list_paper_trades(conn, "legacy", trader["id"])
    assert len(visible) == 1 and visible[0]["asset_id"] == "new-mint"


def test_customer_paper_worker_lease_prevents_duplicate_workers():
    from app.customer_paper_bot import _claim_worker_lease, _release_worker_lease
    conn = connect(":memory:")
    run_migrations(conn)
    assert _claim_worker_lease(conn, "worker-a") is True
    assert _claim_worker_lease(conn, "worker-b") is False
    _release_worker_lease(conn, "worker-a")
    assert _claim_worker_lease(conn, "worker-b") is True
    _release_worker_lease(conn, "worker-b")
    conn.close()
