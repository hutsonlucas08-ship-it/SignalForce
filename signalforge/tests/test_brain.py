from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, run_migrations
from app.main import create_app
from app.research import build_brain_report
from app.store import add_observation, register_source


def test_brain_report_explains_no_data(tmp_path):
    conn = connect(str(tmp_path / "brain-empty.db"))
    run_migrations(conn)
    try:
        report = build_brain_report(conn, "UNKNOWN")
        assert report["conclusion"] == "INSUFFICIENT_EVIDENCE"
        assert report["evidence"]["status"] == "NO_DATA"
        assert report["risk_screen"]["status"] == "INSUFFICIENT_DATA"
    finally:
        conn.close()


def test_brain_report_calls_out_single_source_evidence(tmp_path):
    conn = connect(str(tmp_path / "brain-single.db"))
    run_migrations(conn)
    register_source(conn, "feed_a", "test")
    add_observation(conn, "feed_a", "ASSET1", "create", '{"mint":"ASSET1"}')
    try:
        report = build_brain_report(conn, "ASSET1")
        assert report["conclusion"] == "LIMITED_EVIDENCE"
        assert report["evidence"]["source_count"] == 1
        assert "Multiple source names do not prove independent data or corroboration." in report["limitations"]
    finally:
        conn.close()


def test_brain_report_api_requires_read_key_and_returns_report(tmp_path):
    db_path = str(tmp_path / "brain-api.db")
    conn = connect(db_path)
    run_migrations(conn)
    register_source(conn, "feed_a", "test")
    add_observation(conn, "feed_a", "ASSET1", "create", '{"mint":"ASSET1"}')
    conn.close()
    settings = Settings(db_path=db_path, read_api_keys=frozenset({"r" * 32}), admin_api_keys=frozenset({"a" * 32}))
    client = TestClient(create_app(settings))
    path = "/v1/research/assets/ASSET1/report"
    assert client.get(path).status_code == 401
    response = client.get(path, headers={"X-API-Key": "r" * 32})
    assert response.status_code == 200
    assert response.json()["report_type"] == "deterministic_research_summary"


def test_brain_report_rejects_invalid_asset_id(tmp_path):
    settings = Settings(db_path=str(tmp_path / "brain-invalid.db"), read_api_keys=frozenset({"r" * 32}), admin_api_keys=frozenset({"a" * 32}))
    client = TestClient(create_app(settings))
    response = client.get("/v1/research/assets/%20/report", headers={"X-API-Key": "r" * 32})
    assert response.status_code == 422
