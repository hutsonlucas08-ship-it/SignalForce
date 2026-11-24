"""Explainable, read-only manual trading callouts from fresh Solana market data.

This module intentionally does not invent entries, stops, targets, whale rankings,
or execution instructions. It surfaces candidates for human review only.
"""
from __future__ import annotations

from datetime import datetime, timezone


SOLANA_QUOTE_ASSETS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkGZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
_BASE58 = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def is_solana_mint(value):
    """Conservatively recognize a Solana public-key-shaped mint, excluding quote assets."""
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 44
        and value not in SOLANA_QUOTE_ASSETS
        and all(character in _BASE58 for character in value)
    )


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        if number == number and abs(number) != float("inf"):
            return number
    except (TypeError, ValueError):
        pass
    return None


def build_callouts(assets, pairs, *, now=None, max_age_seconds=300):
    now = now or datetime.now(timezone.utc)
    by_mint = {}
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        base = pair.get("baseToken") or {}
        mint = base.get("address")
        if not isinstance(mint, str) or not mint:
            continue
        liquidity = max(0, _num((pair.get("liquidity") or {}).get("usd")) or 0)
        previous = by_mint.get(mint)
        if previous is None or liquidity > previous[0]:
            by_mint[mint] = (liquidity, pair)

    callouts = []
    rejected = {"stale": 0, "missing_market_data": 0, "low_liquidity": 0,
                "low_volume": 0, "sell_pressure": 0}
    for asset in assets:
        mint = asset.get("asset_id")
        match = by_mint.get(mint)
        if not match:
            rejected["missing_market_data"] += 1
            continue
        _, pair = match
        price = _num(pair.get("priceUsd"))
        liquidity = _num((pair.get("liquidity") or {}).get("usd")) or 0
        volume = pair.get("volume") or {}
        txns = pair.get("txns") or {}
        changes = pair.get("priceChange") or {}
        volume_1h = max(0, _num(volume.get("h1")) or 0)
        buys_5m = max(0, _num((txns.get("m5") or {}).get("buys")) or 0)
        sells_5m = max(0, _num((txns.get("m5") or {}).get("sells")) or 0)
        change_5m = _num(changes.get("m5"))
        change_1h = _num(changes.get("h1"))
        try:
            seen = datetime.strptime(asset.get("latest_collected_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = max(0, (now - seen).total_seconds())
        except (TypeError, ValueError):
            age = None
        if age is None or age > max_age_seconds:
            rejected["stale"] += 1
            continue
        if price is None or price <= 0:
            rejected["missing_market_data"] += 1
            continue
        if liquidity < 25000:
            rejected["low_liquidity"] += 1
            continue
        if volume_1h < 10000:
            rejected["low_volume"] += 1
            continue
        if sells_5m >= max(12, buys_5m * 2) and change_5m is not None and change_5m <= -8:
            rejected["sell_pressure"] += 1
            continue

        reasons = ["fresh market observation", "24h/1h pool data available"]
        score = 50.0
        if liquidity >= 100000:
            score += 15
            reasons.append("liquidity at least $100k")
        elif liquidity >= 50000:
            score += 10
            reasons.append("liquidity at least $50k")
        else:
            score += 5
            reasons.append("liquidity at least $25k")
        if buys_5m + sells_5m:
            buy_ratio = buys_5m / (buys_5m + sells_5m)
            if buy_ratio >= .65:
                score += 10
                reasons.append("5m buys outweigh sells")
            elif buy_ratio < .5:
                score -= 10
                reasons.append("5m sells outweigh buys")
        else:
            buy_ratio = None
            reasons.append("no 5m transaction sample")
        if change_5m is not None and 0 < change_5m <= 10:
            score += 5
            reasons.append("positive but not extreme 5m move")
        elif change_5m is not None and change_5m > 20:
            score -= 10
            reasons.append("sharp 5m move; elevated chase risk")
        if change_1h is not None and change_1h < -10:
            score -= 10
            reasons.append("negative 1h momentum")
        score = round(max(0, min(100, score)), 1)
        base = pair.get("baseToken") or {}
        callouts.append({
            "asset_id": mint,
            "name": (asset.get("token_name") or base.get("name") or base.get("symbol") or "Unknown token")[:100],
            "symbol": (base.get("symbol") or "")[:24],
            "price_usd": price,
            "liquidity_usd": liquidity,
            "volume_1h_usd": volume_1h,
            "buys_5m": int(buys_5m),
            "sells_5m": int(sells_5m),
            "buy_ratio_5m": round(buy_ratio, 3) if buy_ratio is not None else None,
            "change_5m_pct": change_5m,
            "change_1h_pct": change_1h,
            "score": score,
            "tier": "watch" if score >= 70 else "review",
            "reasons": reasons,
            "detected_at": asset.get("latest_collected_at"),
            "age_seconds": int(age),
            "market_url": "https://dexscreener.com/solana/" + str(pair.get("pairAddress") or mint),
            "solscan_url": "https://solscan.io/token/" + mint,
            "fomo_phone_url": "https://fomo.family",
            "fomo_web_url": "https://fomo.family/discover",
            "plan_status": "market_watch_only",
            "entry": None,
            "stop": None,
            "targets": [],
            "limitations": [
                "No validated entry, stop, or target model is configured.",
                "DexScreener metrics are provider-reported and may be delayed.",
                "Liquidity and volume do not prove token safety or executable slippage.",
            ],
        })
    callouts.sort(key=lambda item: (item["score"], item["liquidity_usd"]), reverse=True)
    return {
        "callouts": callouts[:25],
        "scanned_assets": len(assets),
        "matched_market_pairs": len(by_mint),
        "rejected": rejected,
        "source": "SignalForce observations + DexScreener",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "manual_review_only",
        "execution_enabled": False,
        "note": "These are watchlist candidates, not buy/sell instructions. No trade is placed. Public individual FOMO trader performance is not currently verified by the connected feed.",
    }
