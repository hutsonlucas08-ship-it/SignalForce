from datetime import datetime, timezone

from app import customer_data
from app.customer_paper_bot import process_customer_accounts
from app.db import connect, run_migrations, utcnow


def _active_customer(conn):
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_accounts(id,email,password_hash,status,created_at,updated_at) "
        "VALUES('customer-a','customer@example.test','test-hash','active',?,?)",
        (now, now),
    )
    conn.commit()
    return customer_data.start_customer_paper_trader(
        conn, "customer-a", 1000, 500, "normal"
    )


def _market_pair():
    return {
        "chainId": "solana",
        "baseToken": {"address": "MarketMint11111111111111111111111111", "name": "Market Candidate", "symbol": "MKT"},
        "priceUsd": "0.01",
        "marketCap": 250000,
        "liquidity": {"usd": 50000},
        "volume": {"m5": 6000, "h1": 25000},
        "txns": {"m5": {"buys": 30, "sells": 10}, "h1": {"buys": 120, "sells": 80}},
        "priceChange": {"m5": 3.5, "h1": 7.0},
    }


def test_customer_paper_trader_can_enter_market_discovery_candidate_without_wallet_signal():
    conn = connect(":memory:")
    run_migrations(conn)
    _active_customer(conn)

    mint = "MarketMint11111111111111111111111111"
    result = process_customer_accounts(conn, {mint: _market_pair()}, {}, {mint: "Market Candidate"})

    assert result["buys"] == 1
    assert result["wallet_signal_count"] == 0
    trade = conn.execute(
        "SELECT side,reason,amount_usd FROM customer_paper_trades WHERE paper_account_id IN "
        "(SELECT id FROM customer_paper_accounts WHERE account_id='customer-a')"
    ).fetchone()
    assert trade["side"] == "buy"
    assert "independent market momentum" in trade["reason"]
    assert trade["amount_usd"] == 50


def test_customer_paper_trader_keeps_market_only_score_floor_and_reports_rejections():
    conn = connect(":memory:")
    run_migrations(conn)
    _active_customer(conn)

    pair = _market_pair()
    pair["txns"]["m5"]["buys"] = 3
    pair["txns"]["m5"]["sells"] = 10
    result = process_customer_accounts(conn, {pair["baseToken"]["address"]: pair}, {})

    assert result["buys"] == 0
    assert result["rejection_reasons"]
    assert conn.execute("SELECT COUNT(*) FROM customer_paper_trades").fetchone()[0] == 0


def test_customer_paper_trader_still_accepts_wallet_confirmed_candidates():
    conn = connect(":memory:")
    run_migrations(conn)
    _active_customer(conn)

    mint = "MarketMint11111111111111111111111111"
    signals = {mint: {"wallet_count": 1, "buy_count": 1, "latest_buy_at": datetime.now(timezone.utc).isoformat()}}
    result = process_customer_accounts(conn, {mint: _market_pair()}, signals)

    assert result["buys"] == 1
    trade = conn.execute("SELECT reason FROM customer_paper_trades").fetchone()
    assert "tracked-wallet confirmed" in trade["reason"]
