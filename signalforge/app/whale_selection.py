"""Strict FOMO leaderboard qualification; missing evidence always fails closed.

This is a leaderboard eligibility screen, not proof of profitable execution or an
on-chain-verified whale identity. It never creates orders or trade instructions.
"""
from __future__ import annotations

import math


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", "")
            if value.endswith("%"):
                return float(value[:-1]) / 100.0
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _sources(row):
    if not isinstance(row, dict):
        return []
    return [
        row,
        row.get("stats") if isinstance(row.get("stats"), dict) else {},
        row.get("performance") if isinstance(row.get("performance"), dict) else {},
        row.get("metrics") if isinstance(row.get("metrics"), dict) else {},
        row.get("user") if isinstance(row.get("user"), dict) else {},
    ]


def _metric(row, keys):
    for source in _sources(row):
        for key in keys:
            if key in source:
                value = _number(source.get(key))
                if value is not None:
                    return value
    return None


def _identity(row):
    user = row.get("user") if isinstance(row.get("user"), dict) else {}
    user_id = user.get("userId") or user.get("user_id") or user.get("id") or row.get("userId") or row.get("user_id")
    if isinstance(user_id, int) and not isinstance(user_id, bool):
        user_id = str(user_id)
    if not isinstance(user_id, str) or not user_id.strip():
        return None
    handle = user.get("userHandle") or user.get("handle") or user.get("username") or user.get("displayName") or row.get("userHandle") or row.get("handle") or row.get("username") or "FOMO trader"
    return user_id.strip(), str(handle)[:80]


def _window(snapshot):
    payload = snapshot.get("payload") or {}
    value = payload.get("window")
    if not value:
        suffix = snapshot.get("snapshot_type", "").removeprefix("trader_leaderboard_")
        value = suffix if suffix in {"24h", "7d", "30d"} else "unknown"
    value = str(value).lower()
    return value if value in {"24h", "7d", "30d"} else "unknown"


def qualify_traders(snapshots, *, limit=25):
    """Require 2/3 leaderboard windows and all strict 7d metrics before qualifying."""
    records = {}
    rejected = []
    for snapshot in snapshots:
        if not str(snapshot.get("snapshot_type", "")).startswith("trader_leaderboard"):
            continue
        window = _window(snapshot)
        payload = snapshot.get("payload") or {}
        rows = payload.get("traders") or []
        if not isinstance(rows, list):
            continue
        for row in rows[:50]:
            if not isinstance(row, dict):
                continue
            identity = _identity(row)
            if not identity:
                continue
            user_id, handle = identity
            item = records.setdefault(user_id, {"user_id": user_id, "handle": handle, "windows": {}, "row_7d": None})
            item["windows"][window] = row
            if window == "7d":
                item["row_7d"] = row

    qualified = []
    for user_id, item in records.items():
        row = item["row_7d"]
        reasons = []
        if len({window for window in item["windows"] if window != "unknown"}) < 2:
            reasons.append("not_in_two_leaderboard_windows")
        if row is None:
            reasons.append("missing_7d_leaderboard")
            metrics = {}
        else:
            pnl = _metric(row, ("pnlUsd", "pnl", "profitUsd", "profit", "pnl_usd"))
            win_rate = _metric(row, ("winRate", "win_rate"))
            trades = _metric(row, ("totalTrades", "numTrades", "swapCount", "total_trades"))
            hold_minutes = _metric(row, ("medianHoldMinutes", "median_hold_minutes"))
            hold_seconds = _metric(row, ("medianHoldSeconds", "median_hold_seconds"))
            if hold_minutes is None and hold_seconds is not None:
                hold_minutes = hold_seconds / 60.0
            concentration = _metric(row, (
                "maxSingleTokenPnlShare", "topTokenPnlShare", "singleTokenPnlShare", "max_single_token_pnl_share"
            ))
            if win_rate is not None and win_rate > 1:
                win_rate /= 100.0
            if concentration is not None and concentration > 1:
                concentration /= 100.0
            metrics = {
                "pnl_7d": pnl,
                "win_rate": win_rate,
                "trades_7d": int(trades) if trades is not None else None,
                "median_hold_minutes": hold_minutes,
                "max_single_token_pnl_share": concentration,
            }
            if pnl is None or pnl <= 0:
                reasons.append("non_positive_or_missing_7d_pnl")
            if win_rate is None or not 0 <= win_rate <= 1 or win_rate < 0.55:
                reasons.append("win_rate_below_55pct_or_missing")
            if trades is None or trades < 20:
                reasons.append("fewer_than_20_7d_trades_or_missing")
            if hold_minutes is None or hold_minutes < 15:
                reasons.append("median_hold_under_15m_or_missing")
            if concentration is None or concentration < 0 or concentration > 0.40:
                reasons.append("single_token_pnl_concentration_over_40pct_or_missing")
        if reasons:
            rejected.append({"user_id": user_id, "handle": item["handle"], "reasons": reasons})
            continue
        score = metrics["pnl_7d"] * metrics["win_rate"]
        qualified.append({
            "user_id": user_id,
            "handle": item["handle"],
            "leaderboard_windows": sorted(window for window in item["windows"] if window != "unknown"),
            "metrics": metrics,
            "rank_score": round(score, 6),
            "qualification": "leaderboard_qualified_not_chain_verified",
        })

    qualified.sort(key=lambda row: row["rank_score"], reverse=True)
    return {
        "qualified_traders": qualified[:max(1, min(int(limit), 25))],
        "qualified_count": len(qualified),
        "observed_trader_count": len(records),
        "rejected_count": len(rejected),
        "rejected_sample": rejected[:25],
        "rules": {
            "leaderboard_windows_required": 2,
            "win_rate_min": 0.55,
            "trades_7d_min": 20,
            "median_hold_minutes_min": 15,
            "max_single_token_pnl_share": 0.40,
            "rank_score": "7d PnL × win rate",
        },
        "note": "Missing metrics fail closed. Qualification is based on FOMO leaderboard fields only and is not chain-verified proof of realized profit or a buy/sell signal.",
    }
