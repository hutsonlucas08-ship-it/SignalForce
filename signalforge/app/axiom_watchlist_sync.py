"""Safe synchronization of the imported X watchlist to J7Tracker."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

WATCHLIST = json.loads(
    Path(__file__).with_name("axiom_x_watchlist.json").read_text(encoding="utf-8")
)
HANDLES = sorted({row["handle"].lower() for row in WATCHLIST.get("accounts", [])})


async def sync_available_accounts(jwt: str) -> dict:
    """Add only main-feed/available accounts; never consume paid custom slots."""
    base = "https://core.j7tracker.io"
    headers = {"Authorization": "Bearer " + jwt, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=20.0, headers=headers) as client:
        response = await client.get(base + "/api/watched-accounts?fresh=1")
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            raise RuntimeError("J7Tracker returned an invalid watched-account response")
        main = {
            str(row.get("handle", "")).lower()
            for row in data.get("x", {}).get("accounts", [])
            if isinstance(row, dict)
        }
        hidden = {str(value).lower() for value in data.get("hidden", [])}
        custom = {str(value).lower() for value in data.get("custom", {}).get("accounts", [])}
        available_active = {
            str(value).lower()
            for value in data.get("custom", {}).get("availableAccounts", [])
        }
        pool = {
            str(value).lower()
            for value in data.get("available", {}).get("accounts", [])
        }
        already_active = (main - hidden) | custom | available_active
        to_unhide = sorted(set(HANDLES) & main & hidden)
        for handle in to_unhide:
            result = await client.post(base + "/api/accounts", json={"handle": handle})
            result.raise_for_status()
        to_add = sorted((set(HANDLES) - already_active - set(to_unhide)) & pool)
        added = []
        for offset in range(0, len(to_add), 25):
            batch = to_add[offset:offset + 25]
            result = await client.post(
                base + "/api/accounts/available", json={"handles": batch}
            )
            result.raise_for_status()
            added.extend(result.json().get("addedHandles", []))
        active = already_active | set(to_unhide) | {str(value).lower() for value in added}
        return {
            "watchlist_count": len(HANDLES),
            "already_active": len(already_active),
            "unhidden": len(to_unhide),
            "added_available": len(added),
            "not_available_or_custom_slot_required": sorted(set(HANDLES) - active),
            "custom_slots_spent": 0,
            "note": "No paid/custom slots were used. Accounts not available in J7 must be added there separately.",
        }
