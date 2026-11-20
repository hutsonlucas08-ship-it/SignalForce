import json

import pytest

from app.db import connect, run_migrations
from app.sources.ingestion import IngestionError, process_message
from app.store import register_source


@pytest.fixture
def ingestion_db(tmp_path):
    conn = connect(str(tmp_path / "ingestion.db"))
    run_migrations(conn)
    register_source(conn, "pumpportal", "websocket")

    yield conn
    conn.close()


def test_process_valid_message(ingestion_db):
    raw = json.dumps({
        "mint": "TEST123",
        "txType": "create",
        "name": "Example",
        "symbol": "EX",
    })

    observation_id = process_message(ingestion_db, "pumpportal", raw)

    row = ingestion_db.execute(
        "SELECT * FROM observations WHERE id = ?",
        (observation_id,),
    ).fetchone()

    assert row is not None
    assert row["asset_id"] == "TEST123"
    assert row["kind"] == "create"
    assert json.loads(row["payload"])["symbol"] == "EX"


def test_reject_missing_asset_id(ingestion_db):
    raw = json.dumps({"txType": "create"})

    with pytest.raises(IngestionError):
        process_message(ingestion_db, "pumpportal", raw)


def test_reject_missing_message_type(ingestion_db):
    raw = json.dumps({"mint": "TEST123"})

    with pytest.raises(IngestionError):
        process_message(ingestion_db, "pumpportal", raw)


def test_reject_malformed_json(ingestion_db):
    from app.sources.pumpportal import PumpPortalMessageError

    with pytest.raises(PumpPortalMessageError):
        process_message(ingestion_db, "pumpportal", "not json")


def test_invalid_message_records_source_failure(ingestion_db):
    with pytest.raises(IngestionError):
        process_message(
            ingestion_db,
            "pumpportal",
            '{"txType": "create"}',
        )

    row = ingestion_db.execute(
        "SELECT consecutive_failures FROM data_sources WHERE name = ?",
        ("pumpportal",),
    ).fetchone()

    assert row["consecutive_failures"] == 1


def test_migration_message_without_type_is_classified_as_migration(ingestion_db):
    observation_id = process_message(
        ingestion_db,
        "pumpportal",
        json.dumps({"mint": "MIGRATED123", "pool": "POOL123"}),
    )

    row = ingestion_db.execute(
        "SELECT asset_id, kind FROM observations WHERE id = ?",
        (observation_id,),
    ).fetchone()

    assert tuple(row) == ("MIGRATED123", "migration")


def test_subscription_acknowledgement_is_ignored_without_failure(ingestion_db):
    result = process_message(
        ingestion_db,
        "pumpportal",
        json.dumps({"message": "Successfully subscribed to new token events"}),
    )

    assert result is None
    assert ingestion_db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
    row = ingestion_db.execute(
        "SELECT consecutive_failures, last_error FROM data_sources WHERE name = ?",
        ("pumpportal",),
    ).fetchone()
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is None


def test_successful_ingestion_clears_previous_source_error(ingestion_db):
    from app.store import record_source_failure

    record_source_failure(ingestion_db, "pumpportal", "temporary error")
    process_message(
        ingestion_db,
        "pumpportal",
        json.dumps({"mint": "RECOVERED123", "txType": "create"}),
    )

    row = ingestion_db.execute(
        "SELECT consecutive_failures, last_error FROM data_sources WHERE name = ?",
        ("pumpportal",),
    ).fetchone()
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is None
