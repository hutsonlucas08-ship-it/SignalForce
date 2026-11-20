"""Read-only processing of public market-data messages."""

import json

from app import store
from app.sources.pumpportal import (
    PumpPortalMessageError,
    parse_message,
)


class IngestionError(ValueError):
    """Raised when a message cannot be accepted for storage."""


def process_message(conn, source: str, raw: str) -> int | None:
    """Validate and store one public market-data observation.

    PumpPortal also sends subscription acknowledgements over the websocket.
    Those control frames are not token observations and are safely ignored.
    """
    try:
        message = parse_message(raw)

        asset_id = message.get("mint") or message.get("asset_id")
        kind = message.get("txType") or message.get("type")

        # Subscription acknowledgements can contain a human-readable message
        # but no mint or event type. Ignore these instead of recording a
        # misleading "missing asset identifier" ingestion failure.
        if (
            not asset_id
            and not kind
            and isinstance(message.get("message"), str)
        ):
            return None

        if not kind and message.get("mint") and message.get("pool"):
            kind = "migration"

        if not isinstance(asset_id, str) or not asset_id.strip():
            raise IngestionError("Missing or invalid asset identifier")

        if not isinstance(kind, str) or not kind.strip():
            raise IngestionError("Missing or invalid message type")

        if len(asset_id) > 128 or len(kind) > 100:
            raise IngestionError("Message field exceeds allowed length")

        observation_id = store.add_observation(
            conn=conn,
            source=source,
            asset_id=asset_id.strip(),
            kind=kind.strip(),
            payload=json.dumps(message, separators=(",", ":")),
        )

        store.record_source_success(conn, source)
        return observation_id

    except (PumpPortalMessageError, IngestionError) as exc:
        store.record_source_failure(conn, source, str(exc))
        raise
