from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def client():
    settings = Settings(
        db_path=":memory:",
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    )
    return TestClient(create_app(settings))


def test_risk_screen_requires_read_key():
    response = client().post("/v1/research/risk-screen", json={})
    assert response.status_code == 401


def test_risk_screen_returns_insufficient_data_for_missing_fields():
    response = client().post(
        "/v1/research/risk-screen",
        headers={"X-API-Key": "r" * 32},
        json={"liquidity_usd": 100_000},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "INSUFFICIENT_DATA"


def test_risk_screen_flags_risky_snapshot_without_trade_instruction():
    response = client().post(
        "/v1/research/risk-screen",
        headers={"X-API-Key": "r" * 32},
        json={
            "liquidity_usd": 1_000,
            "top_10_holder_pct": 80,
            "mint_authority_active": True,
            "freeze_authority_active": False,
            "data_age_seconds": 5,
        },
    )
    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "HIGH_RISK"
    assert "buy" not in body and "sell" not in body
