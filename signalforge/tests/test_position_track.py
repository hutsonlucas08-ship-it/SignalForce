from datetime import datetime, timedelta, timezone

from app.db import connect, run_migrations
from app.position_track import forget, mark_partial, observe


def test_position_tracking_remembers_peak_and_partial_flag():
    conn = connect(":memory:")
    run_migrations(conn)
    first = observe(conn, "mint-a", 1.0, 1.2, 50000)
    assert first["peak_price"] == 1.2
    second = observe(conn, "mint-a", 1.0, 1.1, 40000)
    assert second["peak_price"] == 1.2
    assert second["entry_liquidity_usd"] == 50000
    mark_partial(conn, "mint-a")
    assert observe(conn, "mint-a", 1.0, 1.3, 30000)["partial_taken"] == 1
    forget(conn, "mint-a")
    assert conn.execute("SELECT 1 FROM position_tracking WHERE asset_id='mint-a'").fetchone() is None


def test_position_tracking_marks_unpriced_since_and_clears_it_when_price_returns():
    conn = connect(":memory:")
    run_migrations(conn)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = observe(conn, "mint-b", 1.0, None, None, now)
    assert row["unpriced_since"] == now
    row = observe(conn, "mint-b", 1.0, 1.1, 20000)
    assert row["unpriced_since"] is None
    assert row["peak_price"] == 1.1
