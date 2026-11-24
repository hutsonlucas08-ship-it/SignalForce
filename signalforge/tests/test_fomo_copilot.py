from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client():
    settings = Settings(
        db_path=":memory:",
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    )
    return TestClient(create_app(settings), base_url="https://testserver")


def _signed_in(client, email):
    response = client.post("/v1/customer/register", json={
        "email": email, "password": "correct horse battery staple"
    })
    assert response.status_code == 201
    return response.json()["csrf_token"]


def test_fomo_copilot_requires_customer_session():
    client = _client()
    assert client.get("/v1/customer/fomo-copilot").status_code == 401


def test_fomo_snapshots_are_csrf_protected_and_tenant_scoped():
    a, b = _client(), _client()
    csrf_a = _signed_in(a, "fomo-a@example.test")
    _signed_in(b, "fomo-b@example.test")
    payload = {"swaps": [{"id": "swap-1", "displayPriceUsd": "0.01"}]}
    denied = a.post("/v1/customer/fomo-copilot/snapshot", json={
        "snapshot_type": "spot_swaps", "payload": payload
    })
    assert denied.status_code == 403

    saved = a.post("/v1/customer/fomo-copilot/snapshot",
                   headers={"X-CSRF-Token": csrf_a},
                   json={"snapshot_type": "spot_swaps", "payload": payload})
    assert saved.status_code == 200
    assert saved.json()["saved"] is True
    assert a.get("/v1/customer/fomo-copilot").json()["connected"] is True
    assert b.get("/v1/customer/fomo-copilot").json()["connected"] is False


def test_fomo_snapshot_rejects_credentials_and_unknown_snapshot_types():
    client = _client()
    csrf = _signed_in(client, "fomo-safe@example.test")
    headers = {"X-CSRF-Token": csrf}
    secret = client.post("/v1/customer/fomo-copilot/snapshot", headers=headers, json={
        "snapshot_type": "spot_swaps", "payload": {"authorization": "must-not-store"}
    })
    assert secret.status_code == 422
    unknown = client.post("/v1/customer/fomo-copilot/snapshot", headers=headers, json={
        "snapshot_type": "balances", "payload": {}
    })
    assert unknown.status_code == 422
