from app import launch_feed


def test_dexscreener_fallback_joins_solana_profile_to_best_liquidity_pair():
    profiles = [
        {"chainId": "solana", "tokenAddress": "Mint111111111111111111111111111111111111111"},
        {"chainId": "ethereum", "tokenAddress": "NotSolana"},
    ]
    pairs = [
        {
            "chainId": "solana",
            "pairAddress": "PoolLow",
            "baseToken": {"address": profiles[0]["tokenAddress"], "name": "Token", "symbol": "TKN"},
            "liquidity": {"usd": 100},
            "volume": {"h24": 200},
            "pairCreatedAt": 1730000000000,
        },
        {
            "chainId": "solana",
            "pairAddress": "PoolHigh",
            "baseToken": {"address": profiles[0]["tokenAddress"], "name": "Token", "symbol": "TKN"},
            "liquidity": {"usd": 500},
            "volume": {"h24": 800},
            "pairCreatedAt": 1730000000000,
            "priceUsd": "0.25",
            "dexId": "raydium",
        },
    ]
    rows = launch_feed.normalize_dexscreener_latest(profiles, pairs)
    assert len(rows) == 1
    assert rows[0]["asset_id"] == profiles[0]["tokenAddress"]
    assert rows[0]["pool_address"] == "PoolHigh"
    assert rows[0]["liquidity_usd"] == 500
    assert rows[0]["volume_24h_usd"] == 800
    assert rows[0]["symbol"] == "TKN"


def test_dexscreener_fallback_respects_expanded_discovery_limit():
    profiles = [
        {"chainId": "solana", "tokenAddress": f"Mint{i:03d}"}
        for i in range(35)
    ]
    rows = launch_feed.normalize_dexscreener_latest(profiles, [], limit=30)
    assert len(rows) == 30
    assert rows[-1]["asset_id"] == "Mint029"


def test_dexscreener_fallback_handles_missing_pairs_and_invalid_profiles():
    rows = launch_feed.normalize_dexscreener_latest(
        [
            None,
            {"chainId": "solana", "tokenAddress": "Mint222222222222222222222222222222222222222"},
            {"chainId": "solana"},
        ],
        [],
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "Mint2222"
    assert rows[0]["pool_address"] is None
    assert rows[0]["liquidity_usd"] is None
