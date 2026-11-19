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
    assert first == ["001_init.sql"] and run_migrations(conn) == []
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
