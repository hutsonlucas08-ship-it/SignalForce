import asyncio
import json

import pytest

from app.db import connect, run_migrations
from app.sources.collector import build_ws_url, run_pumpportal_collector


class FakeSocket:
    def __init__(self, messages, stop_event):
        self.messages = iter(messages)
        self.stop_event = stop_event
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        try:
            message = next(self.messages)
        except StopIteration:
            self.stop_event.set()
            return "{}"
        if message == "__STOP__":
            self.stop_event.set()
            return json.dumps({
                "mint": "MIGRATION123",
                "txType": "migration",
                "pool": "POOL123",
            })
        return message


@pytest.fixture
def collector_db(tmp_path):
    conn = connect(str(tmp_path / "collector.db"))
    run_migrations(conn)
    yield conn
    conn.close()


def test_build_ws_url_does_not_require_api_key(monkeypatch):
    monkeypatch.delenv("PUMPPORTAL_API_KEY", raising=False)
    assert build_ws_url() == "wss://pumpportal.fun/api/data"


def test_build_ws_url_encodes_api_key():
    assert build_ws_url("key with spaces") == (
        "wss://pumpportal.fun/api/data?api-key=key+with+spaces"
    )


@pytest.mark.asyncio
async def test_collector_subscribes_to_free_streams_and_stores_event(collector_db):
    stop_event = asyncio.Event()
    socket = FakeSocket([
        json.dumps({
            "mint": "NEW_TOKEN_123",
            "txType": "create",
            "name": "Example",
            "symbol": "EX",
        }),
        "__STOP__",
    ], stop_event)
    calls = []

    def fake_connect(url, **kwargs):
        calls.append((url, kwargs))
        return socket

    await run_pumpportal_collector(
        collector_db,
        url="wss://example.test/feed",
        connect=fake_connect,
        stop_event=stop_event,
    )

    assert calls[0][0] == "wss://example.test/feed"
    assert socket.sent == [
        {"method": "subscribeNewToken"},
        {"method": "subscribeMigration"},
    ]
    row = collector_db.execute(
        "SELECT asset_id, kind FROM observations"
    ).fetchone()
    assert tuple(row) == ("NEW_TOKEN_123", "create")


@pytest.mark.asyncio
async def test_collector_registers_source_and_stops_cleanly(collector_db):
    stop_event = asyncio.Event()
    stop_event.set()

    await run_pumpportal_collector(
        collector_db,
        connect=lambda *_args, **_kwargs: pytest.fail("must not connect when stopped"),
        stop_event=stop_event,
    )

    row = collector_db.execute(
        "SELECT name, kind FROM data_sources WHERE name = 'pumpportal'"
    ).fetchone()
    assert tuple(row) == ("pumpportal", "websocket")
