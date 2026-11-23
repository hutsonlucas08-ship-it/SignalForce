"""Persistence for paper-position peak price, entry liquidity, and partial-profit state."""
from typing import Optional

from .db import utcnow


def observe(conn, asset_id: str, entry_price: float, price: Optional[float],
            liquidity_usd: Optional[float], now_iso: Optional[str] = None) -> dict:
    now_iso = now_iso or utcnow()
    start_peak = max(entry_price, price) if price and price > 0 else entry_price
    conn.execute(
        "INSERT OR IGNORE INTO position_tracking "
        "(asset_id, first_seen_at, entry_liquidity_usd, peak_price) VALUES (?,?,?,?)",
        (asset_id, now_iso, liquidity_usd, start_peak),
    )
    if price and price > 0:
        conn.execute(
            "UPDATE position_tracking SET peak_price=MAX(peak_price, ?), unpriced_since=NULL, "
            "entry_liquidity_usd=COALESCE(entry_liquidity_usd, ?) WHERE asset_id=?",
            (price, liquidity_usd, asset_id),
        )
    else:
        conn.execute(
            "UPDATE position_tracking SET unpriced_since=COALESCE(unpriced_since, ?) WHERE asset_id=?",
            (now_iso, asset_id),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM position_tracking WHERE asset_id=?", (asset_id,)).fetchone()
    return dict(row)


def mark_partial(conn, asset_id: str) -> None:
    conn.execute("UPDATE position_tracking SET partial_taken=1 WHERE asset_id=?", (asset_id,))
    conn.commit()


def forget(conn, asset_id: str) -> None:
    conn.execute("DELETE FROM position_tracking WHERE asset_id=?", (asset_id,))
    conn.commit()
