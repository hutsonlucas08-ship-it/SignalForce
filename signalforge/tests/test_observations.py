import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import connect, run_migrations
from app.main import create_app
from app.store import add_observation, get_observations, register_source


def test_add_observation_saves_record(tmp_path):
    db_path = tmp_path / "observations.db"
    conn = connect(str(db_path))

    try:
        run_migrations(conn)
        register_source(conn, "test_source", "test")

        observation_id = add_observation(
            conn=conn,
            source="test_source",
            asset_id="TEST123",
            kind="token_metadata",
            payload=json.dumps({"name": "Example", "symbol": "EX"}),
            source_ref="test-reference",
        )

        row = conn.execute(
            "SELECT * FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()

        assert row is not None
        assert row["asset_id"] == "TEST123"
        assert row["kind"] == "token_metadata"
        assert json.loads(row["payload"])["symbol"] == "EX"
        assert row["source_ref"] == "test-reference"
        assert row["collected_at"] is not None

    finally:
        conn.close()


def test_get_observations_filters_and_paginates(tmp_path):
    conn = connect(str(tmp_path / "filters.db"))

    try:
        run_migrations(conn)
        register_source(conn, "pumpportal", "test")
        register_source(conn, "other_source", "test")

        add_observation(conn, "pumpportal", "TEST123", "token_metadata", '{"n": 1}')
        add_observation(conn, "other_source", "TEST123", "token_metadata", '{"n": 2}')
        add_observation(conn, "pumpportal", "TEST123", "price_update", '{"n": 3}')

        all_records = get_observations(conn, "TEST123")
        assert len(all_records) == 3

        source_records = get_observations(
            conn, "TEST123", source="pumpportal"
        )
        assert len(source_records) == 2
        assert all(r["source"] == "pumpportal" for r in source_records)

        kind_records = get_observations(
            conn, "TEST123", kind="price_update"
        )
        assert len(kind_records) == 1
        assert kind_records[0]["kind"] == "price_update"

        page = get_observations(conn, "TEST123", limit=1, offset=1)
        assert len(page) == 1
        assert page[0]["id"] == all_records[1]["id"]

    finally:
        conn.close()


def make_api_client(db_path):
    settings = Settings(
        db_path=str(db_path),
        read_api_keys=frozenset({"r" * 32}),
        admin_api_keys=frozenset({"a" * 32}),
    )
    return TestClient(create_app(settings))


def test_observation_endpoint_requires_read_key(tmp_path):
    client = make_api_client(tmp_path / "auth.db")

    response = client.get("/v1/observations/TEST123")

    assert response.status_code == 401


def test_observation_endpoint_returns_records_and_filters(tmp_path):
    db_path = tmp_path / "api_observations.db"
    client = make_api_client(db_path)

    conn = connect(str(db_path))
    try:
        register_source(conn, "pumpportal", "test")
        register_source(conn, "other_source", "test")
        add_observation(conn, "pumpportal", "TEST123", "token_metadata", '{"name": "Example"}')
        add_observation(conn, "other_source", "TEST123", "price_update", '{"price": 1.25}')
        add_observation(conn, "pumpportal", "TEST123", "price_update", '{"price": 1.50}')
    finally:
        conn.close()

    headers = {"X-API-Key": "r" * 32}

    response = client.get(
        "/v1/observations/TEST123?source=pumpportal&kind=price_update",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["observations"][0]["payload"] == {"price": 1.50}


def test_observation_endpoint_pagination(tmp_path):
    db_path = tmp_path / "api_pagination.db"
    client = make_api_client(db_path)

    conn = connect(str(db_path))
    try:
        register_source(conn, "test_source", "test")
        for number in range(3):
            add_observation(
                conn, "test_source", "TEST123",
                "token_metadata", json.dumps({"number": number})
            )
    finally:
        conn.close()

    response = client.get(
        "/v1/observations/TEST123?limit=1&offset=1",
        headers={"X-API-Key": "r" * 32},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["limit"] == 1
    assert body["offset"] == 1


def test_observation_endpoint_rejects_invalid_pagination(tmp_path):
    client = make_api_client(tmp_path / "invalid_pagination.db")
    headers = {"X-API-Key": "r" * 32}

    assert client.get(
        "/v1/observations/TEST123?limit=0", headers=headers
    ).status_code == 422

    assert client.get(
        "/v1/observations/TEST123?limit=201", headers=headers
    ).status_code == 422

    assert client.get(
        "/v1/observations/TEST123?offset=-1", headers=headers
    ).status_code == 422
