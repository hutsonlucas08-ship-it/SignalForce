from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, run_migrations
from app.main import create_app
from app.research import analyze_asset, scan_assets
from app.store import add_observation, register_source


def seeded_db(tmp_path):
    conn = connect(str(tmp_path / "research.db"))
    run_migrations(conn)
    register_source(conn, "feed_a", "test")
    register_source(conn, "feed_b", "test")
    add_observation(conn, "feed_a", "ASSET1", "create", '{"name":"Example"}')
    add_observation(conn, "feed_b", "ASSET1", "trade", '{"price":1.2}')
    add_observation(conn, "feed_a", "ASSET2", "create", '{"name":"Other"}')
    return conn


def test_scan_assets_counts_observations_and_sources(tmp_path):
    conn = seeded_db(tmp_path)
    try:
        assets = scan_assets(conn)
        by_id = {asset["asset_id"]: asset for asset in assets}
        assert by_id["ASSET1"]["observation_count"] == 2
        assert by_id["ASSET1"]["source_count"] == 2
        assert by_id["ASSET2"]["observation_count"] == 1
    finally:
        conn.close()


def test_scan_assets_records_first_observed_market_cap(tmp_path):
    conn = seeded_db(tmp_path)
    try:
        add_observation(conn, "feed_a", "CAP1", "create", '{"marketCapSol":123.45}')
        add_observation(conn, "feed_a", "CAP1", "trade", '{"marketCapSol":150.0}')
        assets = {item["asset_id"]: item for item in scan_assets(conn)}
        assert assets["CAP1"]["first_seen_market_cap_sol"] == 123.45
    finally:
        conn.close()


def test_analyze_asset_summarizes_evidence_and_flags_single_source(tmp_path):
    conn = seeded_db(tmp_path)
    try:
        report = analyze_asset(conn, "ASSET2", stale_after_seconds=300)
        assert report["status"] == "OBSERVED"
        assert report["observation_count"] == 1
        assert report["sources"] == ["feed_a"]
        assert report["kind_counts"] == {"create": 1}
        assert report["token_metadata"] == {"name": "Other"}
        assert "single_source_only" in report["flags"]
    finally:
        conn.close()


def test_analyze_asset_extracts_only_explicit_provider_fields(tmp_path):
    conn = seeded_db(tmp_path)
    try:
        add_observation(
            conn,
            "feed_a",
            "ASSET3",
            "create",
            '{"name":"Token Three","symbol":"THREE","marketCapSol":42.5,'
            '"vSolInBondingCurve":8.2,"mintAuthority":null}',
        )
        report = analyze_asset(conn, "ASSET3")
        assert report["token_metadata"] == {
            "name": "Token Three",
            "symbol": "THREE",
        }
        assert report["observed_metrics"]["marketCapSol"]["value"] == 42.5
        assert report["observed_metrics"]["vSolInBondingCurve"]["value"] == 8.2
        assert "mintAuthority" in report["payload_fields"]
        # Provider market-cap/bonding-curve values must not be presented as verified risk fields.
        assert "liquidity_usd" not in report["observed_metrics"]
    finally:
        conn.close()


def test_scan_assets_captures_original_metadata_uri(tmp_path):
    conn = seeded_db(tmp_path)
    try:
        add_observation(
            conn, "feed_a", "META1", "create",
            '{"name":"Metadata Token","uri":"https://ipfs.io/ipfs/QmExample"}',
        )
        assets = {item["asset_id"]: item for item in scan_assets(conn)}
        assert assets["META1"]["metadata_uri"] == "https://ipfs.io/ipfs/QmExample"
    finally:
        conn.close()


def test_scan_and_analysis_expose_only_https_image_urls(tmp_path):
    conn = seeded_db(tmp_path)
    try:
        add_observation(conn, "feed_a", "IMAGE1", "create",
                        '{"name":"Image Token","image":"https://cdn.example/token.png"}')
        add_observation(conn, "feed_a", "IMAGE2", "create",
                        '{"name":"Bad Image","image":"javascript:alert(1)"}')
        scanned = {item["asset_id"]: item for item in scan_assets(conn)}
        assert scanned["IMAGE1"]["image_url"] == "https://cdn.example/token.png"
        assert "image_url" not in scanned["IMAGE2"]
        assert analyze_asset(conn, "IMAGE1")["image_url"] == "https://cdn.example/token.png"
        assert analyze_asset(conn, "IMAGE2")["image_url"] is None
    finally:
        conn.close()


def test_analyze_asset_ignores_malformed_payload_without_crashing(tmp_path):
    conn = seeded_db(tmp_path)
    try:
        add_observation(conn, "feed_a", "ASSET4", "create", "not-json")
        report = analyze_asset(conn, "ASSET4")
        assert report["status"] == "OBSERVED"
        assert report["token_metadata"] == {}
        assert report["observed_metrics"] == {}
    finally:
        conn.close()


def test_analyze_asset_returns_no_data_for_unknown_asset(tmp_path):
    conn = seeded_db(tmp_path)
    try:
        report = analyze_asset(conn, "MISSING")
        assert report["status"] == "NO_DATA"
        assert report["flags"] == ["no_observations"]
    finally:
        conn.close()


def test_analyze_asset_marks_old_observations_stale(tmp_path):
    conn = seeded_db(tmp_path)
    try:
        report = analyze_asset(
            conn,
            "ASSET1",
            stale_after_seconds=1,
            now=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        assert report["status"] == "STALE"
        assert "stale_observations" in report["flags"]
    finally:
        conn.close()


def test_research_endpoints_require_read_key(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "api.db"),
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    )
    client = TestClient(create_app(settings))
    assert client.get("/v1/research/assets").status_code == 401
    assert client.get("/v1/research/assets/ASSET1/analysis").status_code == 401
    assert client.get("/v1/research/assets/ASSET1/launch-health").status_code == 401


def test_research_endpoints_return_asset_reports(tmp_path):
    db_path = tmp_path / "api_data.db"
    conn = connect(str(db_path))
    try:
        run_migrations(conn)
        register_source(conn, "feed_a", "test")
        add_observation(conn, "feed_a", "ASSET1", "create", '{"symbol":"EX"}')
    finally:
        conn.close()

    settings = Settings(
        db_path=str(db_path),
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    )
    client = TestClient(create_app(settings))
    headers = {"X-API-Key": "r" * 32}

    assets = client.get("/v1/research/assets", headers=headers)
    analysis = client.get("/v1/research/assets/ASSET1/analysis", headers=headers)

    assert assets.status_code == 200
    assert assets.json()["assets"][0]["asset_id"] == "ASSET1"
    assert analysis.status_code == 200
    assert analysis.json()["observation_count"] == 1
    assert analysis.json()["token_metadata"] == {"symbol": "EX"}


def test_market_data_endpoint_returns_empty_snapshot_without_assets(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "market_data.db"),
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    )
    client = TestClient(create_app(settings))
    response = client.get(
        "/v1/research/market-data",
        headers={"X-API-Key": "r" * 32},
    )
    assert response.status_code == 200
    assert response.json()["market_data"] == {}
    assert response.json()["source"] == "DexScreener"
    assert "Holder counts are not provided" in response.json()["note"]


def test_market_data_limit_is_validated(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "market_limit.db"),
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    )
    client = TestClient(create_app(settings))
    response = client.get(
        "/v1/research/market-data?limit=51",
        headers={"X-API-Key": "r" * 32},
    )
    assert response.status_code == 422


def test_research_asset_limit_is_validated(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "limits.db"),
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    )
    client = TestClient(create_app(settings))
    response = client.get(
        "/v1/research/assets?limit=201",
        headers={"X-API-Key": "r" * 32},
    )
    assert response.status_code == 422
