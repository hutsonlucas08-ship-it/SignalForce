from app.paper_bot import _score


def candidate_pair():
    return {
        "priceUsd": "0.00042",
        "marketCap": 250000,
        "liquidity": {"usd": 50000},
        "volume": {"m5": 6000, "h1": 25000},
        "txns": {"m5": {"buys": 30, "sells": 10}, "h1": {"buys": 120, "sells": 80}},
        "priceChange": {"m5": 3.5, "h1": 7.0},
    }


def test_paper_bot_accepts_active_candidate_with_provider_string_price():
    score, reason = _score(candidate_pair())
    assert reason is None
    assert 0 < score <= 100


def test_paper_bot_rejects_token_without_recent_buy_activity():
    pair = candidate_pair()
    pair["txns"]["m5"]["buys"] = 0
    score, reason = _score(pair)
    assert score is None
    assert reason == "Not enough recent buy transactions"


def test_paper_bot_fails_closed_when_liquidity_is_missing():
    pair = candidate_pair()
    pair["liquidity"] = {}
    score, reason = _score(pair)
    assert score is None
    assert reason == "Liquidity below $15,000 or missing"

from app.db import connect, run_migrations
from app.paper_bot import bot_status, configure_bot


def test_paper_bot_starts_paused_and_requires_timed_manual_start():
    conn = connect(":memory:")
    run_migrations(conn)
    status = bot_status(conn)
    assert status["enabled"] is False
    assert status["status"] == "STOPPED"
    try:
        configure_bot(conn, enabled=True)
        assert False, "Starting without a selected duration must fail"
    except ValueError:
        pass
    status = configure_bot(conn, enabled=True, duration_seconds=1800)
    assert status["enabled"] is True
    assert status["session_seconds"] == 1800
    assert status["remaining_seconds"] > 0
    status = configure_bot(conn, enabled=False)
    assert status["enabled"] is False
    assert status["status"] == "STOPPED"
    assert status["run_stopped_at"] is not None


from datetime import datetime, timezone
from app.paper_bot import _recent_wallet_buy_signals, _trade_decisions


def _seed_recent_wallet_buy(conn, wallet, mint, minutes_ago=2):
    from app.db import utcnow
    conn.execute(
        "INSERT INTO tracked_wallets(wallet_address,label,added_at) VALUES(?,?,?)",
        (wallet, wallet[:6], utcnow()),
    )
    occurred_at = (datetime.now(timezone.utc) - __import__("datetime").timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO wallet_trade_events
        (wallet_address,signature,mint,side,occurred_at,source,observed_at)
        VALUES(?,?,?,'buy',?,'helius',?)""",
        (wallet, "sig-" + wallet[:8], mint, occurred_at, utcnow()),
    )
    conn.commit()


def _paper_strategy_db():
    conn = connect(":memory:")
    run_migrations(conn)
    return conn


def test_recent_smart_money_signal_requires_two_distinct_tracked_wallets_for_normal_score():
    conn = _paper_strategy_db()
    mint = "TokenMint111111111111111111111111111"
    _seed_recent_wallet_buy(conn, "WalletAddress1111111111111111111111", mint)
    _seed_recent_wallet_buy(conn, "WalletAddress2222222222222222222222", mint)
    signals = _recent_wallet_buy_signals(conn)
    assert signals[mint]["wallet_count"] == 2
    assert signals[mint]["buy_count"] == 2

    outcome = _trade_decisions(conn, [{"asset_id": mint, "token_name": "Signal Token"}],
                               {mint: candidate_pair()}, approved_mints={mint})
    assert len(outcome["entries"]) == 1
    assert outcome["entries"][0]["wallet_count"] == 2
    assert outcome["entries"][0]["market_score"] > 0


def test_paper_bot_does_not_enter_on_momentum_without_tracked_wallet_buy():
    conn = _paper_strategy_db()
    mint = "TokenMint222222222222222222222222222"
    outcome = _trade_decisions(conn, [{"asset_id": mint, "token_name": "Momentum Only"}],
                               {mint: candidate_pair()}, approved_mints={mint})
    assert outcome["entries"] == []
    assert outcome["candidate_count"] == 0


def test_old_tracked_wallet_buys_do_not_trigger_new_entry():
    conn = _paper_strategy_db()
    mint = "TokenMint333333333333333333333333333"
    _seed_recent_wallet_buy(conn, "WalletAddress3333333333333333333333", mint, minutes_ago=45)
    _seed_recent_wallet_buy(conn, "WalletAddress4444444444444444444444", mint, minutes_ago=45)
    outcome = _trade_decisions(conn, [{"asset_id": mint, "token_name": "Old Signal"}],
                               {mint: candidate_pair()}, approved_mints={mint})
    assert outcome["entries"] == []
    assert outcome["wallet_signal_count"] == 0


def test_single_wallet_buy_needs_exceptionally_strong_market_score():
    conn = _paper_strategy_db()
    mint = "TokenMint444444444444444444444444444"
    _seed_recent_wallet_buy(conn, "WalletAddress5555555555555555555555", mint)
    outcome = _trade_decisions(conn, [{"asset_id": mint, "token_name": "Single Wallet"}],
                               {mint: candidate_pair()}, approved_mints={mint})
    assert outcome["entries"] == []
    assert outcome["candidate_count"] == 0
