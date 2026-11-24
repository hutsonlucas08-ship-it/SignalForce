from app import copytop_feed


def test_copytop_normalizes_public_fomo_trader_and_trade_rows():
    trader = copytop_feed._normalize_trader({
        "handle": "@lucid", "platform": "fomo", "realizedPnl": 1250,
        "winRate": 0.64, "closedPositions": 32, "fairPlay": "clean",
    })
    assert trader["handle"] == "lucid"
    assert trader["realized_pnl"] == 1250
    assert trader["win_rate"] == 0.64
    trade = copytop_feed._normalize_trade({
        "side": "buy", "trader": {"handle": "lucid"},
        "coin": {"symbol": "TEST", "address": "MintAddress"},
        "amountUsd": 250, "createdAt": "2026-11-24T20:00:00Z",
    })
    assert trade["handle"] == "lucid"
    assert trade["side"] == "buy"
    assert trade["symbol"] == "TEST"
    assert trade["mint"] == "MintAddress"
    assert trade["amount_usd"] == 250


def test_copytop_hot_coin_platform_scope_is_not_assumed_to_be_fomo():
    fomo = copytop_feed._normalize_hot({"symbol": "ABC", "platform": "fomo", "traderCount": 3})
    mixed = copytop_feed._normalize_hot({"symbol": "XYZ", "platform": "provider aggregate"})
    assert fomo["platform_scope"] == "fomo"
    assert "not confirmed" in mixed["platform_scope"]


def test_copytop_rows_supports_nested_data_envelopes():
    rows = copytop_feed._rows({"data": {"trades": [{"side": "buy"}, None]}}, "trades")
    assert rows == [{"side": "buy"}]


def test_customer_copytop_feed_is_authenticated_and_read_only(monkeypatch):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(
        db_path=":memory:",
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    ))
    client = TestClient(app, base_url="https://testserver")
    assert client.get("/v1/customer/fomo-copilot/provider-feed").status_code == 401
    registered = client.post("/v1/customer/register", json={
        "email": "copytop@example.test", "password": "correct horse battery staple"
    })
    assert registered.status_code == 201

    async def fake_feed():
        return {
            "provider": "copy.top", "read_only": True, "execution_enabled": False,
            "leaderboard": [], "live_trades": [], "hot_coins": [], "errors": {},
        }

    monkeypatch.setattr("app.main.copytop_feed.fetch_feed", fake_feed)
    response = client.get("/v1/customer/fomo-copilot/provider-feed")
    assert response.status_code == 200
    assert response.json()["provider"] == "copy.top"
    assert response.json()["read_only"] is True
    assert response.json()["execution_enabled"] is False



def test_copytop_maps_common_alternate_contract_and_trader_fields():
    hot = copytop_feed._normalize_hot({
        "symbol": "TEST",
        "contract_address": "Mint123",
        "trader_count": 4,
        "top_traders": [{"user_handle": "@alpha"}, {"handle": "beta"}],
    })
    assert hot["mint"] == "Mint123"
    assert hot["trader_count"] == 4
    assert hot["traders"] == ["alpha", "beta"]


def test_copytop_alert_accepts_numeric_trader_count_and_alternate_timestamp():
    row = {
        "kind": "confluence",
        "symbol": "TEST",
        "traders": 5,
        "created_at": "2026-11-24T20:00:00Z",
    }
    count = copytop_feed._first(row, "traderCount", "trader_count", "topTraderCount", "top_trader_count", "tradersCount", "traders_count", "top_traders_count", "numTraders", "num_traders", default=(row.get("traders") if isinstance(row.get("traders"), int) else None))
    assert count == 5
