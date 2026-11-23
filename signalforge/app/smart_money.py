"""Deterministic, evidence-only calculations for SignalForge smart-money tracking.

This module intentionally performs no network access and never invents missing
entry prices, token prices, market caps, or wallet rankings.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import mean
from typing import Any, Iterable


def calculate_multiple(entry_price: float | None, current_price: float | None) -> float | None:
    """Return current/entry price, or None when either observed price is unusable."""
    if entry_price is None or current_price is None:
        return None
    try:
        entry = float(entry_price)
        current = float(current_price)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or current <= 0:
        return None
    return current / entry


def summarize_token(
    trades: Iterable[dict[str, Any]],
    prices: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize wallet overlap and observed multiples for one mint.

    A wallet's first recorded BUY with a valid USD entry price is its reference
    entry. Peak X is based only on price observations at or after that entry.
    Missing prices remain None and are excluded from averages.
    """
    buys_by_wallet: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        if trade.get("side") != "buy" or not trade.get("wallet_address"):
            continue
        buys_by_wallet[str(trade["wallet_address"])].append(trade)

    price_rows = []
    for row in prices:
        try:
            value = float(row.get("price_usd"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            price_rows.append((str(row.get("observed_at") or ""), value))
    price_rows.sort(key=lambda item: item[0])
    current_price = price_rows[-1][1] if price_rows else None

    wallet_rows = []
    current_multiples = []
    peak_multiples = []
    for wallet, wallet_trades in buys_by_wallet.items():
        wallet_trades.sort(key=lambda row: str(row.get("occurred_at") or ""))
        first_buy = wallet_trades[0]
        entry_price = first_buy.get("price_usd")
        entry_time = str(first_buy.get("occurred_at") or "")
        current_x = calculate_multiple(entry_price, current_price)
        peak_prices = [price for timestamp, price in price_rows if timestamp >= entry_time]
        peak_x = max((calculate_multiple(entry_price, price) for price in peak_prices), default=None)
        if current_x is not None:
            current_multiples.append(current_x)
        if peak_x is not None:
            peak_multiples.append(peak_x)
        wallet_rows.append({
            "wallet_address": wallet,
            "entry_price_usd": entry_price,
            "entry_at": entry_time or None,
            "current_x": current_x,
            "peak_x": peak_x,
            "buy_count": len(wallet_trades),
            "position_usd": sum(
                float(row.get("usd_amount") or 0)
                for row in wallet_trades
                if row.get("usd_amount") is not None
            ),
            "source": first_buy.get("source"),
        })

    wallet_rows.sort(
        key=lambda row: (row["current_x"] is not None, row["current_x"] or 0),
        reverse=True,
    )
    return {
        "wallet_count": len(wallet_rows),
        "current_price_usd": current_price,
        "average_entry_to_current_x": mean(current_multiples) if current_multiples else None,
        "best_peak_x": max(peak_multiples) if peak_multiples else None,
        "wallets": wallet_rows,
        "data_quality": {
            "wallets_with_entry_price": sum(row["entry_price_usd"] is not None for row in wallet_rows),
            "wallets_with_current_multiple": len(current_multiples),
            "price_observation_count": len(price_rows),
            "note": "Multiples are computed only where observed entry and current prices exist. They do not represent realized PnL."
        },
    }


def find_wallet_overlap(trades: Iterable[dict[str, Any]], minimum_wallets: int = 2) -> list[dict[str, Any]]:
    """Group distinct buyers by mint; repeated buys by one wallet count once."""
    if minimum_wallets < 2:
        raise ValueError("minimum_wallets must be at least 2")
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for trade in trades:
        mint = trade.get("mint")
        wallet = trade.get("wallet_address")
        if trade.get("side") != "buy" or not mint or not wallet:
            continue
        grouped[str(mint)].setdefault(str(wallet), trade)

    results = []
    for mint, wallets in grouped.items():
        if len(wallets) < minimum_wallets:
            continue
        first_seen = sorted(
            str(row.get("occurred_at") or "") for row in wallets.values()
        )
        results.append({
            "mint": mint,
            "wallet_count": len(wallets),
            "wallets": sorted(wallets),
            "first_buy_at": first_seen[0] if first_seen else None,
            "latest_buy_at": first_seen[-1] if first_seen else None,
        })
    return sorted(results, key=lambda row: (row["wallet_count"], row["latest_buy_at"] or ""), reverse=True)
