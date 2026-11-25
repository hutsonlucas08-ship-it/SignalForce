from datetime import datetime, timezone

from app import customer_data
from app.customer_paper_bot import (_adaptive_position_amount, _runner_assessment,
                                    monitor_customer_positions, process_customer_accounts)
from app.db import connect, run_migrations, utcnow


def _active_customer(conn, mode="normal"):
    now = utcnow()
    conn.execute(
        "INSERT INTO customer_accounts(id,email,password_hash,status,created_at,updated_at) "
        "VALUES('customer-a','customer@example.test','test-hash','active',?,?)",
        (now, now),
    )
    conn.commit()
    return customer_data.start_customer_paper_trader(
        conn, "customer-a", 1000, 500, mode
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
    assert trade["amount_usd"] == 42.5
    assert "runner" in trade["reason"] and "size 0.85x" in trade["reason"]


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



def test_ultra_aggressive_sells_40_percent_at_50_percent_then_trails_remaining_position():
    conn = connect(":memory:")
    run_migrations(conn)
    _active_customer(conn, "ultra_aggressive")
    mint = "MarketMint11111111111111111111111111"
    entry = _market_pair()
    process_customer_accounts(conn, {mint: entry}, {})
    position = conn.execute("SELECT quantity FROM customer_paper_positions").fetchone()
    original_quantity = float(position["quantity"])

    rally = _market_pair()
    rally["priceUsd"] = "0.015"
    summary = monitor_customer_positions(conn, {mint: rally})
    remaining = conn.execute("SELECT quantity,partial_profit_taken FROM customer_paper_positions").fetchone()

    assert summary["partial_exits"] == 1
    assert remaining["partial_profit_taken"] == 1
    assert abs(float(remaining["quantity"]) - original_quantity * 0.60) < 1e-8

    stronger_rally = _market_pair()
    stronger_rally["priceUsd"] = "0.02"
    monitor_customer_positions(conn, {mint: stronger_rally})
    trailing_pullback = _market_pair()
    trailing_pullback["priceUsd"] = "0.015"
    summary = monitor_customer_positions(conn, {mint: trailing_pullback})

    assert summary["take_profit_exits"] == 1
    assert conn.execute("SELECT COUNT(*) FROM customer_paper_positions").fetchone()[0] == 0


def test_shared_risk_monitor_exits_position_after_major_liquidity_drop():
    conn = connect(":memory:")
    run_migrations(conn)
    _active_customer(conn, "gradual")
    mint = "MarketMint11111111111111111111111111"
    process_customer_accounts(conn, {mint: _market_pair()}, {})
    dangerous = _market_pair()
    dangerous["liquidity"]["usd"] = 30000
    summary = monitor_customer_positions(conn, {mint: dangerous})

    assert summary["risk_exits"] == 1
    assert conn.execute("SELECT COUNT(*) FROM customer_paper_positions").fetchone()[0] == 0


def test_runner_confidence_rewards_wallet_accumulation_and_quality():
    from app.customer_paper_bot import _runner_assessment
    pair = _market_pair()
    market_score = 56.0
    market_only = _runner_assessment(pair, market_score, {})
    wallet_confirmed = _runner_assessment(
        pair, market_score,
        {"wallet_count": 3, "buy_count": 4, "buy_usd": 15000},
    )
    assert wallet_confirmed["score"] > market_only["score"]
    assert wallet_confirmed["tier"] in {"promising", "strong", "exceptional"}
    assert any("tracked wallet" in reason for reason in wallet_confirmed["reasons"])


def test_adaptive_sizing_allocates_more_to_higher_confidence_but_respects_caps():
    account = {"cash_balance_usd": 1000, "_equity_usd": 1000}
    rules = {
        "position_fraction": 0.10,
        "max_position_fraction": 0.15,
    }
    developing, _ = _adaptive_position_amount(account, rules, 62, 100000)
    exceptional, multiplier = _adaptive_position_amount(account, rules, 95, 100000)
    assert exceptional > developing
    assert exceptional == 150
    assert multiplier == 1.5
    # Even exceptional confidence cannot exceed the pool-impact cap.
    thin_pool, _ = _adaptive_position_amount(account, rules, 95, 5000)
    assert thin_pool == 50


def test_normal_mode_banks_partial_profit_then_protects_remaining_gain():
    conn = connect(":memory:")
    run_migrations(conn)
    _active_customer(conn, "normal")
    mint = "MarketMint11111111111111111111111111"
    process_customer_accounts(conn, {mint: _market_pair()}, {})
    position = conn.execute("SELECT quantity FROM customer_paper_positions").fetchone()
    original_quantity = float(position["quantity"])

    rally = _market_pair()
    rally["priceUsd"] = "0.0135"
    summary = monitor_customer_positions(conn, {mint: rally})
    remaining = conn.execute(
        "SELECT quantity,partial_profit_taken FROM customer_paper_positions"
    ).fetchone()
    assert summary["partial_exits"] == 1
    assert remaining["partial_profit_taken"] == 1
    assert abs(float(remaining["quantity"]) - original_quantity * 0.60) < 1e-8

    pullback = _market_pair()
    pullback["priceUsd"] = "0.011"
    summary = monitor_customer_positions(conn, {mint: pullback})
    assert summary["take_profit_exits"] == 1
    assert conn.execute("SELECT COUNT(*) FROM customer_paper_positions").fetchone()[0] == 0


def test_automated_paper_trade_truncates_long_internal_reason_without_stopping_worker():
    conn = connect(":memory:")
    run_migrations(conn)
    account = _active_customer(conn)
    result = customer_data.place_paper_trade(
        conn, "customer-a", account["id"],
        asset_id="MarketMint11111111111111111111111111",
        token_name="Market Candidate", side="buy", amount_usd=25,
        price_usd=0.01, reason="automated risk explanation " * 20,
        automated=True,
    )
    assert result["side"] == "buy"
    row = conn.execute("SELECT reason FROM customer_paper_trades WHERE paper_account_id=?",
                       (account["id"],)).fetchone()
    assert len(row["reason"]) == 200


def test_manual_paper_trade_still_rejects_overlong_reason():
    conn = connect(":memory:")
    run_migrations(conn)
    account = _active_customer(conn)
    try:
        customer_data.place_paper_trade(
            conn, "customer-a", account["id"],
            asset_id="MarketMint11111111111111111111111111",
            token_name="Market Candidate", side="buy", amount_usd=25,
            price_usd=0.01, reason="manual reason " * 20,
        )
    except customer_data.CustomerDataError as exc:
        assert "1–200" in str(exc)
    else:
        raise AssertionError("Manual trade reason validation should remain strict")