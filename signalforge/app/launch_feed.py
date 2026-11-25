"""Normalize DexScreener's latest Solana token profiles into launch-feed rows."""
from datetime import datetime, timezone
from typing import Any


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def normalize_dexscreener_latest(
    profiles: Any, pairs: Any, limit: int = 10
) -> list[dict]:
    """Join latest Solana token profiles to their best available market pair.

    DexScreener's profile feed is a fallback discovery source, not a guarantee
    that each token is a brand-new pool.
    """
    if not isinstance(profiles, list):
        return []
    pair_rows = pairs if isinstance(pairs, list) else []
    by_token: dict[str, list[dict]] = {}
    for pair in pair_rows:
        if not isinstance(pair, dict):
            continue
        for token_key in ("baseToken", "quoteToken"):
            token = pair.get(token_key) or {}
            address = token.get("address") if isinstance(token, dict) else None
            if isinstance(address, str) and address:
                by_token.setdefault(address, []).append(pair)

    result = []
    seen = set()
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        if str(profile.get("chainId") or "").lower() != "solana":
            continue
        address = profile.get("tokenAddress")
        if not isinstance(address, str) or not address or address in seen:
            continue
        seen.add(address)
        matching = by_token.get(address, [])
        pair = max(
            matching,
            key=lambda row: _number((row.get("liquidity") or {}).get("usd")),
            default={},
        )
        token = {}
        for token_key in ("baseToken", "quoteToken"):
            candidate = pair.get(token_key) or {}
            if isinstance(candidate, dict) and candidate.get("address") == address:
                token = candidate
                break
        created_at = pair.get("pairCreatedAt")
        if isinstance(created_at, (int, float)) and created_at > 0:
            created_at = datetime.fromtimestamp(
                created_at / 1000, tz=timezone.utc
            ).isoformat()
        else:
            created_at = None
        image = profile.get("icon")
        if not isinstance(image, str) or not image.startswith("https://"):
            image = None
        result.append({
            "asset_id": address,
            "name": token.get("name") or profile.get("name") or address[:8],
            "symbol": token.get("symbol") or profile.get("symbol") or "",
            "image_url": image,
            "pool_address": pair.get("pairAddress"),
            "pool_created_at": created_at,
            "price_usd": pair.get("priceUsd"),
            "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
            "volume_24h_usd": (pair.get("volume") or {}).get("h24"),
            "dex": pair.get("dexId"),
            "market_url": pair.get("url"),
            "source": "DexScreener latest token profiles",
        })
        if len(result) >= max(1, limit):
            break
    return result
