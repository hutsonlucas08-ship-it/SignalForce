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
        "snapshot_type": "spot_swaps", "payload": {
            "swaps": [], "authorization": "must-not-store"
        }
    })
    assert secret.status_code == 422
    unknown = client.post("/v1/customer/fomo-copilot/snapshot", headers=headers, json={
        "snapshot_type": "balances", "payload": {}
    })
    assert unknown.status_code == 422



def test_local_bridge_pairing_is_one_time_and_bridge_key_is_tenant_scoped():
    owner, other = _client(), _client()
    csrf = _signed_in(owner, "bridge-owner@example.test")
    _signed_in(other, "bridge-other@example.test")
    generated = owner.post("/v1/customer/fomo-copilot/pairing",
                           headers={"X-CSRF-Token": csrf}, json={})
    assert generated.status_code == 200
    code = generated.json()["pairing_code"]

    paired = owner.post("/v1/customer/fomo-copilot/pair", json={"pairing_code": code})
    assert paired.status_code == 200
    bridge_key = paired.json()["bridge_key"]
    assert bridge_key
    assert len(bridge_key) > 20
    assert bridge_key not in owner.get("/v1/customer/fomo-copilot").text

    reused = owner.post("/v1/customer/fomo-copilot/pair", json={"pairing_code": code})
    assert reused.status_code == 401

    payload = {"swaps": [{"id": "bridge-swap", "inTokenAddress": "mint-a"}]}
    saved = owner.post("/v1/customer/fomo-copilot/bridge-snapshot",
                       headers={"X-Fomo-Bridge-Key": bridge_key},
                       json={"snapshot_type": "spot_swaps", "payload": payload})
    assert saved.status_code == 200
    assert owner.get("/v1/customer/fomo-copilot").json()["connected"] is True
    assert other.get("/v1/customer/fomo-copilot").json()["connected"] is False

    denied = other.post("/v1/customer/fomo-copilot/bridge-snapshot",
                        headers={"X-Fomo-Bridge-Key": "not-the-owner-key"},
                        json={"snapshot_type": "spot_swaps", "payload": payload})
    assert denied.status_code == 401



def test_local_bridge_exposes_no_dom_input_or_wallet_signing_surface():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "fomo_bridge" / "bridge.js").read_text().lower()
    forbidden = [
        "page.click", "page.fill", "page.type", "input.dispatch",
        "dispatchEvent", "keyboard", "mouse", "locator(", "solders",
        "keypair", "solana-py", "web3", "sendtransaction",
    ]
    for pattern in forbidden:
        assert pattern not in source
    assert 'this.send(state, "network.enable")' in source
    assert 'this.send(pagestate, "page.reload"' in source
    assert "service_worker" in source
    assert "cookieheader" in source
    assert "hassessioncredential" in source
    assert "allowed_cdp_methods" in source
    assert "if (!allowed_cdp_methods.has(method))" in source
    assert '"network.getresponsebody"' in source
    assert '"network.loadingfinished"' in source



def test_manual_callout_endpoint_requires_customer_and_valid_limit():
    client = _client()
    assert client.get("/v1/customer/fomo-copilot/callouts").status_code == 401
    _signed_in(client, "callouts@example.test")
    assert client.get("/v1/customer/fomo-copilot/callouts?limit=0").status_code == 422
    assert client.get("/v1/customer/fomo-copilot/callouts?limit=51").status_code == 422



def test_trader_leaderboard_snapshot_accepts_sanitized_rows_only():
    from app.fomo_copilot import FomoSnapshotError, validate_snapshot

    encoded = validate_snapshot("trader_leaderboard", {
        "traders": [{
            "rank": 1,
            "user": {"userHandle": "example"},
            "stats": {"winRate": 0.62, "totalTrades": 25},
        }]
    })
    assert '"userHandle":"example"' in encoded
    try:
        validate_snapshot("trader_leaderboard", {
            "traders": [{"user": {"accessToken": "must-not-store"}}]
        })
    except FomoSnapshotError:
        pass
    else:
        raise AssertionError("leaderboard snapshots must reject credential fields")



def test_trader_leaderboard_snapshot_persists_with_new_schema_migration():
    client = _client()
    csrf = _signed_in(client, "leaderboard-persist@example.test")
    response = client.post(
        "/v1/customer/fomo-copilot/snapshot",
        headers={"X-CSRF-Token": csrf},
        json={
            "snapshot_type": "trader_leaderboard_7d",
            "payload": {
                "window": "7d",
                "traders": [{"rank": 1, "user": {"id": "trader-1"}}],
            },
        },
    )
    assert response.status_code == 200
    snapshots = client.get("/v1/customer/fomo-copilot").json()["snapshots"]
    assert any(item["snapshot_type"] == "trader_leaderboard_7d" for item in snapshots)



def test_bridge_targets_requires_pairing_and_returns_only_strictly_qualified_traders():
    client = _client()
    assert client.get("/v1/customer/fomo-copilot/bridge-targets").status_code == 401
    csrf = _signed_in(client, "qualified-targets@example.test")
    pairing = client.post(
        "/v1/customer/fomo-copilot/pairing",
        headers={"X-CSRF-Token": csrf},
        json={},
    )
    assert pairing.status_code == 200
    paired = client.post(
        "/v1/customer/fomo-copilot/pair",
        json={"pairing_code": pairing.json()["pairing_code"]},
    )
    assert paired.status_code == 200
    bridge_key = paired.json()["bridge_key"]
    row = {
        "user": {"id": "qualified-trader-1", "userHandle": "qualified"},
        "stats": {
            "pnlUsd": 1200,
            "winRate": 0.61,
            "totalTrades": 25,
            "medianHoldMinutes": 18,
            "maxSingleTokenPnlShare": 0.25,
        },
    }
    for window in ("24h", "7d"):
        saved = client.post(
            "/v1/customer/fomo-copilot/snapshot",
            headers={"X-CSRF-Token": csrf},
            json={
                "snapshot_type": "trader_leaderboard_" + window,
                "payload": {"window": window, "traders": [row]},
            },
        )
        assert saved.status_code == 200
    targets = client.get(
        "/v1/customer/fomo-copilot/bridge-targets",
        headers={"X-Fomo-Bridge-Key": bridge_key},
    )
    assert targets.status_code == 200
    assert targets.json()["count"] == 1
    assert targets.json()["qualified_traders"][0]["user_id"] == "qualified-trader-1"
    activity = client.post(
        "/v1/customer/fomo-copilot/bridge-snapshot",
        headers={"X-Fomo-Bridge-Key": bridge_key},
        json={
            "snapshot_type": "trader_spot_swaps",
            "payload": {
                "traders": [{
                    "user_id": "qualified-trader-1",
                    "handle": "qualified",
                    "swaps": [{
                        "createdAt": "2026-11-24T20:00:00Z",
                        "display": {"side": "buy"},
                        "outTokenAddress": "mint-1",
                    }],
                }],
            },
        },
    )
    assert activity.status_code == 200
    imported = client.get("/v1/customer/fomo-copilot").json()["snapshots"]
    assert any(item["snapshot_type"] == "trader_spot_swaps" for item in imported)


def test_trader_spot_swap_snapshot_accepts_only_sanitized_trader_rows():
    from app.fomo_copilot import FomoSnapshotError, validate_snapshot

    encoded = validate_snapshot("trader_spot_swaps", {
        "traders": [{
            "user_id": "trader-1",
            "handle": "example",
            "swaps": [{
                "id": "fomo-swap-id",
                "createdAt": "2026-11-24T20:00:00Z",
                "display": {"side": "buy"},
                "outTokenAddress": "mint-address",
                "signature": "provided-chain-signature",
            }],
        }]
    })
    assert '"outTokenAddress":"mint-address"' in encoded
    try:
        validate_snapshot("trader_spot_swaps", {
            "traders": [{"user_id": "trader-1", "swaps": [{"authorization": "secret"}]}]
        })
    except FomoSnapshotError:
        pass
    else:
        raise AssertionError("trader swap snapshots must reject credentials")
