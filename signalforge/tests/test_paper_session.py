from app import paper
from app.db import connect, run_migrations, utcnow


def test_session_overview_excludes_previous_simulation_activity_and_positions():
    conn = connect(":memory:")
    run_migrations(conn)

    # A previous-session buy and holding must not appear in the new session view.
    paper.place_order(conn, "automatic", "old-token", "Old Token", "buy", 100, 1, "old session")
    conn.execute("UPDATE paper_trades SET created_at='2020-01-01T00:00:00Z'")
    conn.execute("UPDATE paper_positions SET opened_at='2020-01-01T00:00:00Z'")
    conn.commit()

    started = utcnow()
    paper.place_order(conn, "automatic", "new-token", "New Token", "buy", 100, 1, "session entry")
    paper.place_order(conn, "automatic", "new-token", "New Token", "sell", 20, 2, "session partial exit")

    result = paper.session_overview(conn, started, active=True)

    assert result["active"] is True
    assert result["trade_count"] == 2
    assert result["buy_count"] == 1
    assert result["sell_count"] == 1
    assert result["invested_usd"] == 100.0
    assert result["realized_pnl_usd"] > 0
    assert len(result["open_positions"]) == 1
    assert result["open_positions"][0]["asset_id"] == "new-token"
    assert {trade["asset_id"] for trade in result["trades"]} == {"new-token"}
    assert result["runtime_seconds"] >= 0
    conn.close()


def test_session_overview_is_empty_before_first_session():
    conn = connect(":memory:")
    run_migrations(conn)
    result = paper.session_overview(conn, None)
    assert result["session_started_at"] is None
    assert result["trade_count"] == 0
    assert result["invested_usd"] == 0
    assert result["open_positions"] == []
    conn.close()
