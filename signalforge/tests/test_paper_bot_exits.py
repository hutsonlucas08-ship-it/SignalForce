from app.db import connect, run_migrations
from app import paper
from app.paper_bot import _trade_decisions
from app.position_track import observe


def _db_with_position():
    conn = connect(":memory:")
    run_migrations(conn)
    paper.ensure_accounts(conn)
    paper.place_order(conn, "automatic", "mint-exit-test", "Exit Test", "buy", 100, 1.0, "test entry")
    row = conn.execute("SELECT * FROM paper_positions WHERE asset_id='mint-exit-test'").fetchone()
    return conn, dict(row)


def test_paper_bot_takes_one_partial_profit_and_keeps_remainder_open():
    conn, pos = _db_with_position()
    price = pos["avg_entry_price"] * 2.0
    outcome = _trade_decisions(conn, [], {"mint-exit-test": {
        "priceUsd": str(price), "liquidity": {"usd": 50000}
    }})
    assert len(outcome["exits"]) == 1
    assert outcome["exits"][0]["partial"] is True
    remaining = conn.execute("SELECT quantity FROM paper_positions WHERE asset_id='mint-exit-test'").fetchone()
    assert remaining is not None
    assert remaining["quantity"] < pos["quantity"]
    tracked = conn.execute("SELECT partial_taken FROM position_tracking WHERE asset_id='mint-exit-test'").fetchone()
    assert tracked["partial_taken"] == 1


def test_paper_bot_uses_persisted_peak_for_trailing_stop():
    conn, pos = _db_with_position()
    entry = pos["avg_entry_price"]
    observe(conn, "mint-exit-test", entry, entry * 2.0, 50000)
    price = entry * 1.6
    outcome = _trade_decisions(conn, [], {"mint-exit-test": {
        "priceUsd": str(price), "liquidity": {"usd": 50000}
    }})
    assert len(outcome["exits"]) == 1
    assert outcome["exits"][0]["partial"] is False
    assert "trailing stop" in outcome["exits"][0]["reason"]
    assert conn.execute("SELECT 1 FROM paper_positions WHERE asset_id='mint-exit-test'").fetchone() is None
    assert conn.execute("SELECT 1 FROM position_tracking WHERE asset_id='mint-exit-test'").fetchone() is None


def test_paper_bot_reports_open_position_without_a_price():
    conn, _ = _db_with_position()
    observe(conn, "mint-exit-test", 1.0, None, None, "2026-11-22T20:00:00Z")
    outcome = _trade_decisions(conn, [], {})
    assert len(outcome["stale_positions"]) == 1
    assert outcome["stale_positions"][0]["asset_id"] == "mint-exit-test"
