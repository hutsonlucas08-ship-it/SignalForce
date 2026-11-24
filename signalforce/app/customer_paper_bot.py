"""Tenant-isolated automatic paper trading driven by the shared tracked-wallet feed.

This worker never signs transactions or sends real orders. It reads the existing
shared tracked_wallets/wallet_trade_events signal source and writes only to
customer-owned paper tables.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from . import customer_data, paper_bot
from .db import connect, utcnow
from .research import scan_assets

log = logging.getLogger("signalforce.customer_paper_bot")
INTERVAL_SECONDS = 30.0
MODE_RULES = {
    "gradual": {"position_fraction": 0.03, "max_positions": 4, "min_score": 55.0, "stop_loss": -8.0, "take_profit": 25.0},
    "normal": {"position_fraction": 0.05, "max_positions": 7, "min_score": 45.0, "stop_loss": -10.0, "take_profit": 35.0},
    "aggressive": {"position_fraction": 0.08, "max_positions": 10, "min_score": 35.0, "stop_loss": -15.0, "take_profit": 50.0},
}


def _account_equity(conn, account):
    rows = conn.execute(
        "SELECT quantity,average_entry_price,last_price FROM customer_paper_positions WHERE paper_account_id=?",
        (account["id"],),
    ).fetchall()
    market_value = sum(float(r["quantity"]) * (float(r["last_price"]) or float(r["average_entry_price"])) for r in rows)
    return float(account["cash_balance_usd"]) + market_value


def _sell_position(conn, account, position, price, reason):
    amount = float(position["quantity"]) * float(price)
    if amount <= 0:
        return
    customer_data.place_paper_trade(
        conn, account["account_id"], account["id"],
        asset_id=position["asset_id"], token_name=position["token_name"],
        side="sell", amount_usd=amount, price_usd=price, reason=reason,
    )


def process_customer_accounts(conn, pairs, wallet_signals):
    """Apply one market snapshot to every enabled tenant without sharing balances."""
    summary = {"customers": 0, "buys": 0, "sells": 0, "targets_reached": 0}
    rows = conn.execute(
        "SELECT * FROM customer_paper_accounts WHERE enabled=1 AND auto_status='running' ORDER BY created_at"
    ).fetchall()
    for raw in rows:
        account = dict(raw)
        rules = MODE_RULES.get(account["strategy_mode"], MODE_RULES["normal"])
        positions = conn.execute(
            "SELECT * FROM customer_paper_positions WHERE paper_account_id=?",
            (account["id"],),
        ).fetchall()
        # Mark positions and apply independent, mode-specific exits.
        for raw_pos in positions:
            pos = dict(raw_pos)
            pair = pairs.get(pos["asset_id"])
            try:
                price = float((pair or {}).get("priceUsd"))
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0:
                continue
            conn.execute(
                "UPDATE customer_paper_positions SET last_price=?,updated_at=? WHERE id=? AND paper_account_id=?",
                (price, utcnow(), pos["id"], account["id"]),
            )
            change_pct = (price / float(pos["average_entry_price"]) - 1.0) * 100.0
            if change_pct <= rules["stop_loss"]:
                _sell_position(conn, account, pos, price, "Automatic " + account["strategy_mode"] + " stop-loss")
                summary["sells"] += 1
            elif change_pct >= rules["take_profit"]:
                _sell_position(conn, account, pos, price, "Automatic " + account["strategy_mode"] + " take-profit")
                summary["sells"] += 1

        conn.commit()
        account = dict(conn.execute(
            "SELECT * FROM customer_paper_accounts WHERE id=? AND account_id=?",
            (account["id"], account["account_id"]),
        ).fetchone())
        equity = _account_equity(conn, account)
        profit = equity - float(account["starting_capital_usd"])
        if profit >= float(account["profit_target_usd"]):
            conn.execute(
                "UPDATE customer_paper_accounts SET enabled=0,auto_status='target_reached',updated_at=? WHERE id=? AND account_id=?",
                (utcnow(), account["id"], account["account_id"]),
            )
            conn.commit()
            summary["targets_reached"] += 1
            continue

        current = conn.execute(
            "SELECT COUNT(*) n FROM customer_paper_positions WHERE paper_account_id=?",
            (account["id"],),
        ).fetchone()["n"]
        if current >= rules["max_positions"]:
            conn.execute("UPDATE customer_paper_accounts SET last_scan_at=? WHERE id=?", (utcnow(), account["id"]))
            conn.commit()
            summary["customers"] += 1
            continue
        candidates = []
        for mint, signal in wallet_signals.items():
            if conn.execute(
                "SELECT 1 FROM customer_paper_positions WHERE paper_account_id=? AND asset_id=?",
                (account["id"], mint),
            ).fetchone():
                continue
            pair = pairs.get(mint)
            if not pair:
                continue
            score, _ = paper_bot._score(pair)
            if score is None or score < rules["min_score"]:
                continue
            # Only shared tracked-wallet buys can trigger a customer entry.
            if int(signal.get("wallet_count") or 0) < 1:
                continue
            candidates.append((score + min(int(signal["wallet_count"]) * 10, 25), mint, pair))
        candidates.sort(reverse=True, key=lambda item: item[0])
        if candidates:
            _, mint, pair = candidates[0]
            price = float(pair["priceUsd"])
            positions_value = sum(
                float(p["quantity"]) * (float(p["last_price"]) or float(p["average_entry_price"]))
                for p in conn.execute(
                    "SELECT quantity,last_price,average_entry_price FROM customer_paper_positions WHERE paper_account_id=?",
                    (account["id"],),
                ).fetchall()
            )
            equity = float(account["cash_balance_usd"]) + positions_value
            amount = min(float(account["cash_balance_usd"]), equity * rules["position_fraction"])
            if amount >= 1:
                base = pair.get("baseToken") or {}
                customer_data.place_paper_trade(
                    conn, account["account_id"], account["id"], asset_id=mint,
                    token_name=base.get("name") or base.get("symbol") or mint[:8],
                    side="buy", amount_usd=amount, price_usd=price,
                    reason="Automated shared-wallet signal · " + account["strategy_mode"].title() + " mode",
                )
                conn.execute(
                    "UPDATE customer_paper_positions SET last_price=? WHERE paper_account_id=? AND asset_id=?",
                    (price, account["id"], mint),
                )
                summary["buys"] += 1
        conn.execute("UPDATE customer_paper_accounts SET last_scan_at=?,updated_at=? WHERE id=?",
                     (utcnow(), utcnow(), account["id"]))
        conn.commit()
        summary["customers"] += 1
    return summary


async def run_customer_paper_bot(db_path: str, database_url: str | None, stop_event: asyncio.Event):
    """One worker per service process; all account writes are tenant-scoped."""
    async with httpx.AsyncClient(timeout=10.0, headers={"Accept": "application/json"}) as client:
        while not stop_event.is_set():
            started = time.monotonic()
            conn = None
            try:
                conn = connect(db_path, database_url)
                enabled = conn.execute(
                    "SELECT 1 FROM customer_paper_accounts WHERE enabled=1 AND auto_status='running' LIMIT 1"
                ).fetchone()
                if enabled:
                    assets = scan_assets(conn, 100)
                    signals = paper_bot._recent_wallet_buy_signals(conn)
                    held = [r["asset_id"] for r in conn.execute(
                        "SELECT DISTINCT asset_id FROM customer_paper_positions"
                    ).fetchall()]
                    mints = list(dict.fromkeys(
                        held + list(signals.keys()) + [a["asset_id"] for a in assets if a.get("asset_id")]
                    ))[:180]
                    pairs = await paper_bot._fetch_pairs(client, mints) if mints else {}
                    result = process_customer_accounts(conn, pairs, signals)
                    log.info("Customer paper scan: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Customer paper trader scan failed")
            finally:
                if conn is not None:
                    conn.close()
            remaining = max(0.0, INTERVAL_SECONDS - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
