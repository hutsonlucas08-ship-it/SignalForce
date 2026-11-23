"""Background, read-only Helius polling for tracked Solana wallets."""
from __future__ import annotations

import asyncio
import logging
import os

import httpx

from app import store
from app.db import connect, utcnow
from app.sources.helius_sync import store_wallet_transactions

log = logging.getLogger("signalforge.helius")
SOURCE_NAME = "helius_wallet_sync"
API_URL = "https://api-mainnet.helius-rpc.com/v0/addresses/{wallet}/transactions"


async def sync_once(db_path: str, api_key: str, *, client=None, limit: int = 100) -> dict:
    """Poll each tracked wallet once and persist newly observed swaps."""
    if not api_key:
        raise ValueError("Helius API key is required")
    own_client = client is None
    http = client or httpx.AsyncClient(timeout=20.0)
    conn = connect(db_path)
    totals = {"wallets_checked": 0, "wallets_succeeded": 0,
              "transactions_parsed": 0, "transactions_inserted": 0,
              "duplicates": 0, "wallet_errors": 0}
    try:
        store.register_source(conn, SOURCE_NAME, "helius_polling")
        wallets = conn.execute(
            "SELECT wallet_address, last_signature FROM tracked_wallets ORDER BY added_at, id"
        ).fetchall()
        page_limit = max(1, min(int(limit), 100))
        for row in wallets:
            wallet = row["wallet_address"]
            checkpoint = row["last_signature"]
            totals["wallets_checked"] += 1
            try:
                before_signature = None
                newest_signature = None
                caught_up = checkpoint is None
                for _page in range(20):
                    params = {"api-key": api_key, "limit": page_limit,
                              "sort-order": "desc", "type": "SWAP",
                              "token-accounts": "balanceChanged"}
                    if before_signature:
                        params["before-signature"] = before_signature
                    response = await http.get(API_URL.format(wallet=wallet), params=params)
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list):
                        raise ValueError("Unexpected Helius response format")
                    if not payload:
                        caught_up = True
                        break

                    if newest_signature is None:
                        newest_signature = payload[0].get("signature")

                    new_rows = []
                    found_checkpoint = False
                    for transaction in payload:
                        signature = transaction.get("signature") if isinstance(transaction, dict) else None
                        if checkpoint and signature == checkpoint:
                            found_checkpoint = True
                            break
                        new_rows.append(transaction)

                    result = store_wallet_transactions(conn, wallet, new_rows)
                    totals["transactions_parsed"] += result["parsed"]
                    totals["transactions_inserted"] += result["inserted"]
                    totals["duplicates"] += result["duplicates"]

                    if checkpoint is None or found_checkpoint or len(payload) < page_limit:
                        caught_up = True
                        break

                    before_signature = payload[-1].get("signature")
                    if not before_signature:
                        raise ValueError("Helius page did not contain a pagination signature")

                if caught_up:
                    if newest_signature:
                        conn.execute(
                            "UPDATE tracked_wallets SET last_signature=?, last_seen_at=? WHERE wallet_address=?",
                            (newest_signature, utcnow(), wallet),
                        )
                        conn.commit()
                    totals["wallets_succeeded"] += 1
                else:
                    # Keep the old checkpoint so the next cycle resumes without silently skipping history.
                    totals["wallet_errors"] += 1
                    log.warning("Helius polling page cap reached for wallet %s; checkpoint retained", wallet)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                totals["wallet_errors"] += 1
                log.warning("Helius polling failed for wallet %s (%s)", wallet, type(exc).__name__)
        if totals["wallet_errors"]:
            store.record_source_failure(
                conn, SOURCE_NAME,
                f"{totals['wallet_errors']} of {totals['wallets_checked']} wallet requests failed",
            )
        else:
            store.record_source_success(conn, SOURCE_NAME)
        return totals
    finally:
        conn.close()
        if own_client:
            await http.aclose()


async def run_helius_sync(db_path: str, stop_event: asyncio.Event,
                          *, interval_seconds: int = 60, limit: int = 100) -> None:
    """Poll until application shutdown; a failed cycle does not kill the worker."""
    while not stop_event.is_set():
        key = os.getenv("HELIUS_API_KEY")
        if key:
            try:
                await sync_once(db_path, key, limit=limit)
            except Exception as exc:
                log.exception("Helius polling cycle failed (%s)", type(exc).__name__)
                conn = connect(db_path)
                try:
                    store.register_source(conn, SOURCE_NAME, "helius_polling")
                    store.record_source_failure(conn, SOURCE_NAME, type(exc).__name__)
                finally:
                    conn.close()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(5, interval_seconds))
        except asyncio.TimeoutError:
            continue
