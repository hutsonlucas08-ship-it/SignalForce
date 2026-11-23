import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

from app.config import Settings
from app.main import create_app\nfrom app import customer_email


def _client():
    settings = Settings(
        db_path=":memory:",
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    )
    return TestClient(create_app(settings), base_url="https://testserver")


def _register_and_activate(client, email, monkeypatch):
    tokens = {}
    async def fake_send(address, token):
        tokens[address] = token
    monkeypatch.setattr(customer_email, "send_verification_email", fake_send)
    response = client.post("/v1/customer/register", json={
        "email": email, "password": "correct horse battery staple"
    })
    assert response.status_code == 201
    assert response.json()["status"] == "pending_verification"
    verified = client.get("/v1/customer/verify-email", params={"token": tokens[email]})
    assert verified.status_code == 200

def _login(client, email):
    response = client.post("/v1/customer/login", json={
        "email": email, "password": "correct horse battery staple"
    })
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_customer_can_register_verify_email_and_sign_in_without_manual_activation(monkeypatch):
    client = _client()
    _register_and_activate(client, "one@example.test", monkeypatch)
    csrf = _login(client, "one@example.test")
    assert csrf
    assert client.get("/v1/customer/me").json()["email"] == "one@example.test"


def test_customer_routes_enforce_csrf_and_tenant_isolation(monkeypatch):
    client_a, client_b = _client(), _client()
    _register_and_activate(client_a, "a@example.test", monkeypatch)
    _register_and_activate(client_b, "b@example.test", monkeypatch)
    csrf_a = _login(client_a, "a@example.test")
    csrf_b = _login(client_b, "b@example.test")
    wallet = "11111111111111111111111111111111"

    denied = client_a.post("/v1/customer/wallets", json={"wallet_address": wallet})
    assert denied.status_code == 403
    added = client_a.post("/v1/customer/wallets", headers={"X-CSRF-Token": csrf_a},
                          json={"wallet_address": wallet, "label": "Private"})
    assert added.status_code == 200
    assert client_a.get("/v1/customer/wallets").json()["wallets"][0]["label"] == "Private"
    assert client_b.get("/v1/customer/wallets").json()["wallets"] == []
    assert client_b.delete("/v1/customer/wallets/" + wallet,
                           headers={"X-CSRF-Token": csrf_b}).status_code == 404


def test_customer_paper_accounts_are_created_per_signed_in_customer(monkeypatch):
    client = _client()
    _register_and_activate(client, "paper@example.test", monkeypatch)
    csrf = _login(client, "paper@example.test")
    response = client.post("/v1/customer/paper-accounts",
                           headers={"X-CSRF-Token": csrf},
                           json={"starting_capital_usd": 500, "name": "My simulation"})
    assert response.status_code == 201
    assert response.json()["paper_account"]["cash_balance_usd"] == 500
    assert client.get("/v1/customer/paper-accounts").json()["paper_accounts"][0]["name"] == "My simulation"


def test_customer_integration_credentials_are_encrypted_and_never_returned(monkeypatch):
    monkeypatch.setenv("SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    client = _client()
    _register_and_activate(client, "integration@example.test", monkeypatch)
    csrf = _login(client, "integration@example.test")
    secret = "helius-secret-not-for-browser"
    response = client.post(
        "/v1/customer/integrations",
        headers={"X-CSRF-Token": csrf},
        json={"provider": "helius", "credentials": {"api_key": secret}},
    )
    assert response.status_code == 200
    assert secret not in response.text
    assert "encrypted_payload" not in response.text
    status = client.get("/v1/customer/integrations").json()["integrations"]
    assert any(item["provider"] == "helius" and item["configured"] for item in status)


def test_customer_session_logout_revokes_cookie_session(monkeypatch):
    client = _client()
    _register_and_activate(client, "logout@example.test", monkeypatch)
    csrf = _login(client, "logout@example.test")
    assert client.post("/v1/customer/logout", headers={"X-CSRF-Token": csrf}).status_code == 200
    assert client.get("/v1/customer/me").status_code == 401


def test_customer_paper_trade_api_is_csrf_protected_and_account_scoped(monkeypatch):
    client_a, client_b = _client(), _client()
    _register_and_activate(client_a, "trade-a@example.test", monkeypatch)
    _register_and_activate(client_b, "trade-b@example.test", monkeypatch)
    csrf_a, csrf_b = _login(client_a, "trade-a@example.test"), _login(client_b, "trade-b@example.test")
    paper = client_a.post("/v1/customer/paper-accounts", headers={"X-CSRF-Token": csrf_a},
                          json={"starting_capital_usd": 100}).json()["paper_account"]
    denied = client_a.post(f"/v1/customer/paper-accounts/{paper['id']}/trades",
                           json={"asset_id": "mint", "side": "buy", "amount_usd": 20, "reference_price": 2})
    assert denied.status_code == 403
    buy = client_a.post(f"/v1/customer/paper-accounts/{paper['id']}/trades",
                        headers={"X-CSRF-Token": csrf_a},
                        json={"asset_id": "mint", "side": "buy", "amount_usd": 20, "reference_price": 2})
    assert buy.status_code == 201
    assert buy.json()["trade"]["quantity"] == 10
    assert client_b.get(f"/v1/customer/paper-accounts/{paper['id']}/positions").status_code == 404
    assert client_a.get(f"/v1/customer/paper-accounts/{paper['id']}/positions").json()["positions"][0]["quantity"] == 10


def test_customer_can_refresh_csrf_after_session_cookie_survives_reload(monkeypatch):
    client = _client()
    _register_and_activate(client, "csrf@example.test", monkeypatch)
    _login(client, "csrf@example.test")
    fresh = client.get("/v1/customer/csrf")
    assert fresh.status_code == 200
    wallet = client.post("/v1/customer/wallets", headers={"X-CSRF-Token": fresh.json()["csrf_token"]},
                         json={"wallet_address": "11111111111111111111111111111111"})
    assert wallet.status_code == 200
