from datetime import datetime, timezone, timedelta

from app.fomo_signals import build_callouts


def _asset(mint="mint1", age=30):
    seen = datetime.now(timezone.utc) - timedelta(seconds=age)
    return {"asset_id": mint, "token_name": "Example", "latest_collected_at": seen.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _pair(mint="mint1"):
    return {
        "chainId": "solana",
        "pairAddress": "pair1",
        "baseToken": {"address": mint, "name": "Example", "symbol": "EX"},
        "priceUsd": "0.001",
        "liquidity": {"usd": 60000},
        "volume": {"h1": 25000},
        "txns": {"m5": {"buys": 20, "sells": 5}},
        "priceChange": {"m5": 3, "h1": 4},
    }


def test_builds_manual_watchlist_candidate_without_fabricated_levels():
    result = build_callouts([_asset()], [_pair()])
    item = result["callouts"][0]
    assert item["plan_status"] == "market_watch_only"
    assert item["entry"] is None and item["stop"] is None and item["targets"] == []
    assert result["execution_enabled"] is False


def test_rejects_stale_assets():
    result = build_callouts([_asset(age=1200)], [_pair()])
    assert result["callouts"] == []
    assert result["rejected"]["stale"] == 1


def test_rejects_low_liquidity_and_sell_pressure():
    low = _pair()
    low["liquidity"]["usd"] = 1000
    result = build_callouts([_asset()], [low])
    assert result["rejected"]["low_liquidity"] == 1
    pressure = _pair()
    pressure["txns"]["m5"] = {"buys": 3, "sells": 20}
    pressure["priceChange"]["m5"] = -10
    result = build_callouts([_asset()], [pressure])
    assert result["rejected"]["sell_pressure"] == 1



def test_solana_mint_validation_excludes_quote_assets_and_non_solana_addresses():
    from app.fomo_signals import is_solana_mint

    assert is_solana_mint("TokenMint1111111111111111111111111111111111")
    assert not is_solana_mint("So11111111111111111111111111111111111111112")
    assert not is_solana_mint("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkGZwyTDt1v")
    assert not is_solana_mint("0x0000000000000000000000000000000000000000")
    assert not is_solana_mint("mint1")
