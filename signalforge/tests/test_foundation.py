import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import store
from app.config import ConfigError, Settings, load_settings
from app.db import MigrationError, connect, run_migrations
from app.main import create_app

READ = "r" * 32
ADMIN = "a" * 32


def make_client(**over):
    s = Settings(db_path=":memory:", read_api_keys=frozenset({READ}),
                 admin_api_keys=frozenset({ADMIN}), **over)
    app = create_app(s)
    return TestClient(app, raise_server_exceptions=False), app


def h(key):
    return {"X-API-Key": key}


def test_auth_status_reports_configuration_without_exposing_keys():
    c, _ = make_client()
    response = c.get("/v1/auth/status")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "read_key_configured": True, "read_key_count": 1,
        "read_key_fingerprints": [__import__("hashlib").sha256(READ.encode()).hexdigest()[:8]],
        "admin_key_configured": True, "admin_key_count": 1,
    }
    assert READ not in response.text and ADMIN not in response.text


def test_auth_status_reports_missing_keys():
    settings = Settings(db_path=":memory:", read_api_keys=frozenset(), admin_api_keys=frozenset())
    response = TestClient(create_app(settings)).get("/v1/auth/status")
    assert response.json()["read_key_configured"] is False
    assert response.json()["read_key_count"] == 0


def test_liveness_is_public_and_minimal():
    c, _ = make_client()
    r = c.get("/health")
    assert r.status_code == 200 and set(r.json()) == {"status", "time"}


def test_protected_endpoints_reject_missing_and_bad_keys():
    c, _ = make_client()
    assert c.get("/v1/system/health").status_code == 401
    assert c.get("/v1/system/health", headers=h("x" * 32)).status_code == 401


def test_read_key_can_read_but_not_halt_or_resume():
    c, _ = make_client()
    assert c.get("/v1/system/health", headers=h(READ)).status_code == 200
    assert c.post("/v1/control/resume", headers=h(READ), json={"reason": "try it"}).status_code == 403
    assert c.post("/v1/control/halt", headers=h(READ), json={"reason": "try it"}).status_code == 403


def test_bearer_header_works():
    c, _ = make_client()
    assert c.get("/v1/system/sources", headers={"Authorization": f"Bearer {READ}"}).status_code == 200


def test_trading_starts_halted_and_admin_can_toggle_with_audit():
    c, _ = make_client()
    assert c.get("/v1/control/halt", headers=h(READ)).json()["trading_halted"] == 1
    r = c.post("/v1/control/resume", headers=h(ADMIN), json={"reason": "testing resume"})
    assert r.status_code == 200 and r.json()["trading_halted"] == 0
    r = c.post("/v1/control/halt", headers=h(ADMIN), json={"reason": "testing halt"})
    assert r.json()["trading_halted"] == 1
    msgs = [e["message"] for e in c.get("/v1/system/events", headers=h(READ)).json()["events"]]
    assert any("RESUMED" in m for m in msgs) and any("HALTED" in m for m in msgs)
    actors = [e["actor"] for e in c.get("/v1/system/events", headers=h(READ)).json()["events"] if e["actor"]]
    assert actors and all(ADMIN not in a for a in actors)  # key never logged, only a fingerprint


def test_halt_requires_a_reason():
    c, _ = make_client()
    assert c.post("/v1/control/halt", headers=h(ADMIN), json={"reason": ""}).status_code == 422


def test_rate_limit():
    c, _ = make_client(rate_limit_per_minute=3)
    codes = [c.get("/v1/system/sources", headers=h(READ)).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_health_reports_no_sources_honestly():
    c, _ = make_client()
    body = c.get("/v1/system/health", headers=h(READ)).json()
    assert body["status"] == "ok" and body["source_count"] == 0 and body["note"]
    assert body["last_observation_collected_at"] is None


def test_source_status_unknown_ok_stale_failing():
    c, app = make_client()
    conn = connect(":memory:")
    run_migrations(conn)
    store.register_source(conn, "dex", "market")
    now = datetime.now(timezone.utc)
    st = lambda: store.list_sources(conn, 300, now)[0]["status"]
    assert st() == "unknown"
    store.record_source_success(conn, "dex")
    assert st() == "ok"
    assert store.list_sources(conn, 300, now + timedelta(seconds=301))[0]["status"] == "stale"
    for _ in range(3):
        store.record_source_failure(conn, "dex", "timeout")
    assert st() == "failing"
    store.record_source_success(conn, "dex")
    assert st() == "ok"


def test_migrations_idempotent_and_detect_edits(tmp_path):
    conn = connect(":memory:")
    first = run_migrations(conn)
    assert first == [
        "001_init.sql",
        "002_paper_simulator.sql",
        "003_paper_bot.sql",
        "004_timed_paper_sessions.sql",
        "005_tracked_wallets.sql",
        "006_position_tracking.sql",
        "006_smart_money.sql",
        "007_wallet_quarantine.sql",
    ]
    assert run_migrations(conn) == []
    state = conn.execute("SELECT enabled FROM paper_bot_state WHERE id=1").fetchone()
    assert state["enabled"] == 0
    d = tmp_path / "m"; d.mkdir()
    (d / "001_a.sql").write_text("CREATE TABLE t (x INTEGER);")
    c2 = connect(":memory:"); run_migrations(c2, d)
    (d / "001_a.sql").write_text("CREATE TABLE t (x INTEGER, y TEXT);")
    with pytest.raises(MigrationError):
        run_migrations(c2, d)


def test_failed_migration_rolls_back(tmp_path):
    d = tmp_path / "m"; d.mkdir()
    (d / "001_bad.sql").write_text("CREATE TABLE ok (x INTEGER); CREATE TABLE ok (y INTEGER);")
    conn = connect(":memory:")
    with pytest.raises(sqlite3.Error):
        run_migrations(conn, d)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok" not in tables


def test_shadow_tables_cannot_be_labeled_live():
    conn = connect(":memory:"); run_migrations(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO shadow_sessions (started_at, starting_balance, mode) VALUES ('t', 100, 'live')")


def test_config_ignores_unedited_template_placeholders(monkeypatch):
    monkeypatch.setenv("SIGNALFORGE_READ_KEYS", "REPLACE_WITH_YOUR_OWN_READ_KEY")
    monkeypatch.setenv("SIGNALFORGE_ADMIN_KEYS", "REPLACE_WITH_YOUR_OWN_ADMIN_KEY")
    settings = load_settings()
    assert settings.read_api_keys == frozenset()
    assert settings.admin_api_keys == frozenset()


def test_config_rejects_short_and_shared_keys(monkeypatch):
    monkeypatch.setenv("SIGNALFORGE_READ_KEYS", "short")
    with pytest.raises(ConfigError):
        load_settings()
    monkeypatch.setenv("SIGNALFORGE_READ_KEYS", READ)
    monkeypatch.setenv("SIGNALFORGE_ADMIN_KEYS", READ)
    with pytest.raises(ConfigError):
        load_settings()


def test_no_keys_configured_fails_closed():
    s = Settings(db_path=":memory:", read_api_keys=frozenset(), admin_api_keys=frozenset())
    c = TestClient(create_app(s))
    assert c.get("/v1/system/health", headers=h("anything" * 4)).status_code == 401


def test_unhandled_errors_do_not_leak_details(monkeypatch):
    c, _ = make_client()
    monkeypatch.setattr(store, "list_sources", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("secret detail")))
    r = c.get("/v1/system/sources", headers=h(READ))
    assert r.status_code == 500 and "secret" not in r.text


def test_helius_status_reports_missing_secret_without_network(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    c, _ = make_client()
    response = c.get("/v1/integrations/helius/status", headers=h(READ))
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["healthy"] is False
    assert body["rpc"]["status"] == "not_configured"
    assert body["enhanced_transactions"]["status"] == "not_configured"


def test_helius_status_checks_both_services_without_returning_secret(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "test-helius-secret-that-is-never-returned")

    class FakeResponse:
        status_code = 200
        def __init__(self, payload):
            self.payload = payload
        def raise_for_status(self):
            return None
        def json(self):
            return self.payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def post(self, *args, **kwargs):
            return FakeResponse({"jsonrpc": "2.0", "result": "ok", "id": 1})
        async def get(self, *args, **kwargs):
            return FakeResponse([])

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    c, _ = make_client()
    response = c.get("/v1/integrations/helius/status", headers=h(READ))
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["healthy"] is True
    assert body["rpc"] == {"status": "connected", "http_status": 200}
    assert body["enhanced_transactions"] == {"status": "connected", "http_status": 200}
    assert "test-helius-secret" not in response.text


def test_geckoterminal_new_launches_returns_token_artwork(monkeypatch):
    from app import main
    main._launch_cache.clear()

    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return {
                "data": [{
                    "id": "solana_pool123",
                    "attributes": {
                        "pool_created_at": "2026-11-22T20:00:00Z",
                        "base_token_price_usd": "0.0001",
                        "reserve_in_usd": "25000",
                        "volume_usd": {"h24": "12000"},
                    },
                    "relationships": {
                        "base_token": {"data": {"id": "solana_mint123"}},
                        "dex": {"data": {"id": "raydium"}},
                    },
                }],
                "included": [{
                    "id": "solana_mint123",
                    "type": "token",
                    "attributes": {
                        "address": "MintAddress123",
                        "name": "Example Coin",
                        "symbol": "EX",
                        "image_url": "https://images.example/token.png",
                    },
                }],
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, url, **kwargs):
            assert "geckoterminal.com" in url
            assert kwargs["params"]["include"] == "base_token,quote_token,dex"
            return FakeResponse()

    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    c, _ = make_client()
    response = c.get("/v1/research/new-launches", headers=h(READ))
    assert response.status_code == 200
    body = response.json()
    assert body["source"].startswith("GeckoTerminal")
    assert body["launches"][0]["asset_id"] == "MintAddress123"
    assert body["launches"][0]["image_url"] == "https://images.example/token.png"
    assert body["launches"][0]["liquidity_usd"] == "25000"


def test_geckoterminal_new_launches_requires_read_auth():
    c, _ = make_client()
    assert c.get("/v1/research/new-launches").status_code == 401


def test_trending_now_uses_local_tokens_and_dexscreener_only(monkeypatch):
    from app import main, paper_bot
    c, _ = make_client()
    # Empty local discovery is an honest empty state and does not call Pump.fun.
    monkeypatch.setattr(
        paper_bot, "_pumpfun_discoveries",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Pump.fun must not be called")),
    )
    response = c.get("/v1/research/trending-now", headers=h(READ))
    assert response.status_code == 200
    assert response.json()["candidates"] == []
    assert response.json()["source"] == "DEX Screener market data"


def test_dashboard_is_served_at_root():
    c, _ = make_client()
    response = c.get("/")
    assert response.status_code == 200
    assert "SignalForge" in response.text
    assert "Token Scanner" in response.text


def test_paper_accounts_start_with_separate_persistent_balances():
    c, _ = make_client()
    response = c.get("/v1/paper/overview", headers=h(READ))
    assert response.status_code == 200
    accounts = response.json()["accounts"]
    assert [a["strategy"] for a in accounts] == ["automatic", "approval"]
    assert all(a["starting_balance"] == 10000 for a in accounts)
    assert all(a["cash_balance"] == 10000 for a in accounts)
    assert all(a["mode"] == "paper" for a in accounts)


def test_paper_order_persists_and_enforces_position_limit():
    c, _ = make_client()
    too_large = {"strategy":"approval","asset_id":"MintABC","token_name":"Demo","side":"buy",
                 "amount_usd":600,"reference_price":0.01,"reason":"test"}
    assert c.post("/v1/paper/orders", headers=h(READ), json=too_large).status_code == 422
    order = dict(too_large, amount_usd=250)
    placed = c.post("/v1/paper/orders", headers=h(READ), json=order)
    assert placed.status_code == 200
    assert placed.json()["mode"] == "paper"
    assert placed.json()["fill_price"] > placed.json()["reference_price"]
    overview = c.get("/v1/paper/overview", headers=h(READ)).json()["accounts"]
    approval = next(a for a in overview if a["strategy"] == "approval")
    automatic = next(a for a in overview if a["strategy"] == "automatic")
    assert approval["cash_balance"] == 9750
    assert approval["open_positions"] == 1
    assert automatic["cash_balance"] == 10000
    assert c.get("/v1/paper/activity?strategy=approval", headers=h(READ)).json()["activity"]


def test_paper_sell_records_realized_result_and_never_live_mode():
    c, _ = make_client()
    base = {"strategy":"automatic","asset_id":"MintXYZ","token_name":"Demo","side":"buy",
            "amount_usd":200,"reference_price":0.02,"reason":"entry"}
    assert c.post("/v1/paper/orders", headers=h(READ), json=base).status_code == 200
    sell = dict(base, side="sell", amount_usd=100, reference_price=0.025, reason="exit")
    result = c.post("/v1/paper/orders", headers=h(READ), json=sell)
    assert result.status_code == 200
    assert result.json()["realized_pnl_usd"] > 0
    assert result.json()["mode"] == "paper"


def test_viewing_wallet_activity_does_not_advance_ingestion_checkpoint(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None
        def json(self):
            return [{
                "signature": "view-only-signature",
                "timestamp": 1790000000,
                "description": "swap",
                "type": "SWAP",
                "source": "JUPITER",
                "tokenTransfers": [],
                "nativeTransfers": [],
            }]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setenv("HELIUS_API_KEY", "test-key")
    monkeypatch.setattr("app.main.httpx.AsyncClient", FakeAsyncClient)
    c, _ = make_client()
    assert c.post("/v1/copytraders/wallets", headers=h(READ),
                  json={"wallet_address": "11111111111111111111111111111111"}).status_code == 200

    response = c.get(
        "/v1/copytraders/activity?wallet=11111111111111111111111111111111",
        headers=h(READ),
    )
    assert response.status_code == 200
    assert response.json()["transactions"][0]["signature"] == "view-only-signature"

    wallets = c.get("/v1/copytraders/wallets", headers=h(READ)).json()["wallets"]
    assert wallets[0]["last_signature"] is None


def test_helius_worker_status_distinguishes_configured_from_running(monkeypatch):
    monkeypatch.setenv("HELIUS_API_KEY", "test-key")
    c, app = make_client()
    response = c.get("/v1/integrations/helius/worker-status", headers=h(READ))
    assert response.status_code == 200
    body = response.json()
    # In-memory test apps intentionally do not start background workers.
    assert body["configured"] is True
    assert body["worker_state"] == "not_started"
    assert body["worker_running"] is False
    assert body["tracked_wallet_count"] == 0
    assert body["last_success_at"] is None


def test_helius_worker_status_reports_disabled_without_secret(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    c, _ = make_client()
    body = c.get("/v1/integrations/helius/worker-status", headers=h(READ)).json()
    assert body["configured"] is False
    assert body["worker_state"] == "disabled"
    assert body["worker_running"] is False


def test_bulk_add_wallets_imports_many_and_skips_duplicates():
    c, _ = make_client()
    addresses = [
        "11111111111111111111111111111111",
        "Vote111111111111111111111111111111111111111",
        "11111111111111111111111111111111",
    ]
    response = c.post("/v1/copytraders/wallets/bulk", headers=h(READ),
                      json={"wallet_addresses": addresses})
    assert response.status_code == 200
    body = response.json()
    assert body["submitted_count"] == 3
    assert body["unique_valid_count"] == 2
    assert body["added_count"] == 2
    assert body["duplicate_count"] == 1
    assert body["invalid_count"] == 0
    wallets = c.get("/v1/copytraders/wallets", headers=h(READ)).json()["wallets"]
    assert {w["wallet_address"] for w in wallets} == set(addresses[:2])


def test_bulk_add_wallets_skips_invalid_and_existing_addresses():
    c, _ = make_client()
    existing = "11111111111111111111111111111111"
    c.post("/v1/copytraders/wallets", headers=h(READ), json={"wallet_address": existing})
    response = c.post("/v1/copytraders/wallets/bulk", headers=h(READ),
                      json={"wallet_addresses": [existing, "not-a-wallet", "Vote111111111111111111111111111111111111111"]})
    assert response.status_code == 200
    body = response.json()
    assert body["added_count"] == 1
    assert body["duplicate_count"] == 1
    assert body["invalid_count"] == 1
    assert body["invalid_addresses"] == ["not-a-wallet"]


def test_bulk_add_wallets_requires_read_auth():
    c, _ = make_client()
    assert c.post("/v1/copytraders/wallets/bulk", json={
        "wallet_addresses": ["11111111111111111111111111111111"]
    }).status_code == 401


def test_bulk_add_wallets_caps_request_at_one_thousand_addresses():
    c, _ = make_client()
    response = c.post("/v1/copytraders/wallets/bulk", headers=h(READ),
                      json={"wallet_addresses": ["11111111111111111111111111111111"] * 1001})
    assert response.status_code == 422
