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
    assert status["status"] == "PAUSED"
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
