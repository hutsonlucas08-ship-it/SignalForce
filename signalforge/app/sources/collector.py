"""Long-running, read-only PumpPortal launch and migration collector."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib.parse import urlencode

import websockets

from app import store
from app.sources.ingestion import process_message
from app.sources.pumpportal import MAX_MESSAGE_SIZE

SOURCE_NAME = "pumpportal"
DEFAULT_WS_URL = "wss://pumpportal.fun/api/data"
logger = logging.getLogger(__name__)
_SECRET_RE = re.compile(r"(?i)(api[-_]?key=)[^&\\s'\"]+")


def redact_secrets(text: str) -> str:
    """Redact query-string API keys before logging or persisting provider errors."""
    return _SECRET_RE.sub(r"\\1[redacted]", text)


async def _wait_backoff(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


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
                logger.info("Connected to PumpPortal; subscribing to new-token and migration events")
                await websocket.send(json.dumps({"method": "subscribeNewToken"}))
                await websocket.send(json.dumps({"method": "subscribeMigration"}))
                logger.info("Subscriptions sent; waiting for token events")

                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue

                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")

                    try:
                        observation_id = process_message(conn, SOURCE_NAME, raw)
                        if observation_id is not None:
                            backoff = min_backoff
                            logger.info("Stored PumpPortal observation id=%s", observation_id)
                    except (ValueError, UnicodeError) as exc:
                        # Invalid provider frames are counted by ingestion and
                        # must not terminate an otherwise healthy stream.
                        logger.warning("Ignored invalid PumpPortal frame: %s", redact_secrets(str(exc)))
                        continue

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            safe_error = f"{type(exc).__name__}: {redact_secrets(str(exc))[:400]}"
            # Tracebacks can include the websocket URL, which may contain a provider key.
            logger.error("PumpPortal collector connection failed: %s", safe_error)
            store.record_source_failure(conn, SOURCE_NAME, safe_error)

        if stop_event.is_set():
            break

        await _wait_backoff(stop_event, backoff)
        backoff = min(backoff * 2, max_backoff)



async def _run_from_environment() -> None:
    from app.config import load_settings
    from app.db import connect, run_migrations

    settings = load_settings()
    conn = connect(settings.db_path)
    try:
        run_migrations(conn)
        await run_pumpportal_collector(conn)
    finally:
        conn.close()


def main() -> None:
    """Run with: python -m app.sources.collector (from the signalforge folder)."""
    import logging

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        asyncio.run(_run_from_environment())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
