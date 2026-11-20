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
