"""Safe parsing and basic validation of public PumpPortal messages."""

import json
from typing import Any


MAX_MESSAGE_SIZE = 256 * 1024


class PumpPortalMessageError(ValueError):
    """Raised when a PumpPortal message cannot be parsed or validated."""


def parse_message(raw: str) -> dict[str, Any]:
    """Parse a JSON message and require a non-empty object."""
    if not isinstance(raw, str):
        raise PumpPortalMessageError("Message must be a string")

    if not raw.strip():
        raise PumpPortalMessageError("Message cannot be empty")

    if len(raw.encode("utf-8")) > MAX_MESSAGE_SIZE:
        raise PumpPortalMessageError("Message exceeds maximum size")

    try:
        message = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PumpPortalMessageError("Invalid JSON message") from exc

    if not isinstance(message, dict):
        raise PumpPortalMessageError("Expected a JSON object")

    if not message:
        raise PumpPortalMessageError("Message object cannot be empty")

    if not all(isinstance(key, str) for key in message):
        raise PumpPortalMessageError("Message keys must be strings")

    return message
