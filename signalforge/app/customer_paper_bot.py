"""Tenant-isolated automatic paper trading with wallet and market discovery."""
from __future__ import annotations

import asyncio
import logging
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import customer_data, paper_bot
from .db import connect, utcnow

log = logging.getLogger("signalforce.customer_paper_bot")
log.setLevel(logging.INFO)
RISK_MONITOR_INTERVAL_SECONDS = 5.0
STRATEGY_SCAN_INTERVAL_SECONDS = 30.0
MARKET_ONLY_MIN_SCORE = 50.0
BUNDLE_SELL_WINDOW_SECONDS = 60
WORKER_LEASE_SECONDS = 90
WORKER_LEASE_HEARTBEAT_SECONDS = 20
BUNDLE_SELL_MIN_WALLETS = 3
BUNDLE_SELL_MIN_USD = 250.0
LIQUIDITY_RUG_DROP_PCT = 35.0
SELL_PRESSURE_MIN_SELLS = 12
SELL_PRESSURE_RATIO = 2.0
MODE_RULES = {
    "gradual": {"position_fraction": 0.03, "max_position_fraction": 0.05, "max_positions": 4, "min_score": 55.0, "stop_loss": -8.0, "partial_at": 25.0, "partial_fraction": 0.50, "trailing_fraction": 0.12, "profit_lock": 0.05},
    "normal": {"position_fraction": 0.05, "max_position_fraction": 0.08, "max_positions": 7, "min_score": 45.0, "stop_loss": -10.0, "partial_at": 35.0, "partial_fraction": 0.40, "trailing_fraction": 0.15, "profit_lock": 0.10},
    "aggressive": {"position_fraction": 0.08, "max_position_fraction": 0.12, "max_positions": 10, "min_score": 35.0, "stop_loss": -15.0, "partial_at": 50.0, "partial_fraction": 0.40, "trailing_fraction": 0.20, "profit_lock": 0.20},
    "ultra_aggressive": {"position_fraction": 0.10, "max_position_fraction": 0.15, "max_positions": 8, "min_score": 40.0, "stop_loss": -15.0, "partial_at": 50.0, "partial_fraction": 0.40, "trailing_fraction": 0.20, "profit_lock": 0.20},
}


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else default
    except (TypeError, ValueError):
        return default


def _runner_assessment(pair, market_score, wallet_signal=None):
    """Score runner quality from market structure and tracked-wallet evidence."""
    wallet_signal = wallet_signal or {}
    liquidity = _number((pair.get("liquidity") or {}).get("usd"))
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    changes = pair.get("priceChange") or {}
    m5, h1 = txns.get("m5") or {}, txns.get("h1") or {}
    buys5, sells5 = _number(m5.get("buys")), _number(m5.get("sells"))
    vol5, vol1 = _number(volume.get("m5")), _number(volume.get("h1"))
    change5, change1 = _number(changes.get("m5")), _number(changes.get("h1"))
    market_cap = _number(pair.get("marketCap") or pair.get("fdv"))
    wallet_count = int(wallet_signal.get("wallet_count") or 0)
    wallet_buys = int(wallet_signal.get("buy_count") or 0)
    wallet_usd = _number(wallet_signal.get("buy_usd"))

    score = float(market_score) * 0.72
    reasons = ["market structure %.0f/100" % market_score]
    buy_ratio = buys5 / max(1.0, buys5 + sells5)
    if buy_ratio >= 0.75:
        score += 5
        reasons.append("strong 5m buy dominance")
    elif buy_ratio >= 0.62:
        score += 2
        reasons.append("positive 5m buy pressure")
    elif buy_ratio < 0.52:
        score -= 8
        reasons.append("weak 5m buy pressure")

    acceleration = (vol5 * 12.0 / vol1) if vol1 > 0 else 0.0
    if acceleration >= 2.0:
        score += 8
        reasons.append("accelerating volume")
    elif acceleration >= 1.25:
        score += 4
        reasons.append("rising volume")
    elif acceleration < 0.35:
        score -= 5
        reasons.append("volume fading")

    if liquidity >= 100000:
        score += 6
        reasons.append("deep liquidity")
    elif liquidity >= 50000:
        score += 4
        reasons.append("healthy liquidity")
    elif liquidity >= 20000:
        score += 2
    if market_cap > 0 and liquidity > 0:
        cap_to_liquidity = market_cap / liquidity
        if cap_to_liquidity <= 10:
            score += 4
            reasons.append("market cap supported by liquidity")
        elif cap_to_liquidity > 100:
            score -= 8
            reasons.append("thin liquidity versus valuation")

    if 0 < change5 <= 12 and change1 > 0:
        score += 4
        reasons.append("constructive momentum")
    elif change5 > 20:
        score -= 8
        reasons.append("late/overheated short-term move")
    if change1 < 0:
        score -= 5
        reasons.append("negative hourly trend")

    score += min(wallet_count * 3.0, 12.0)
    if wallet_count:
        reasons.append("%s tracked wallet(s) accumulating" % wallet_count)
    if wallet_usd >= 10000:
        score += 4
        reasons.append("meaningful tracked-wallet buy flow")
    elif wallet_usd >= 1000:
        score += 2
    if wallet_buys >= 3 and wallet_count >= 2:
        score += 2
        reasons.append("repeat wallet accumulation")

    score = round(max(0.0, min(100.0, score)), 1)
    tier = ("exceptional" if score >= 90 else "strong" if score >= 80
            else "promising" if score >= 70 else "developing" if score >= 60 else "weak")
    return {"score": score, "tier": tier, "reasons": reasons,
            "wallet_count": wallet_count, "buy_ratio": round(buy_ratio, 3),
            "volume_acceleration": round(acceleration, 2)}


def _adaptive_position_amount(account, rules, runner_score, liquidity):
    """Scale exposure with evidence, bounded by cash, equity, and pool depth."""
    equity = _number(account.get("_equity_usd"), _number(account.get("cash_balance_usd")))
    multiplier = (0.65 if runner_score < 60 else 0.85 if runner_score < 70
                  else 1.0 if runner_score < 80 else 1.25 if runner_score < 90 else 1.5)
    target_fraction = min(float(rules["max_position_fraction"]),
                          float(rules["position_fraction"]) * multiplier)
    liquidity_cap = _number(liquidity) * 0.01 if _number(liquidity) > 0 else 0.0
    amount = min(_number(account.get("cash_balance_usd")), equity * target_fraction, liquidity_cap)
    return round(max(0.0, amount), 2), multiplier


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




def _sell_fraction(conn, account, pos, price, fraction, reason):
    fraction = max(0.0, min(1.0, float(fraction)))
    amount = float(pos["quantity"]) * price * fraction
    if amount > 0:
        return customer_data.place_paper_trade(
            conn, account["account_id"], account["id"], asset_id=pos["asset_id"],
            token_name=pos["token_name"], side="sell", amount_usd=amount,
            price_usd=price, reason=reason, automated=True,
        )
    return None


def _bundle_sell_cluster(conn, mint):
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=BUNDLE_SELL_WINDOW_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = conn.execute(
        """SELECT COUNT(DISTINCT wallet_address) AS wallets,
                  COALESCE(SUM(COALESCE(usd_amount,0)),0) AS usd
           FROM wallet_trade_events
           WHERE mint=? AND side='sell' AND occurred_at>=?""",
        (mint, cutoff),
    ).fetchone()
    return int(row["wallets"] or 0), float(row["usd"] or 0)


def monitor_customer_positions(conn, pairs):
    """Fast shared exit/rug monitor for every customer strategy mode."""
    summary = {"checked": 0, "risk_exits": 0, "stop_exits": 0,
               "take_profit_exits": 0, "partial_exits": 0,
               "bundle_sell_flags": 0, "stale_positions": 0}
    rows = conn.execute(
        """SELECT p.*, a.account_id, COALESCE(a.strategy_profile,a.strategy_mode) AS strategy_mode
           FROM customer_paper_positions p
           JOIN customer_paper_accounts a ON a.id=p.paper_account_id
           WHERE a.enabled=1 AND a.auto_status='running'
           ORDER BY p.updated_at"""
    ).fetchall()
    for raw in rows:
        pos = dict(raw)
        pair = pairs.get(pos["asset_id"])
        try:
            price = float((pair or {}).get("priceUsd"))
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            summary["stale_positions"] += 1
            continue

        rules = MODE_RULES.get(pos["strategy_mode"], MODE_RULES["normal"])
        liquidity_value = ((pair.get("liquidity") or {}).get("usd")) if pair else None
        try:
            liquidity = float(liquidity_value) if liquidity_value is not None else 0.0
        except (TypeError, ValueError):
            liquidity = 0.0
        entry_liquidity = float(pos.get("entry_liquidity_usd") or 0)
        if entry_liquidity <= 0 and liquidity > 0:
            entry_liquidity = liquidity
        peak = max(float(pos.get("peak_price") or 0), float(pos["average_entry_price"]), price)
        conn.execute(
            """UPDATE customer_paper_positions
               SET last_price=?,peak_price=?,entry_liquidity_usd=?,updated_at=?
               WHERE id=? AND paper_account_id=?""",
            (price, peak, entry_liquidity, utcnow(), pos["id"], pos["paper_account_id"]),
        )
        summary["checked"] += 1

        change = (price / float(pos["average_entry_price"]) - 1) * 100
        price_change = pair.get("priceChange") or {}
        txns = pair.get("txns") or {}
        m5 = txns.get("m5") or {}
        try:
            buys5, sells5 = int(m5.get("buys")), int(m5.get("sells"))
            change5 = float(price_change.get("m5"))
        except (TypeError, ValueError):
            buys5 = sells5 = None
            change5 = None

        cluster_wallets, cluster_usd = _bundle_sell_cluster(conn, pos["asset_id"])
        danger_reason = None
        if cluster_wallets >= BUNDLE_SELL_MIN_WALLETS and cluster_usd >= BUNDLE_SELL_MIN_USD:
            danger_reason = "Risk exit: %s tracked wallets sold within %ss ($%.0f observed)" % (
                cluster_wallets, BUNDLE_SELL_WINDOW_SECONDS, cluster_usd
            )
            summary["bundle_sell_flags"] += 1
        elif entry_liquidity > 0 and liquidity > 0:
            liquidity_drop = (1 - liquidity / entry_liquidity) * 100
            if liquidity_drop >= LIQUIDITY_RUG_DROP_PCT:
                danger_reason = "Risk exit: liquidity dropped %.0f%% from entry" % liquidity_drop
        if (danger_reason is None and buys5 is not None and sells5 is not None
                and change5 is not None and sells5 >= max(SELL_PRESSURE_MIN_SELLS, buys5 * SELL_PRESSURE_RATIO)
                and change5 <= -8.0):
            danger_reason = "Risk exit: heavy sell pressure (%s sells vs %s buys in 5m)" % (sells5, buys5)

        account = {"id": pos["paper_account_id"], "account_id": pos["account_id"]}
        if danger_reason:
            _sell(conn, account, pos, price, danger_reason[:195])
            summary["risk_exits"] += 1
            continue
        if change <= rules["stop_loss"]:
            _sell(conn, account, pos, price, "Automatic %s stop-loss" % pos["strategy_mode"])
            summary["stop_exits"] += 1
            continue

        if not bool(pos.get("partial_profit_taken")) and change + 1e-9 >= rules["partial_at"]:
            fraction = rules["partial_fraction"]
            _sell_fraction(conn, account, pos, price, fraction,
                           "%s: banked %.0f%% at +%.1f%%; trailing remainder" % (
                               pos["strategy_mode"].replace("_", " ").title(), fraction * 100, change))
            conn.execute(
                "UPDATE customer_paper_positions SET partial_profit_taken=1 WHERE id=?",
                (pos["id"],),
            )
            summary["partial_exits"] += 1
            continue
        if bool(pos.get("partial_profit_taken")):
            trail_price = max(
                float(pos["average_entry_price"]) * (1 + rules["profit_lock"]),
                peak * (1 - rules["trailing_fraction"]),
            )
            if price <= trail_price:
                _sell(conn, account, pos, price,
                      "%s trailing exit: protected-profit stop triggered" %
                      pos["strategy_mode"].replace("_", " ").title())
                summary["take_profit_exits"] += 1

    conn.commit()
    return summary

def _recent_fomo_buy_signals(conn, lookback_minutes=10):
    """Return fresh, Solana-address FOMO buy observations for opted-in customers.

    FOMO activity is discovery context only. The normal independent market
    quality checks still decide whether any simulated entry is allowed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    rows = conn.execute(
        """SELECT s.account_id, s.payload_json
           FROM customer_fomo_snapshots s
           JOIN customer_paper_accounts a ON a.account_id=s.account_id
           WHERE s.snapshot_type='spot_swaps' AND a.enabled=1
             AND a.auto_status='running' AND COALESCE(a.fomo_signals_enabled,0)=1"""
    ).fetchall()
    result = {}
    address_pattern = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
    excluded = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                "Es9vMFrzaCERmJfrF4H2FYD4KCoNKL7v2R7d4QfY3g6"}
    for raw in rows:
        account_id = raw["account_id"]
        try:
            swaps = (json.loads(raw["payload_json"]) or {}).get("swaps", [])
        except (TypeError, ValueError):
            continue
        for swap in swaps if isinstance(swaps, list) else []:
            if not isinstance(swap, dict) or swap.get("isOffPlatform") or swap.get("isCrossmint"):
                continue
            display = swap.get("display") or {}
            side = display.get("side") if isinstance(display, dict) else display
            if str(side or "").lower() != "buy":
                continue
            created = swap.get("createdAt")
            try:
                stamp = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    continue
                age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError):
                continue
            if age < -60 or age > lookback_minutes * 60:
                continue
            mint = swap.get("outTokenAddress")
            if not isinstance(mint, str) or mint in excluded or not address_pattern.fullmatch(mint):
                continue
            result.setdefault(account_id, {})[mint] = {
                "created_at": stamp.astimezone(timezone.utc).isoformat(),
                "swap_id": str(swap.get("id") or "")[:128],
            }
    return result


def process_customer_accounts(conn, pairs, wallet_signals, asset_names=None, fomo_signals_by_account=None):
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
        strategy_mode = account.get("strategy_profile") or account["strategy_mode"]
        rules = MODE_RULES.get(strategy_mode, MODE_RULES["normal"])
        positions = conn.execute(
            "SELECT * FROM customer_paper_positions WHERE paper_account_id=?", (account["id"],)
        ).fetchall()

        # The five-second monitor is primary; retain strategy-cycle exits as a
        # fallback if a risk-monitor request was delayed or temporarily skipped.
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
            change = (price / float(pos["average_entry_price"]) - 1) * 100
            if change <= rules["stop_loss"]:
                _sell(conn, account, pos, price, "Automatic " + strategy_mode + " stop-loss")
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
                summary["sells"] += 1
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
                fomo_signal = (fomo_signals_by_account or {}).get(account["account_id"], {}).get(mint)
                wallet_count = int((signal or {}).get("wallet_count") or 0)
                required_score = rules["min_score"] if wallet_count else max(
                    rules["min_score"], MARKET_ONLY_MIN_SCORE
                )
                if score < required_score:
                    reason = "Market score below %s threshold" % (
                        "wallet-confirmed" if wallet_count else "market-only"
                    )
                    summary["rejection_reasons"][reason] = summary["rejection_reasons"].get(reason, 0) + 1
                    continue
                assessment = _runner_assessment(pair, score, signal)
                runner_floor = 55.0 if wallet_count else 60.0
                if assessment["score"] < runner_floor:
                    reason = "Runner quality below %.0f threshold" % runner_floor
                    summary["rejection_reasons"][reason] = summary["rejection_reasons"].get(reason, 0) + 1
                    continue
                # FOMO is a small ranking hint only; it cannot bypass market filters or score floors.
                candidate_rank = assessment["score"] + (2.0 if fomo_signal else 0.0)
                candidates.append((candidate_rank, mint, pair, score, wallet_count, assessment, fomo_signal))

            candidates.sort(reverse=True, key=lambda item: item[0])
            if candidates:
                _, mint, pair, score, wallet_count, assessment, fomo_signal = candidates[0]
                runner_score = assessment["score"]
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
                sizing_account = dict(account)
                sizing_account["_equity_usd"] = float(account["cash_balance_usd"]) + positions_value
                liquidity = _number(((pair.get("liquidity") or {}).get("usd")))
                amount, size_multiplier = _adaptive_position_amount(
                    sizing_account, rules, runner_score, liquidity
                )
                if price > 0 and amount >= 1:
                    base = pair.get("baseToken") or {}
                    token_name = names.get(mint) or base.get("name") or base.get("symbol") or mint[:8]
                    source = ("tracked-wallet confirmed" if wallet_count else
                              "FOMO Copilot buy + independent market confirmation" if fomo_signal else
                              "independent market momentum")
                    reason_detail = "; ".join(assessment["reasons"][:3])
                    if fomo_signal:
                        reason_detail += "; recent FOMO buy observation (discovery hint only)"
                    customer_data.place_paper_trade(
                        conn, account["account_id"], account["id"], asset_id=mint,
                        token_name=token_name, side="buy", amount_usd=amount, price_usd=price,
                        reason="Automated %s entry · runner %.1f/100 (%s) · market %.1f · size %.2fx · %s · %s" % (
                            source, runner_score, assessment["tier"], score, size_multiplier,
                            reason_detail, strategy_mode.replace("_", " ").title(),
                        )[:200],
                        automated=True,
                    )
                    conn.execute(
                        "UPDATE customer_paper_positions SET last_price=?,peak_price=?,entry_liquidity_usd=? WHERE paper_account_id=? AND asset_id=?",
                        (price, price, liquidity, account["id"], mint),
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
            (worker_id, now + WORKER_LEASE_SECONDS, now),
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


def _renew_worker_lease(conn, worker_id):
    """Extend the lease only while this worker still owns it."""
    row = conn.execute(
        "UPDATE customer_paper_bot_state SET lock_until_epoch=? "
        "WHERE id=1 AND lock_owner=? RETURNING id",
        (int(time.time()) + WORKER_LEASE_SECONDS, worker_id),
    ).fetchone()
    conn.commit()
    return row is not None


async def _keep_worker_lease_alive(db_path, database_url, worker_id, stop_event):
    """Renew during slow provider calls so another replica cannot steal a live job."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=WORKER_LEASE_HEARTBEAT_SECONDS
            )
            return
        except asyncio.TimeoutError:
            pass
        conn = None
        try:
            conn = connect(db_path, database_url)
            if not _renew_worker_lease(conn, worker_id):
                log.error("Customer paper worker lost its database lease; stopping heartbeat")
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Customer paper worker lease heartbeat failed")
        finally:
            if conn is not None:
                conn.close()


async def run_customer_paper_bot(db_path, database_url, stop_event):
    worker_id = secrets.token_urlsafe(16)
    last_strategy_scan = 0.0
    log.warning(
        "Customer paper trader started; shared risk monitor=%ss; strategy scan=%ss",
        RISK_MONITOR_INTERVAL_SECONDS, STRATEGY_SCAN_INTERVAL_SECONDS,
    )
    async with httpx.AsyncClient(timeout=8.0, headers={"Accept": "application/json"}) as client:
        while not stop_event.is_set():
            started = time.monotonic()
            conn = None
            owns_lease = False
            lease_heartbeat = None
            try:
                conn = connect(db_path, database_url)
                enabled = conn.execute(
                    "SELECT 1 FROM customer_paper_accounts WHERE enabled=1 AND auto_status='running' LIMIT 1"
                ).fetchone()
                conn.commit()
                if enabled and _claim_worker_lease(conn, worker_id):
                    owns_lease = True
                    lease_heartbeat = asyncio.create_task(
                        _keep_worker_lease_alive(db_path, database_url, worker_id, stop_event)
                    )
                    log.info("Customer paper worker acquired database lease")
                    held_rows = conn.execute(
                        """SELECT DISTINCT p.asset_id
                           FROM customer_paper_positions p
                           JOIN customer_paper_accounts a ON a.id=p.paper_account_id
                           WHERE a.enabled=1 AND a.auto_status='running'"""
                    ).fetchall()
                    held = [row["asset_id"] for row in held_rows]
                    held_pairs = await paper_bot._fetch_pairs(client, held) if held else {}
                    risk_summary = monitor_customer_positions(conn, held_pairs)
                    now_mono = time.monotonic()
                    if now_mono - last_strategy_scan >= STRATEGY_SCAN_INTERVAL_SECONDS:
                        signals = paper_bot._recent_wallet_buy_signals(conn)
                        observed = paper_bot.scan_assets(conn, 100)
                        launches = await paper_bot._fetch_gecko_new_launches(client)
                        names = {
                            item["asset_id"]: item.get("token_name")
                            for item in (observed + launches) if item.get("asset_id")
                        }
                        discovered = list(dict.fromkeys(
                            held + list(signals.keys())
                            + [item["asset_id"] for item in observed if item.get("asset_id")]
                            + [item["asset_id"] for item in launches if item.get("asset_id")]
                        ))[:180]
                        pairs = await paper_bot._fetch_pairs(client, discovered) if discovered else {}
                        fomo_signals = _recent_fomo_buy_signals(conn)
                        fomo_mints = [mint for account_signals in fomo_signals.values() for mint in account_signals]
                        discovered = list(dict.fromkeys(discovered + fomo_mints))[:180]
                        pairs = await paper_bot._fetch_pairs(client, discovered) if discovered else {}
                        summary = process_customer_accounts(conn, pairs, signals, names, fomo_signals)
                        summary.update({
                            "discovered": len(discovered),
                            "wallet_candidates": len(signals),
                            "market_candidates": len(observed) + len(launches),
                            "risk_monitor": risk_summary,
                        })
                        log.info("Customer paper strategy scan: %s", summary)
                        last_strategy_scan = now_mono
                    else:
                        log.info("Customer fast risk check: %s", risk_summary)
                elif enabled:
                    lease = conn.execute(
                        "SELECT lock_until_epoch FROM customer_paper_bot_state WHERE id=1"
                    ).fetchone()
                    remaining = int((lease or {}).get("lock_until_epoch") or 0) - int(time.time())
                    log.warning(
                        "Customer risk monitor skipped: another worker holds the database lease (expires in %ss)",
                        max(0, remaining),
                    )
                else:
                    log.debug("Customer paper trader idle: no enabled running customer accounts")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Customer paper trader risk/strategy cycle failed")
            finally:
                if lease_heartbeat is not None:
                    lease_heartbeat.cancel()
                    try:
                        await lease_heartbeat
                    except asyncio.CancelledError:
                        pass
                if conn is not None:
                    if owns_lease:
                        try:
                            _release_worker_lease(conn, worker_id)
                        except Exception:
                            log.exception("Could not release customer paper worker lease")
                    conn.close()
            remaining = max(0.0, RISK_MONITOR_INTERVAL_SECONDS - (time.monotonic() - started))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
