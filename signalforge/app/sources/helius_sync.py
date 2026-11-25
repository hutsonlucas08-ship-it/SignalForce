"""Persist parsed Helius wallet swap activity into the smart-money event tables.

This module is deliberately read-only: it fetches no transactions and never signs
or submits transactions. Callers supply Enhanced Transactions API rows.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from app.db import utcnow

SOURCE = "helius"


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result < 0 or result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _occurred_at(timestamp: Any) -> str | None:
    try:
        value = int(timestamp)
        if value <= 0:
            return None
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_wallet_swaps(wallet_address: str, transactions: Iterable[dict]) -> list[dict]:
    """Extract only unambiguous token transfers involving wallet_address.

    Incoming tokens are classified as buys and outgoing tokens as sells.
    USD price/value is intentionally left unknown; Helius transfer amounts alone
    do not establish a reliable historical USD valuation.
    """
    if not isinstance(wallet_address, str) or not wallet_address.strip():
        raise ValueError("wallet_address is required")

    events: list[dict] = []
    for tx in transactions:
        if not isinstance(tx, dict):
            continue
        signature = tx.get("signature")
        occurred_at = _occurred_at(tx.get("timestamp"))
        if not isinstance(signature, str) or not signature or not occurred_at:
            continue

        token_transfers = tx.get("tokenTransfers")
        if not isinstance(token_transfers, list):
            continue

        native_transfers = tx.get("nativeTransfers")
        sol_in = 0.0
        sol_out = 0.0
        if isinstance(native_transfers, list):
            for transfer in native_transfers:
                if not isinstance(transfer, dict):
                    continue
                lamports = _number(transfer.get("amount"))
                if lamports is None:
                    continue
                if transfer.get("fromUserAccount") == wallet_address:
                    sol_out += lamports / 1_000_000_000
                if transfer.get("toUserAccount") == wallet_address:
                    sol_in += lamports / 1_000_000_000

        for transfer in token_transfers:
            if not isinstance(transfer, dict):
                continue
            mint = transfer.get("mint")
            amount = _number(transfer.get("tokenAmount"))
            if not isinstance(mint, str) or not mint or amount is None or amount == 0:
                continue
            sender = transfer.get("fromUserAccount")
            recipient = transfer.get("toUserAccount")
            if recipient == wallet_address and sender != wallet_address:
                side = "buy"
                sol_amount = sol_out if sol_out > 0 else None
            elif sender == wallet_address and recipient != wallet_address:
                side = "sell"
                sol_amount = sol_in if sol_in > 0 else None
            else:
                continue

            events.append({
                "wallet_address": wallet_address,
                "signature": signature,
                "mint": mint,
                "token_name": None,
                "token_symbol": None,
                "side": side,
                "token_amount": amount,
                "sol_amount": sol_amount,
                "usd_amount": None,
                "price_usd": None,
                "market_cap_usd": None,
                "occurred_at": occurred_at,
                "source": SOURCE,
            })
    return events


def store_wallet_transactions(conn, wallet_address: str, transactions: Iterable[dict]) -> dict:
    """Insert parsed events idempotently and return insert/skip counts."""
    events = parse_wallet_swaps(wallet_address, transactions)
    inserted = 0
    observed_at = utcnow()
    for event in events:
        cursor = conn.execute(
            """
            INSERT INTO wallet_trade_events (
                wallet_address, signature, mint, token_name, token_symbol, side,
                token_amount, sol_amount, usd_amount, price_usd, market_cap_usd,
                occurred_at, source, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING
            """,
            (
                event["wallet_address"], event["signature"], event["mint"],
                event["token_name"], event["token_symbol"], event["side"],
                event["token_amount"], event["sol_amount"], event["usd_amount"],
                event["price_usd"], event["market_cap_usd"], event["occurred_at"],
                event["source"], observed_at,
            ),
        )
        inserted += cursor.rowcount
    conn.commit()
    return {"parsed": len(events), "inserted": inserted, "duplicates": len(events) - inserted}
