"""Tenant-isolated automatic paper trading with wallet and market discovery."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import customer_data, paper_bot
from .db import connect, utcnow

log = logging.getLogger("signalforce.customer_paper_bot")
log.setLevel(logging.INFO)
INTERVAL_SECONDS = 30.0
MARKET_ONLY_MIN_SCORE = 50.0
MODE_RULES = {
    "gradual": {"position_fraction": 0.03, "max_positions": 4, "min_score": 55.0, "stop_loss": -8.0, "take_profit": 25.0},
    "normal": {"position_fraction": 0.05, "max_positions": 7, "min_score": 45.0, "stop_loss": -10.0, "take_profit": 35.0},
    "aggressive": {"position_fraction": 0.08, "max_positions": 10, "min_score": 35.0, "stop_loss": -15.0, "take_profit": 50.0},
}


def _equity(conn, account):
    rows = conn.execute(
        "SELECT quantity,average_entry_price,last_price FROM customer_paper_positions WHERE paper_account_id=?",
        (account["id"],),
    ).fetchall()
    return float(account["cash_balance_usd"]) + sum(
        float(p["quantity"]) * (float(p["last_price"]) or float(p["average_entry_price"])) for p in rows
    )


def _sell(conn, account, pos, price, reason):
    amount = float(pos["quantity"]) * price
    if amount > 0:
        customer_data.place_paper_trade(
            conn, account["account_id"], account["id"], asset_id=pos["asset_id"],
            token_name=pos["token_name"], side="sell", amount_usd=amount,
            price_usd=price, reason=reason, automated=True,
        )


def process_customer_accounts(conn, pairs, wallet_signals, asset_names=None):
    """Manage exits and evaluate both wallet-confirmed and market-only entries."""
    summary = {
        "customers": 0, "buys": 0, "sells": 0, "targets_reached": 0,
        "priced_candidates": len(pairs), "wallet_signal_count": len(wallet_signals),
        "rejection_reasons": {},
    }
    names = asset_names or {}
    accounts = conn.execute(
        "SELECT * FROM customer_paper_accounts WHERE enabled=1 AND auto_status='running' ORDER BY created_at"
    ).fetchall()

    for raw in accounts:
        account = dict(raw)
        rules = MODE_RULES.get(account["strategy_mode"], MODE_RULES["normal"])
        positions = conn.execute(
            "SELECT * FROM customer_paper_positions WHERE paper_account_id=?", (account["id"],)
        ).fetchall()

        for raw_pos in positions:
            pos = dict(raw_pos)
            pair = pairs.get(pos["asset_id"])
            try:
                price = float((pair or {}).get("priceUsd"))
            except (TypeError, ValueError):
                price = 0
            if price <= 0:
                continue
            conn.execute(
                "UPDATE customer_paper_positions SET last_price=?,updated_at=? WHERE id=? AND paper_account_id=?",
                (price, utcnow(), pos["id"], account["id"]),
            )
            change = (price / float(pos["average_entry_price"]) - 1) * 100
            if change <= rules["stop_loss"]:
                _sell(conn, account, pos, price, "Automatic " + account["strategy_mode"] + " stop-loss")
                summary["sells"] += 1
            elif change >= rules["take_profit"]:
                _sell(conn, account, pos, price, "Automatic " + account["strategy_mode"] + " take-profit")
                summary["sells"] += 1

        conn.commit()
        account = dict(conn.execute(
            "SELECT * FROM customer_paper_accounts WHERE id=? AND account_id=?",
            (account["id"], account["account_id"]),
        ).fetchone())

        if _equity(conn, account) - float(account["starting_capital_usd"]) >= float(account["profit_target_usd"]):
            open_positions = conn.execute(
                "SELECT * FROM customer_paper_positions WHERE paper_account_id=?", (account["id"],)
            ).fetchall()
            for open_pos in open_positions:
                pos = dict(open_pos)
                mark = float(pos["last_price"]) or float(pos["average_entry_price"])
                _sell(conn, account, pos, mark, "Profit target reached · automatic paper close")
            conn.execute(
                "UPDATE customer_paper_accounts SET enabled=0,auto_status='target_reached',updated_at=? WHERE id=? AND account_id=?",
                (utcnow(), account["id"], account["account_id"]),
            )
            conn.commit()
            summary["targets_reached"] += 1
            continue

        count = conn.execute(
            "SELECT COUNT(*) n FROM customer_paper_positions WHERE paper_account_id=?", (account["id"],)
        ).fetchone()["n"]
        if count < rules["max_positions"]:
            cooldown_since = (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
            candidates = []
            for mint, pair in pairs.items():
                if conn.execute(
                    "SELECT 1 FROM customer_paper_positions WHERE paper_account_id=? AND asset_id=?",
                    (account["id"], mint),
                ).fetchone():
                    continue
                if conn.execute(
                    """SELECT 1 FROM customer_paper_trades
                       WHERE paper_account_id=? AND asset_id=? AND side='buy'
                         AND paper_session_id=? AND created_at>=? LIMIT 1""",
                    (account["id"], mint, account["auto_session_id"], cooldown_since),
                ).fetchone():
                    continue

                score, rejection = paper_bot._score(pair)
                if score is None:
                    reason = rejection or "Market data did not qualify"
                    summary["rejection_reasons"][reason] = summary["rejection_reasons"].get(reason, 0) + 1
                    continue

                signal = wallet_signals.get(mint)
                wallet_count = int((signal or {}).get("wallet_count") or 0)
                # Wallet buys can qualify at the selected mode threshold. Without
                # a wallet signal, require the older engine's independent momentum
                # threshold as well; never force a trade just to create activity.
                required_score = rules["min_score"] if wallet_count else max(
                    rules["min_score"], MARKET_ONLY_MIN_SCORE
                )
                if score < required_score:
                    reason = "Market score below %s threshold" % (
                        "wallet-confirmed" if wallet_count else "market-only"
                    )
                    summary["rejection_reasons"][reason] = summary["rejection_reasons"].get(reason, 0) + 1
                    continue
                combined = min(100.0, score + min(wallet_count * 10.0, 25.0))
                candidates.append((combined, mint, pair, score, wallet_count))

            candidates.sort(reverse=True, key=lambda item: item[0])
            if candidates:
                _, mint, pair, score, wallet_count = candidates[0]
                try:
                    price = float(pair.get("priceUsd"))
                except (TypeError, ValueError):
                    price = 0
                positions_value = sum(
                    float(p["quantity"]) * (float(p["last_price"]) or float(p["average_entry_price"]))
                    for p in conn.execute(
                        "SELECT quantity,last_price,average_entry_price FROM customer_paper_positions WHERE paper_account_id=?",
                        (account["id"],),
                    ).fetchall()
                )
                amount = min(
                    float(account["cash_balance_usd"]),
                    (float(account["cash_balance_usd"]) + positions_value) * rules["position_fraction"],
                )
                if price > 0 and amount >= 1:
                    base = pair.get("baseToken") or {}
                    token_name = names.get(mint) or base.get("name") or base.get("symbol") or mint[:8]
                    source = "tracked-wallet confirmed" if wallet_count else "independent market momentum"
                    customer_data.place_paper_trade(
                        conn, account["account_id"], account["id"], asset_id=mint,
                        token_name=token_name, side="buy", amount_usd=amount, price_usd=price,
                        reason="Automated %s entry · score %.1f · %s · %s mode" % (
                            source, score, wallet_count and (str(wallet_count) + " tracked wallet(s)") or "no wallet signal",
                            account["strategy_mode"].title(),
                        ),
                        automated=True,
                    )
                    conn.execute(
                        "UPDATE customer_paper_positions SET last_price=? WHERE paper_account_id=? AND asset_id=?",
                        (price, account["id"], mint),
                    )
                    summary["buys"] += 1
                else:
                    reason = "Insufficient cash or invalid price for position"
                    summary["rejection_reasons"][reason] = summary["rejection_reasons"].get(reason, 0) + 1
            elif not pairs:
                reason = "No market pairs returned for discovered candidates"
                summary["rejection_reasons"][reason] = summary["rejection_reasons"].get(reason, 0) + 1

        conn.execute(
            "UPDATE customer_paper_accounts SET last_scan_at=?,updated_at=? WHERE id=?",
            (utcnow(), utcnow(), account["id"]),
        )
        conn.commit()
        summary["customers"] += 1

    return summary


def _claim_worker_lease(conn, worker_id):
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "UPDATE customer_paper_bot_state SET lock_owner=?,lock_until_epoch=? "
            "WHERE id=1 AND lock_until_epoch<? RETURNING id",
            (worker_id, now + 180, now),
        ).fetchone()
        conn.commit()
        return row is not None
    except Exception:
        conn.rollback()
        raise


def _release_worker_lease(conn, worker_id):
    conn.execute(
        "UPDATE customer_paper_bot_state SET lock_owner=NULL,lock_until_epoch=0 WHERE id=1 AND lock_owner=?",
        (worker_id,),
    )
    conn.commit()


async def run_customer_paper_bot(db_path, database_url, stop_event):
    worker_id = secrets.token_urlsafe(16)
    log.info("Customer paper trader worker started; scan interval=%ss", INTERVAL_SECONDS)
    async with httpx.AsyncClient(timeout=10.0, headers={"Accept": "application/json"}) as client:
        while not stop_event.is_set():
            started = time.monotonic()
            conn = None
            owns_lease = False
            try:
                conn = connect(db_path, database_url)
                enabled = conn.execute(
                    "SELECT 1 FROM customer_paper_accounts WHERE enabled=1 AND auto_status='running' LIMIT 1"
                ).fetchone()
                conn.commit()
                if enabled:
                    if _claim_worker_lease(conn, worker_id):
                        owns_lease = True
                        signals = paper_bot._recent_wallet_buy_signals(conn)
                        held = [
                            row["asset_id"] for row in conn.execute(
                                "SELECT DISTINCT asset_id FROM customer_paper_positions"
                            ).fetchall()
                        ]
                        observed = paper_bot.scan_assets(conn, 100)
                        launches = await paper_bot._fetch_gecko_new_launches(client)
                        names = {
                            item["asset_id"]: item.get("token_name")
                            for item in (observed + launches) if item.get("asset_id")
                        }
                        discovered = list(dict.fromkeys(
                            held
                            + list(signals.keys())
                            + [item["asset_id"] for item in observed if item.get("asset_id")]
                            + [item["asset_id"] for item in launches if item.get("asset_id")]
                        ))[:180]
                        pairs = await paper_bot._fetch_pairs(client, discovered) if discovered else {}
                        summary = process_customer_accounts(conn, pairs, signals, names)
                        summary.update({
                            "discovered": len(discovered),
                            "wallet_candidates": len(signals),
                            "market_candidates": len(observed) + len(launches),
                        })
                        log.info("Customer paper scan: %s", summary)
                    else:
                        log.warning("Customer paper scan skipped: another worker holds the database lease")
                else:
                    log.debug("Customer paper trader idle: no enabled running customer accounts")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Customer paper trader scan failed")
            finally:
                if conn is not None:
                    if owns_lease:
                        try:
                            _release_worker_lease(conn, worker_id)
                        except Exception:
                            log.exception("Could not release customer paper worker lease")
                    conn.close()
            remaining = max(0.0, INTERVAL_SECONDS - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
