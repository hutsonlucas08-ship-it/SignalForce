"""Automatic paper-only scanner. No wallet, signing, or live execution."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
import os
import time
import uuid

import httpx

from . import paper
from .db import connect, utcnow
from .research import scan_assets

log = logging.getLogger("signalforge.paper_bot")
OWNER = str(uuid.uuid4())
_BUBBLE_CACHE = {}
INTERVAL_SECONDS = 3.0
LEASE_SECONDS = 12.0
MIN_LIQUIDITY_USD = 15000.0
MIN_VOLUME_5M_USD = 3000.0
MIN_BUYS_5M = 15
MIN_MARKET_CAP_USD = 25000.0
MAX_MARKET_CAP_USD = 5000000.0
STOP_LOSS_PCT = -10.0
TAKE_PROFIT_PCT = 20.0


def _state(conn):
    conn.execute("INSERT OR IGNORE INTO paper_bot_state(id,enabled,allocated_capital_usd) VALUES(1,0,1000)")
    conn.commit()
    state = dict(conn.execute("SELECT * FROM paper_bot_state WHERE id=1").fetchone())
    paper.ensure_accounts(conn)
    account = conn.execute("SELECT * FROM paper_accounts WHERE strategy='automatic'").fetchone()
    trades = conn.execute("SELECT COUNT(*) n FROM paper_trades WHERE account_id=?",(account["id"],)).fetchone()["n"]
    positions = conn.execute("SELECT COUNT(*) n FROM paper_positions WHERE account_id=?",(account["id"],)).fetchone()["n"]
    if not trades and not positions and account["starting_balance"] != state["allocated_capital_usd"]:
        now = utcnow()
        conn.execute("UPDATE paper_accounts SET starting_balance=?,cash_balance=?,updated_at=? WHERE id=?",
                     (state["allocated_capital_usd"],state["allocated_capital_usd"],now,account["id"]))
        conn.commit()
    elif trades or positions:
        state["allocated_capital_usd"] = account["starting_balance"]
        conn.execute("UPDATE paper_bot_state SET allocated_capital_usd=? WHERE id=1",
                     (account["starting_balance"],))
        conn.commit()
    return state


def _set_status(conn, **values):
    allowed = {"heartbeat_at", "last_scan_at", "last_error", "started_at", "run_started_at", "run_until", "session_seconds"}
    fields = [(k, v) for k, v in values.items() if k in allowed]
    if fields:
        conn.execute("UPDATE paper_bot_state SET " + ",".join(k+"=?" for k,_ in fields) + " WHERE id=1",
                     tuple(v for _,v in fields))
        conn.commit()


def _claim(conn):
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT lock_owner,lock_until_epoch FROM paper_bot_state WHERE id=1").fetchone()
    if row and row["lock_owner"] not in (None, OWNER) and row["lock_until_epoch"] > now:
        conn.rollback()
        return False
    conn.execute("UPDATE paper_bot_state SET lock_owner=?,lock_until_epoch=? WHERE id=1",
                 (OWNER, now + LEASE_SECONDS))
    conn.commit()
    return True


def _release(conn):
    conn.execute("UPDATE paper_bot_state SET lock_owner=NULL,lock_until_epoch=0 WHERE id=1 AND lock_owner=?",(OWNER,))
    conn.commit()


def _score(pair):
    """Return an explainable score and rejection reason; missing metrics fail closed."""
    liquidity = (pair.get("liquidity") or {}).get("usd")
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    changes = pair.get("priceChange") or {}
    m5 = txns.get("m5") or {}
    h1 = txns.get("h1") or {}
    buys5, sells5 = m5.get("buys"), m5.get("sells")
    buys1, sells1 = h1.get("buys"), h1.get("sells")
    vol5, vol1 = volume.get("m5"), volume.get("h1")
    change5, change1 = changes.get("m5"), changes.get("h1")
    market_cap = pair.get("marketCap") or pair.get("fdv")
    try:
        price = float(pair.get("priceUsd"))
    except (TypeError, ValueError):
        price = 0.0
    checks = [
        (price > 0, "No current price"),
        (isinstance(liquidity, (int,float)) and liquidity >= MIN_LIQUIDITY_USD, "Liquidity below $15,000 or missing"),
        (isinstance(market_cap, (int,float)) and MIN_MARKET_CAP_USD <= market_cap <= MAX_MARKET_CAP_USD, "Outside configured market-cap range"),
        (isinstance(vol5, (int,float)) and vol5 >= MIN_VOLUME_5M_USD, "Five-minute volume too low or missing"),
        (isinstance(buys5, int) and buys5 >= MIN_BUYS_5M, "Not enough recent buy transactions"),
        (isinstance(sells5, int) and sells5 > 0, "Sell-side activity missing"),
        (isinstance(buys1, int) and isinstance(sells1, int) and buys1 > sells1, "One-hour buy pressure is not positive"),
        (isinstance(vol1, (int,float)) and vol1 >= 10000, "One-hour volume too low or missing"),
        (isinstance(change5, (int,float)) and 0.5 <= change5 <= 20, "Five-minute momentum is weak or overheated"),
        (isinstance(change1, (int,float)) and change1 > 0, "One-hour momentum is not positive"),
    ]
    for passed, reason in checks:
        if not passed:
            return None, reason
    ratio5 = buys5 / max(sells5, 1)
    ratio1 = buys1 / max(sells1, 1)
    score = min(35, ratio5 * 10) + min(25, ratio1 * 8) + min(20, float(change5) * 2) + min(20, float(change1))
    return round(score, 2), None


async def _pumpfun_discoveries(client):
    """Fetch trending and newest callout mints when an approved Pump.fun key is configured."""
    key = os.getenv("PUMPFUN_API_KEY")
    if not key:
        return [], {}
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    requests = [
        client.get("https://api.pump.fun/v1/home-feed",
                   params={"pageSize": 50, "chain": "solana", "minMarketCapUsd": 10000,
                           "maxMarketCapUsd": 5000000, "assetClasses": "coin"},
                   headers=headers),
        client.get("https://api.pump.fun/v1/home-feed/new",
                   params={"pageSize": 50, "chain": "solana", "updatesLimit": 2},
                   headers=headers),
    ]
    responses = await asyncio.gather(*requests, return_exceptions=True)
    names = {}
    mints = []
    for response in responses:
        if isinstance(response, Exception):
            log.warning("Pump.fun discovery request failed: %s", type(response).__name__)
            continue
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            log.warning("Pump.fun discovery response unavailable")
            continue
        for coin in (payload.get("coins") or []) if isinstance(payload, dict) else []:
            if not isinstance(coin, dict):
                continue
            mint = coin.get("coinMint")
            if not isinstance(mint, str) or not mint:
                continue
            if coin.get("chain") not in (None, "solana"):
                continue
            if mint not in mints:
                mints.append(mint)
            names[mint] = coin.get("coinName") or coin.get("symbol") or mint[:8]
    return mints, names


async def _fetch_pairs(client, mints):
    async def one(batch):
        try:
            r = await client.get("https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch))
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except (httpx.HTTPError, ValueError):
            log.warning("Paper bot market-data request failed")
            return []
    batches = [mints[i:i+30] for i in range(0, len(mints), 30)]
    groups = await asyncio.gather(*(one(b) for b in batches))
    out = {}
    for group in groups:
        for pair in group:
            if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                continue
            mint = (pair.get("baseToken") or {}).get("address")
            if mint not in mints:
                continue
            old = out.get(mint)
            liq = ((pair.get("liquidity") or {}).get("usd") or 0)
            old_liq = (((old or {}).get("liquidity") or {}).get("usd") or 0)
            if old is None or liq > old_liq:
                out[mint] = pair
    return out


def _trade_decisions(conn, assets, pairs, extra_names=None, approved_mints=None):
    paper.ensure_accounts(conn)
    account = conn.execute("SELECT * FROM paper_accounts WHERE strategy='automatic'").fetchone()
    open_rows = conn.execute("SELECT * FROM paper_positions WHERE account_id=?",(account["id"],)).fetchall()
    positions = {r["asset_id"]: r for r in open_rows}
    prices = {}
    for mint, pair in pairs.items():
        try:
            price = float(pair.get("priceUsd"))
            if price > 0:
                prices[mint] = price
        except (TypeError, ValueError):
            continue
    paper.mark_positions(conn, prices)

    # Exit management runs before any new entries.
    exits = []
    for mint, pos in list(positions.items()):
        price = prices.get(mint)
        if not price:
            continue
        pnl_pct = (price / pos["avg_entry_price"] - 1) * 100
        if pnl_pct <= STOP_LOSS_PCT or pnl_pct >= TAKE_PROFIT_PCT:
            reason = "Automatic paper exit: stop loss" if pnl_pct <= STOP_LOSS_PCT else "Automatic paper exit: take profit"
            try:
                sold = paper.place_order(conn, "automatic", mint, pos["token_name"], "sell",
                                  pos["quantity"] * price, price, reason)
                exits.append({"asset_id": mint, "token_name": pos["token_name"], "realized_pnl_usd": sold["realized_pnl_usd"], "reference_price": price})
            except ValueError as exc:
                log.warning("Paper exit blocked for %s: %s", mint[:8], exc)

    # Never chase tokens without recent buyers, volume, liquidity and positive momentum.
    candidates = []
    names = {a["asset_id"]: a.get("token_name") for a in assets}
    names.update(extra_names or {})
    for mint, pair in pairs.items():
        if mint in positions or (approved_mints is not None and mint not in approved_mints):
            continue
        score, reason = _score(pair)
        if score is not None:
            candidates.append((score, mint, pair))
    candidates.sort(reverse=True, key=lambda x: x[0])

    account = conn.execute("SELECT * FROM paper_accounts WHERE strategy='automatic'").fetchone()
    equity = account["cash_balance"] + conn.execute(
        "SELECT COALESCE(SUM(last_price*quantity),0) v FROM paper_positions WHERE account_id=?",
        (account["id"],)).fetchone()["v"]
    count = conn.execute("SELECT COUNT(*) n FROM paper_positions WHERE account_id=?",(account["id"],)).fetchone()["n"]
    per_trade = min(equity * 0.05, account["cash_balance"])
    entries = []
    for score, mint, pair in candidates:
        if count >= 10 or per_trade < 10:
            break
        price = prices.get(mint)
        if not price:
            continue
        # Use the best eligible setup; don't force a trade just to appear active.
        try:
            result = paper.place_order(conn, "automatic", mint, names.get(mint) or (pair.get("baseToken") or {}).get("name"),
                                       "buy", per_trade, price,
                                       "Automatic momentum entry; score %.1f; 5m buy/sell and liquidity filters passed" % score)
            entries.append({"asset_id": mint, "score": score, "trade_id": result["trade_id"]})
            count += 1
            break
        except ValueError as exc:
            log.info("Candidate skipped %s: %s", mint[:8], exc)
    return {"prices": prices, "candidate_count": len(candidates), "entries": entries, "exits": exits}


async def _bubble_approved(client, mint):
    """Fail closed unless BubbleMaps reports strong launch-health evidence."""
    now = time.time()
    cached = _BUBBLE_CACHE.get(mint)
    if cached and now - cached[0] < 300:
        return cached[1]
    approved = False
    try:
        response = await client.get("https://bubblemaps.dev/api/map/" + mint)
        response.raise_for_status()
        payload = response.json()
        launch = ((payload.get("map") or {}).get("launch") or {})
        score = launch.get("score")
        if launch.get("status") == "ok" and isinstance(score, (int,float)) and score >= 70:
            limits = {"sniperShare":0.35, "bundleShare":0.25, "devClusterShare":0.25, "top10Share":0.75}
            approved = all(not isinstance(launch.get(field),(int,float)) or launch[field] <= ceiling
                           for field,ceiling in limits.items())
    except (httpx.HTTPError, ValueError, TypeError):
        approved = False
    _BUBBLE_CACHE[mint] = (now, approved)
    return approved


async def _approved_candidates(client, pairs):
    ranked = []
    for mint, pair in pairs.items():
        score, reason = _score(pair)
        if score is not None:
            ranked.append((score, mint))
    ranked.sort(reverse=True)
    # Check only the three strongest activity-qualified candidates per scan;
    # results are cached for five minutes to respect provider limits.
    checks = ranked[:3]
    results = await asyncio.gather(*(_bubble_approved(client, mint) for _,mint in checks))
    return {mint for (_,mint), ok in zip(checks, results) if ok}


async def _telegram_send(client, text):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        response = await client.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            log.warning("Telegram paper alert was not accepted")
    except (httpx.HTTPError, ValueError):
        log.warning("Telegram paper alert delivery failed")


async def run_paper_bot(db_path: str, stop_event: asyncio.Event):
    """Run while the API process is alive; SQLite lease prevents duplicate workers."""
    conn = connect(db_path)
    try:
        _state(conn)
        _set_status(conn, started_at=utcnow())
        async with httpx.AsyncClient(timeout=3.5, headers={"Accept":"application/json"}) as client:
            while not stop_event.is_set():
                cycle_started = time.monotonic()
                try:
                    state = _state(conn)
                    _set_status(conn, heartbeat_at=utcnow())
                    if state.get("run_until") and state["enabled"] and datetime.fromisoformat(state["run_until"].replace("Z","+00:00")) <= datetime.now(timezone.utc):
                        conn.execute("UPDATE paper_bot_state SET enabled=0 WHERE id=1")
                        conn.commit()
                        state = _state(conn)
                    if not state["enabled"] or not _claim(conn):
                        await asyncio.sleep(INTERVAL_SECONDS)
                        continue
                    try:
                        assets = scan_assets(conn, 50)
                        mints = [a["asset_id"] for a in assets if a.get("asset_id")]
                        pump_mints, pump_names = await _pumpfun_discoveries(client)
                        for mint in pump_mints:
                            if mint not in mints:
                                mints.append(mint)
                        pairs = await _fetch_pairs(client, mints) if mints else {}
                        approved_mints = await _approved_candidates(client, pairs)
                        outcome = _trade_decisions(conn, assets, pairs, pump_names, approved_mints)
                        for trade in outcome.get("entries", []):
                            pair = pairs.get(trade["asset_id"], {})
                            token = (pair.get("baseToken") or {}).get("name") or pump_names.get(trade["asset_id"]) or trade["asset_id"][:8]
                            await _telegram_send(client, "SIGNALFORGE PAPER BUY\n" + token + "\nEntry: $" + str(pair.get("priceUsd") or "n/a") + "\nScore: " + str(trade["score"]) + "/100\nNo real funds moved.")
                        for trade in outcome.get("exits", []):
                            await _telegram_send(client, "SIGNALFORGE PAPER SELL\n" + str(trade.get("token_name") or trade["asset_id"][:8]) + "\nExit reference: $" + str(trade["reference_price"]) + "\nRealized paper P&L: $" + str(round(trade["realized_pnl_usd"],2)) + "\nNo real funds moved.")
                        _set_status(conn, last_scan_at=utcnow(), heartbeat_at=utcnow(), last_error=None)
                        log.info("Paper bot scan: assets=%d priced=%d eligible=%d entries=%d",
                                 len(assets), len(pairs), outcome["candidate_count"], len(outcome["entries"]))
                    finally:
                        _release(conn)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("Paper bot scan failed")
                    _set_status(conn, heartbeat_at=utcnow(), last_error=str(exc)[:300])
                remaining = max(0.0, INTERVAL_SECONDS - (time.monotonic() - cycle_started))
                if remaining:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        pass
    finally:
        _release(conn)
        conn.close()


def bot_status(conn):
    state = _state(conn)
    age = None
    if state.get("heartbeat_at"):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["heartbeat_at"].replace("Z","+00:00"))).total_seconds()
        except ValueError:
            pass
    return {
        "enabled": bool(state["enabled"]),
        "status": "ACTIVE" if state["enabled"] and age is not None and age < 12 else ("STARTING" if state["enabled"] else "PAUSED"),
        "allocated_capital_usd": state["allocated_capital_usd"],
        "started_at": state.get("run_started_at"),
        "run_until": state.get("run_until"),
        "session_seconds": state.get("session_seconds"),
        "remaining_seconds": max(0, int((datetime.fromisoformat(state["run_until"].replace("Z","+00:00"))-datetime.now(timezone.utc)).total_seconds())) if state.get("run_until") and state["enabled"] else 0,
        "heartbeat_at": state["heartbeat_at"],
        "last_scan_at": state["last_scan_at"],
        "heartbeat_age_seconds": round(age,1) if age is not None else None,
        "last_error": state["last_error"],
        "scan_interval_seconds": INTERVAL_SECONDS,
        "entry_rules": {"min_liquidity_usd":MIN_LIQUIDITY_USD,"min_volume_5m_usd":MIN_VOLUME_5M_USD,
                        "min_buys_5m":MIN_BUYS_5M,"min_bubblemaps_health":70,"stop_loss_pct":STOP_LOSS_PCT,"take_profit_pct":TAKE_PROFIT_PCT},
        "pumpfun_api_configured": bool(os.getenv("PUMPFUN_API_KEY")),
        "note": "Paper simulation only. Discovery combines PumpPortal launches with Pump.fun trending and newest callouts when an approved API key is configured. New entries require strong BubbleMaps launch health; missing evidence is rejected. The scanner is not the full market and may make no trades."
    }


def configure_bot(conn, enabled=None, allocated_capital_usd=None, duration_seconds=None):
    state = _state(conn)
    if allocated_capital_usd is not None:
        if not isinstance(allocated_capital_usd,(int,float)) or allocated_capital_usd < 100 or allocated_capital_usd > 100000:
            raise ValueError("Virtual capital must be between $100 and $100,000")
        account = conn.execute("SELECT * FROM paper_accounts WHERE strategy='automatic'").fetchone()
        trades = conn.execute("SELECT COUNT(*) n FROM paper_trades WHERE account_id=?",(account["id"],)).fetchone()["n"]
        positions = conn.execute("SELECT COUNT(*) n FROM paper_positions WHERE account_id=?",(account["id"],)).fetchone()["n"]
        if trades or positions:
            raise ValueError("Capital can only be changed before the automatic account has any trades or open positions")
        now=utcnow()
        conn.execute("UPDATE paper_accounts SET starting_balance=?,cash_balance=?,updated_at=? WHERE id=?",
                     (float(allocated_capital_usd),float(allocated_capital_usd),now,account["id"]))
        conn.execute("UPDATE paper_bot_state SET allocated_capital_usd=? WHERE id=1",(float(allocated_capital_usd),))
    if enabled is not None:
        if enabled:
            allowed_durations = {900, 1800, 3600, 7200, 14400}
            if duration_seconds not in allowed_durations:
                raise ValueError("Choose a run duration of 15 minutes, 30 minutes, 1 hour, 2 hours, or 4 hours")
            started = datetime.now(timezone.utc)
            ends = started + timedelta(seconds=duration_seconds)
            conn.execute("UPDATE paper_bot_state SET enabled=1,run_started_at=?,run_until=?,session_seconds=?,last_error=NULL WHERE id=1",
                         (started.strftime("%Y-%m-%dT%H:%M:%SZ"),ends.strftime("%Y-%m-%dT%H:%M:%SZ"),duration_seconds))
        else:
            conn.execute("UPDATE paper_bot_state SET enabled=0,run_until=NULL WHERE id=1")
    conn.commit()
    return bot_status(conn)
