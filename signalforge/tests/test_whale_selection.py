from app.whale_selection import qualify_traders


def _snapshot(window, row):
    return {
        "snapshot_type": "trader_leaderboard_" + window,
        "payload": {"window": window, "traders": [row]},
    }


def _row():
    return {
        "user": {"id": "user-1", "userHandle": "example"},
        "stats": {
            "pnlUsd": 1200,
            "winRate": 0.61,
            "totalTrades": 25,
            "medianHoldMinutes": 18,
            "maxSingleTokenPnlShare": 0.25,
        },
    }


def test_qualifies_only_with_two_windows_and_all_strict_7d_metrics():
    row = _row()
    result = qualify_traders([
        _snapshot("24h", row),
        _snapshot("7d", row),
    ])
    assert result["qualified_count"] == 1
    trader = result["qualified_traders"][0]
    assert trader["handle"] == "example"
    assert trader["qualification"] == "leaderboard_qualified_not_chain_verified"


def test_missing_hold_or_concentration_data_fails_closed():
    row = _row()
    del row["stats"]["medianHoldMinutes"]
    del row["stats"]["maxSingleTokenPnlShare"]
    result = qualify_traders([
        _snapshot("24h", row),
        _snapshot("7d", row),
    ])
    assert result["qualified_count"] == 0
    reasons = result["rejected_sample"][0]["reasons"]
    assert "median_hold_under_15m_or_missing" in reasons
    assert "single_token_pnl_concentration_over_40pct_or_missing" in reasons


def test_requires_two_leaderboard_windows_and_twenty_7d_trades():
    row = _row()
    row["stats"]["totalTrades"] = 19
    result = qualify_traders([_snapshot("7d", row)])
    assert result["qualified_count"] == 0
    reasons = result["rejected_sample"][0]["reasons"]
    assert "not_in_two_leaderboard_windows" in reasons
    assert "fewer_than_20_7d_trades_or_missing" in reasons
