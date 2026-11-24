"""Read-only Copy.top public FOMO intelligence adapter; never places orders."""
from datetime import datetime, timezone
from typing import Any
import httpx

BASE_URL = "https://copy.top/v1"
_trade_cursor = None
_alert_cursor = None


def _rows(payload: Any, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    data = payload.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


def _first(row: dict, *keys: str, default=None):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return default


def _handle(row: dict) -> str | None:
    value = _first(row, "handle", "username", "userHandle", "trader_handle")
    if value is None:
        for path in ("trader", "user", "profile"):
            nested = row.get(path)
            if isinstance(nested, dict):
                value = _first(nested, "handle", "username", "userHandle")
                if value is not None:
                    break
    return value.lstrip("@")[:80] if isinstance(value, str) else None


def _normalize_trader(row: dict) -> dict:
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    return {
        "handle": _handle(row) or "Unknown trader",
        "name": _first(row, "name", "displayName"),
        "platform": _first(row, "platform", "source", default="fomo"),
        "realized_pnl": _first(row, "realizedPnl", "realized_pnl", "realized", default=_first(metrics, "realizedPnl", "realized_pnl")),
        "total_pnl": _first(row, "pnl", "totalPnl", "total_pnl", default=_first(metrics, "pnl", "totalPnl")),
        "roi": _first(row, "roi", default=metrics.get("roi")),
        "win_rate": _first(row, "winRate", "win_rate", default=_first(metrics, "winRate", "win_rate")),
        "closed_trades": _first(row, "closedPositions", "closed_positions", "trades", "tradeCount"),
        "median_hold": _first(row, "medianHold", "median_hold", "medianHoldTime"),
        "fair_play": _first(row, "fairPlay", "fair_play", "verdict"),
        "rank": _first(row, "rank", "position"),
    }


def _normalize_trade(row: dict) -> dict:
    trader = row.get("trader") if isinstance(row.get("trader"), dict) else {}
    token = row.get("coin") if isinstance(row.get("coin"), dict) else row.get("token")
    token = token if isinstance(token, dict) else {}
    return {
        "id": _first(row, "id", "tradeId", "signature"),
        "handle": _handle(row) or _handle(trader) or "FOMO trader",
        "platform": _first(row, "platform", "source", default="fomo watchlist"),
        "side": str(_first(row, "side", "action", default="unknown")).lower(),
        "symbol": _first(row, "symbol", "ticker", default=_first(token, "symbol", "ticker", default="?")),
        "token_name": _first(row, "tokenName", "name", default=_first(token, "name", "symbol")),
        "mint": _first(row, "address", "tokenAddress", "mint", "coinAddress", default=_first(token, "address", "mint")),
        "amount_usd": _first(row, "amountUsd", "amount_usd", "usd", "valueUsd", "value_usd"),
        "price_usd": _first(row, "priceUsd", "price_usd", "price"),
        "timestamp": _first(row, "timestamp", "createdAt", "created_at", "time"),
        "tx_signature": _first(row, "signature", "txSignature", "tx_signature"),
    }


def _normalize_hot(row: dict) -> dict:
    traders = _first(row, "traders", "buyers", "firstBuyers", "first_buyers", default=[])
    traders = traders if isinstance(traders, list) else []
    handles = []
    for trader in traders[:20]:
        handle = trader if isinstance(trader, str) else (_handle(trader) if isinstance(trader, dict) else None)
        if handle and handle not in handles:
            handles.append(handle)
    platform = _first(row, "platform", "source", default="provider aggregate")
    return {
        "symbol": _first(row, "symbol", "ticker", default="?"),
        "name": _first(row, "name", "tokenName"),
        "mint": _first(row, "address", "mint", "tokenAddress", "coinAddress"),
        "chain": _first(row, "chain", "chainId"),
        "platform": platform,
        "platform_scope": "fomo" if str(platform).lower() == "fomo" else "provider aggregate; platform scope not confirmed",
        "trader_count": _first(row, "traderCount", "tradersCount", "uniqueTraders", "unique_traders", "buyersCount", "topTraderCount", "top_traders_count", default=len(handles)),
        "traders": handles,
        "bought_usd": _first(row, "boughtUsd", "buyUsd", "buy_usd", "totalBoughtUsd", "total_bought_usd", "dollarsBought"),
        "sold_usd": _first(row, "soldUsd", "sellUsd", "sell_usd", "totalSoldUsd", "total_sold_usd", "dollarsSold"),
        "market_cap_usd": _first(row, "marketCapUsd", "market_cap_usd", "mcap"),
        "minutes_since_first_buy": _first(row, "minutesSinceFirstBuy", "minutes_since_first_buy", "firstBuyMinutesAgo"),
        "attention_score": _first(row, "score", "index", "attention"),
    }


async def _get(client: httpx.AsyncClient, path: str, params: dict):
    try:
        response = await client.get(BASE_URL + path, params=params, headers={"Accept": "application/json"})
        response.raise_for_status()
        payload = response.json()
        return (payload if isinstance(payload, (dict, list)) else {}), None
    except httpx.HTTPStatusError as exc:
        return {}, "Provider rate limit reached." if exc.response.status_code == 429 else f"Provider returned HTTP {exc.response.status_code}."
    except (httpx.HTTPError, ValueError):
        return {}, "Provider temporarily unavailable."


async def fetch_feed() -> dict:
    """Fetch FOMO leaderboard, watchlist tape, confluence alerts and hot-coin context."""
    global _trade_cursor, _alert_cursor
    async with httpx.AsyncClient(timeout=8.0) as client:
        lb_payload, lb_error = await _get(client, "/traders", {"platform": "fomo", "window": "7d", "sort": "realized", "limit": 20})
        lb_rows = _rows(lb_payload, "traders", "items", "results", "leaderboard")
        leaderboard = [_normalize_trader(row) for row in lb_rows]
        handles = list(dict.fromkeys(x["handle"] for x in leaderboard if x["handle"] != "Unknown trader"))[:20]
        if handles:
            trade_params = {"traders": ",".join(handles), "limit": 30}
            if _trade_cursor:
                trade_params["since"] = _trade_cursor
            trade_payload, trade_error = await _get(client, "/trades", trade_params)
        else:
            trade_payload, trade_error = {}, "No FOMO leaderboard handles available."
        hot_payload, hot_error = await _get(client, "/coins/hot", {"window": "1h", "limit": 15})
    trade_cursor = _first(trade_payload, "cursor", "nextCursor", "next_cursor") if isinstance(trade_payload, dict) else None
    if isinstance(trade_cursor, str) and trade_cursor:
        _trade_cursor = trade_cursor
    trades = [_normalize_trade(x) for x in _rows(trade_payload, "trades", "items", "results")]
    # The live-trade request is scoped to the FOMO leaderboard handles. If the
    # provider labels platform, exclude rows explicitly marked as another platform.
    trades = [x for x in trades if str(x["platform"]).lower() in {"fomo", "fomo watchlist", "unknown", "none"}]
    # Confluence and large-trade events are the strongest direct provider callouts.
    async with httpx.AsyncClient(timeout=8.0) as client:
        alert_payload, alert_error = await _get(
            client, "/alerts",
            {"min_top_traders": 3, "min_trade_usd": 3000, "kinds": "confluence,big_trade", "limit": 20, **({"since": _alert_cursor} if _alert_cursor else {})},
        )
    alert_cursor = _first(alert_payload, "cursor", "nextCursor", "next_cursor") if isinstance(alert_payload, dict) else None
    if isinstance(alert_cursor, str) and alert_cursor:
        _alert_cursor = alert_cursor
    alerts = []
    for row in _rows(alert_payload, "alerts", "events", "items", "results"):
        trader_rows = _first(row, "traders", "topTraders", "top_traders", default=[])
        trader_rows = trader_rows if isinstance(trader_rows, list) else []
        handles_in_alert = []
        for trader in trader_rows[:20]:
            handle = trader if isinstance(trader, str) else (_handle(trader) if isinstance(trader, dict) else None)
            if handle and handle not in handles_in_alert:
                handles_in_alert.append(handle)
        coin = row.get("coin") if isinstance(row.get("coin"), dict) else {}
        alerts.append({
            "kind": _first(row, "kind", "type", "event", "event_type"),
            "symbol": _first(row, "symbol", "ticker", default=_first(coin, "symbol", "ticker", default="?")),
            "name": _first(row, "name", "tokenName", default=coin.get("name")),
            "mint": _first(row, "address", "mint", "tokenAddress", default=_first(coin, "address", "mint")),
            "chain": _first(row, "chain", "chainId", default=coin.get("chain")),
            "trader_count": _first(row, "traderCount", "topTraderCount", "traders_count", "top_traders_count", default=len(handles_in_alert)),
            "traders": handles_in_alert,
            "amount_usd": _first(row, "amountUsd", "amount_usd", "usd", "valueUsd"),
            "timestamp": _first(row, "timestamp", "createdAt", "created_at", "time"),
            "summary": _first(row, "summary", "message", "description"),
        })
    return {
        "provider": "copy.top",
        "provider_url": "https://copy.top/fomo-api",
        "read_only": True,
        "execution_enabled": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "leaderboard": leaderboard,
        "live_trades": trades,
        "hot_coins": [_normalize_hot(x) for x in _rows(hot_payload, "coins", "items", "results", "hot")],
        "alerts": alerts,
        "watchlist_handles": handles,
        "errors": {"leaderboard": lb_error, "live_trades": trade_error, "hot_coins": hot_error, "alerts": alert_error},
        "note": "Copy.top is independent of FOMO. Live trades are requested for handles on its FOMO leaderboard. Hot-coin data may aggregate platforms unless explicitly labeled. Provider data is not independent on-chain verification.",
    }
