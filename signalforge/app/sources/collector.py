"""Long-running, read-only PumpPortal launch and migration collector."""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlencode

import websockets

from app import store
from app.sources.ingestion import process_message
from app.sources.pumpportal import MAX_MESSAGE_SIZE

SOURCE_NAME = "pumpportal"
DEFAULT_WS_URL = "wss://pumpportal.fun/api/data"


def build_ws_url(api_key: str | None = None) -> str:
    """Build the public data endpoint URL without exposing credentials."""
    key = api_key if api_key is not None else os.getenv("PUMPPORTAL_API_KEY")
    if not key:
        return DEFAULT_WS_URL
    return f"{DEFAULT_WS_URL}?{urlencode({'api-key': key})}"


async def run_pumpportal_collector(
    conn,
    *,
    url: str | None = None,
    connect=None,
    stop_event: asyncio.Event | None = None,
    min_backoff: float = 1.0,
    max_backoff: float = 30.0,
) -> None:
    """Collect free new-token and migration events, reconnecting on failures.

    This collector deliberately does not subscribe to metered trade streams.
    The supplied SQLite connection is owned by the caller and must remain open
    for the lifetime of this coroutine.
    """
    if min_backoff <= 0 or max_backoff < min_backoff:
        raise ValueError("Invalid reconnect backoff settings")

    stop_event = stop_event or asyncio.Event()
    connect = connect or websockets.connect
    endpoint = url or build_ws_url()
    store.register_source(conn, SOURCE_NAME, "websocket")
    backoff = min_backoff

    while not stop_event.is_set():
        try:
            async with connect(
                endpoint,
                ping_interval=20,
                ping_timeout=20,
                max_size=MAX_MESSAGE_SIZE,
            ) as websocket:
                await websocket.send(json.dumps({"method": "subscribeNewToken"}))
                await websocket.send(json.dumps({"method": "subscribeMigration"}))
                store.record_source_success(conn, SOURCE_NAME)
                backoff = min_backoff

                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue

                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")

                    try:
                        process_message(conn, SOURCE_NAME, raw)
                    except (ValueError, UnicodeError):
                        # Invalid provider frames are counted by ingestion and
                        # must not terminate an otherwise healthy stream.
                        continue

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            store.record_source_failure(
                conn,
                SOURCE_NAME,
                f"{type(exc).__name__}: {str(exc)[:400]}",
            )

        if stop_event.is_set():
            break

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, max_backoff)
