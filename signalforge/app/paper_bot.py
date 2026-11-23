"""Automatic paper-only scanner. No wallet, signing, or live execution."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
import os
import time
import uuid

import httpx

from . import exit_rules, paper, position_track
from .db import connect, utcnow
from .research import scan_assets

log = logging.getLogger("signalforge.paper_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
OWNER = str(uuid.uuid4())
_BUBBLE_CACHE = {}
_STALE_ALERTED = {}
INTERVAL_SECONDS = 30.0
SMART_MONEY_LOOKBACK_MINUTES = 15
MIN_DISTINCT_WALLET_BUYS = 1
SINGLE_WALLET_MIN_MARKET_SCORE = 35.0
MARKET_ONLY_MIN_SCORE = 50.0
MAX_OPEN_POSITIONS = 20
POSITION_EQUITY_FRACTION = 0.10
LEASE_SECONDS = 90.0
MIN_LIQUIDITY_USD = 5000.0
MIN_VOLUME_5M_USD = 500.0
MIN_BUYS_5M = 3
MIN_MARKET_CAP_USD = 5000.0
MAX_MARKET_CAP_USD = 20000000.0
STOP_LOSS_PCT = -10.0
STALE_POSITION_SECONDS = 300.0
EXIT_CONFIG = exit_rules.ExitConfig(stop_loss_pct=STOP_LOSS_PCT)


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
    allowed = {"heartbeat_at", "last_scan_at", "last_error", "started_at", "run_started_at", "run_until", "run_stopped_at", "session_seconds", "last_scan_summary"}
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


def _liquidity_usd(pair):
    value = ((pair or {}).get("liquidity") or {}).get("usd")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _held_mints(conn):
    paper.ensure_accounts(conn)
    account = conn.execute("SELECT id FROM paper_accounts WHERE strategy='automatic'").fetchone()
    rows = conn.execute("SELECT asset_id FROM paper_positions WHERE account_id=?", (account["id"],)).fetchall()
    return [row["asset_id"] for row in rows]


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
        (isinstance(liquidity, (int,float)) and liquidity >= MIN_LIQUIDITY_USD, "Liquidity below $5,000 or missing"),
        (isinstance(market_cap, (int,float)) and MIN_MARKET_CAP_USD <= market_cap <= MAX_MARKET_CAP_USD, "Outside configured market-cap range"),
        (isinstance(vol5, (int,float)) and vol5 >= MIN_VOLUME_5M_USD, "Five-minute volume too low or missing"),
        (isinstance(buys5, int) and buys5 >= MIN_BUYS_5M, "Not enough recent buy transactions"),
        (isinstance(sells5, int) and sells5 >= 0, "Five-minute transaction data missing"),
        (isinstance(buys1, int) and isinstance(sells1, int) and buys1 >= sells1 * 0.8, "One-hour buy pressure is too weak"),
        (isinstance(vol1, (int,float)) and vol1 >= 2000, "One-hour volume too low or missing"),
        (isinstance(change5, (int,float)) and -5 <= change5 <= 35, "Five-minute momentum is too weak or overheated"),
        (isinstance(change1, (int,float)) and change1 > -10, "One-hour momentum is collapsing"),
    ]
    for passed, reason in checks:
        if not passed:
            return None, reason
    ratio5 = buys5 / max(sells5, 1)
    ratio1 = buys1 / max(sells1, 1)
    score = min(35, ratio5 * 10) + min(25, ratio1 * 8) + min(20, float(change5) * 2) + min(20, float(change1))
    return round(score, 2), None


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


def _recent_wallet_buy_signals(conn, lookback_minutes=SMART_MONEY_LOOKBACK_MINUTES):
    """Aggregate recent, ingested buys from active tracked wallets only."""
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """
        SELECT e.mint, COUNT(*) AS buy_count,
               COUNT(DISTINCT e.wallet_address) AS wallet_count,
               MAX(e.occurred_at) AS latest_buy_at
        FROM wallet_trade_events e
        JOIN tracked_wallets w ON w.wallet_address=e.wallet_address
        WHERE e.side='buy' AND e.occurred_at>=?
          AND COALESCE(w.helius_status, 'active')='active'
        GROUP BY e.mint
        """,
        (cutoff,),
    ).fetchall()
    return {row["mint"]: dict(row) for row in rows}


def _trade_decisions(conn, assets, pairs, extra_names=None, approved_mints=None):
    paper.ensure_accounts(conn)
    session_id = _state(conn).get("run_session_id")
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

    # Exit management runs before any new entries. Peak prices and partial-profit state
    # persist across scans and process restarts.
    now_dt = datetime.now(timezone.utc)
    now_iso = utcnow()
    exits = []
    stale = []
    for mint, pos in list(positions.items()):
        price = prices.get(mint)
        pair = pairs.get(mint)
        track = position_track.observe(
            conn, mint, pos["avg_entry_price"], price, _liquidity_usd(pair), now_iso
        )
        if not price:
            since = exit_rules.parse_ts(track.get("unpriced_since"))
            if since and (now_dt - since).total_seconds() >= STALE_POSITION_SECONDS:
                stale.append({
                    "asset_id": mint,
                    "token_name": pos["token_name"],
                    "unpriced_seconds": int((now_dt - since).total_seconds()),
                })
            continue

        first_seen = exit_rules.parse_ts(track.get("first_seen_at"))
        decision = exit_rules.decide_exit(
            entry_price=pos["avg_entry_price"],
            price=price,
            peak_price=track["peak_price"],
            entry_liquidity_usd=track["entry_liquidity_usd"],
            liquidity_usd=_liquidity_usd(pair),
            held_seconds=(now_dt - first_seen).total_seconds() if first_seen else 0.0,
            partial_taken=bool(track["partial_taken"]),
            config=EXIT_CONFIG,
        )
        if decision.action == "hold":
            continue

        fraction = 1.0 if decision.action == "sell_all" else decision.fraction
        try:
            sold = paper.place_order(
                conn, "automatic", mint, pos["token_name"], "sell",
                pos["quantity"] * fraction * price, price,
                "Automatic paper exit: " + decision.reason, session_id=session_id,
            )
        except ValueError as exc:
            log.warning("Paper exit blocked for %s: %s", mint[:8], exc)
            continue

        if decision.action == "sell_all":
            position_track.forget(conn, mint)
        else:
            position_track.mark_partial(conn, mint)
        exits.append({
            "asset_id": mint,
            "token_name": pos["token_name"],
            "realized_pnl_usd": sold["realized_pnl_usd"],
            "reference_price": price,
            "reason": decision.reason,
            "partial": decision.action == "sell_partial",
        })

    # Combine recent tracked-wallet accumulation with independent market confirmation.
    candidates = []
    names = {a["asset_id"]: a.get("token_name") for a in assets}
    names.update(extra_names or {})
    wallet_signals = _recent_wallet_buy_signals(conn)
    for mint, pair in pairs.items():
        if mint in positions or (approved_mints is not None and mint not in approved_mints):
            continue
        market_score, reason = _score(pair)
        signal = wallet_signals.get(mint)
        if market_score is None:
            continue
        wallet_count = int(signal["wallet_count"]) if signal else 0
        # Aggressive paper mode: one tracked-wallet buy is confirmation; strong
        # momentum setups may qualify even without a tracked-wallet signal.
        if wallet_count == 0 and market_score < MARKET_ONLY_MIN_SCORE:
            continue
        if wallet_count > 0 and market_score < SINGLE_WALLET_MIN_MARKET_SCORE:
            continue
        combined_score = min(100.0, market_score + min(wallet_count * 10.0, 25.0))
        candidates.append((combined_score, mint, pair, signal or {"wallet_count": 0, "buy_count": 0, "latest_buy_at": None}, market_score))
    candidates.sort(reverse=True, key=lambda x: x[0])

    account = conn.execute("SELECT * FROM paper_accounts WHERE strategy='automatic'").fetchone()
    equity = account["cash_balance"] + conn.execute(
        "SELECT COALESCE(SUM(last_price*quantity),0) v FROM paper_positions WHERE account_id=?",
        (account["id"],)).fetchone()["v"]
    count = conn.execute("SELECT COUNT(*) n FROM paper_positions WHERE account_id=?",(account["id"],)).fetchone()["n"]
    per_trade = min(equity * POSITION_EQUITY_FRACTION, account["cash_balance"])
    entries = []
    for score, mint, pair, signal, market_score in candidates:
        if count >= MAX_OPEN_POSITIONS or per_trade < 10:
            break
        price = prices.get(mint)
        if not price:
            continue
        # Use the best eligible setup; don't force a trade just to appear active.
        try:
            result = paper.place_order(conn, "automatic", mint, names.get(mint) or (pair.get("baseToken") or {}).get("name"),
                                       "buy", per_trade, price,
                                       "Aggressive paper entry; score %.1f; %d tracked wallets bought in last %d minutes; market momentum filters passed" % (score, signal["wallet_count"], SMART_MONEY_LOOKBACK_MINUTES),
                                       session_id=session_id, max_position_fraction=POSITION_EQUITY_FRACTION, max_open_positions=MAX_OPEN_POSITIONS)
            position_track.forget(conn, mint)
            position_track.observe(conn, mint, result["fill_price"], price, _liquidity_usd(pair))
            entries.append({"asset_id": mint, "score": score, "market_score": market_score, "wallet_count": signal["wallet_count"], "buy_count": signal["buy_count"], "latest_buy_at": signal["latest_buy_at"], "trade_id": result["trade_id"]})
            count += 1
            break
        except ValueError as exc:
            log.info("Candidate skipped %s: %s", mint[:8], exc)
    return {"prices": prices, "candidate_count": len(candidates), "entries": entries, "exits": exits, "stale_positions": stale, "wallet_signal_count": len(wallet_signals)}


async def _bubble_approved(client, mint):
    """Fail closed unless BubbleMaps reports strong launch-health evidence."""
    now = time.time()
    cached = _BUBBLE_CACHE.get(mint)
    if cached and now - cached[0] < 300:
        return cached[1]
    approved = False
    why = "no launch evidence"
    try:
        response = await client.get("https://bubblemaps.dev/api/map/" + mint)
        response.raise_for_status()
        payload = response.json()
        launch = ((payload.get("map") or {}).get("launch") or {})
        score = launch.get("score")
        if launch.get("status") == "ok" and isinstance(score, (int,float)) and score >= 70:
            limits = {"sniperShare":0.35, "bundleShare":0.25, "devClusterShare":0.25, "top10Share":0.75}
            missing = [field for field in limits if not isinstance(launch.get(field), (int, float))]
            exceeded = [field for field, ceiling in limits.items()
                        if isinstance(launch.get(field), (int, float)) and launch[field] > ceiling]
            approved = not missing and not exceeded
            why = "passed" if approved else ("missing " + ",".join(missing) if missing else "exceeded " + ",".join(exceeded))
        else:
            why = "launch status/score did not qualify"
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        why = "request failed: " + type(exc).__name__
    _BUBBLE_CACHE[mint] = (now, approved)
    log.info("BubbleMaps check %s: approved=%s (%s)", mint[:8], approved, why)
    return approved


async def _approved_candidates(client, pairs, eligible_mints=None):
    ranked = []
    for mint, pair in pairs.items():
        if eligible_mints is not None and mint not in eligible_mints:
            continue
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
        async with httpx.AsyncClient(timeout=8.0, headers={"Accept":"application/json"}) as client:
            while not stop_event.is_set():
                cycle_started = time.monotonic()
                try:
                    state = _state(conn)
                    _set_status(conn, heartbeat_at=utcnow())
                    if state.get("run_until") and state["enabled"] and datetime.fromisoformat(state["run_until"].replace("Z","+00:00")) <= datetime.now(timezone.utc):
                        conn.execute("UPDATE paper_bot_state SET enabled=0,run_stopped_at=COALESCE(run_stopped_at,run_until) WHERE id=1")
                        conn.commit()
                        state = _state(conn)
                    if not state["enabled"] or not _claim(conn):
                        await asyncio.sleep(INTERVAL_SECONDS)
                        continue
                    try:
                        assets = scan_assets(conn, 100)
                        wallet_signals = _recent_wallet_buy_signals(conn)
                        # Always reserve room for open positions so they continue to be re-priced.
                        held = _held_mints(conn)
                        mints = list(dict.fromkeys(
                            held
                            + list(wallet_signals.keys())
                            + [a["asset_id"] for a in assets if a.get("asset_id")]
                        ))[:150]
                        pairs = await _fetch_pairs(client, mints) if mints else {}
                        # Aggressive paper mode uses market and momentum confirmation;
                        # BubbleMaps launch data remains a research signal, not a hard gate.
                        outcome = _trade_decisions(conn, assets, pairs)
                        for trade in outcome.get("entries", []):
                            pair = pairs.get(trade["asset_id"], {})
                            token = (pair.get("baseToken") or {}).get("name") or trade["asset_id"][:8]
                            await _telegram_send(client, "SIGNALFORGE PAPER BUY\n" + token + "\nEntry: $" + str(pair.get("priceUsd") or "n/a") + "\nScore: " + str(trade["score"]) + "/100\nNo real funds moved.")
                        for trade in outcome.get("exits", []):
                            label = "PAPER PARTIAL SELL" if trade.get("partial") else "PAPER SELL"
                            await _telegram_send(client, "SIGNALFORCE " + label + "\n" + str(trade.get("token_name") or trade["asset_id"][:8]) + "\nReason: " + str(trade.get("reason")) + "\nExit reference: $" + str(trade["reference_price"]) + "\nRealized paper P&L: $" + str(round(trade["realized_pnl_usd"],2)) + "\nNo real funds moved.")
                        stale = outcome.get("stale_positions", [])
                        for item in stale:
                            last = _STALE_ALERTED.get(item["asset_id"], 0)
                            if time.time() - last > 1800:
                                _STALE_ALERTED[item["asset_id"]] = time.time()
                                await _telegram_send(client, "SIGNALFORCE WARNING\nNo price for open paper position " + str(item.get("token_name") or item["asset_id"][:8]) + " for " + str(item["unpriced_seconds"] // 60) + "+ minutes. Its exit rules cannot run until a price returns.")
                        stale_note = ("No price for open position(s): " + ", ".join(str(item.get("token_name") or item["asset_id"][:8]) for item in stale))[:300] if stale else None
                        _set_status(conn, last_scan_at=utcnow(), heartbeat_at=utcnow(), last_error=stale_note, last_scan_summary="Assets %d · prices %d · eligible %d · tracked-wallet signals %d · buys %d · sells %d" % (len(assets), len(pairs), outcome["candidate_count"], outcome["wallet_signal_count"], len(outcome["entries"]), len(outcome["exits"])))
                        log.info("Paper bot scan: assets=%d priced=%d eligible=%d entries=%d exits=%d",
                                 len(assets), len(pairs), outcome["candidate_count"], len(outcome["entries"]), len(outcome["exits"]))
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
        "status": "ACTIVE" if state["enabled"] and age is not None and age < INTERVAL_SECONDS * 2 else ("STARTING" if state["enabled"] else "STOPPED"),
        "allocated_capital_usd": state["allocated_capital_usd"],
        "started_at": state.get("run_started_at"),
        "run_until": state.get("run_until"),
        "run_stopped_at": state.get("run_stopped_at"),
        "runtime_seconds": max(0, int(((datetime.fromisoformat((state.get("run_stopped_at") or utcnow()).replace("Z","+00:00"))) - datetime.fromisoformat(state["run_started_at"].replace("Z","+00:00"))).total_seconds())) if state.get("run_started_at") else 0,
        "session_seconds": state.get("session_seconds"),
        "remaining_seconds": max(0, int((datetime.fromisoformat(state["run_until"].replace("Z","+00:00"))-datetime.now(timezone.utc)).total_seconds())) if state.get("run_until") and state["enabled"] else 0,
        "heartbeat_at": state["heartbeat_at"],
        "last_scan_at": state["last_scan_at"],
        "heartbeat_age_seconds": round(age,1) if age is not None else None,
        "last_error": state["last_error"],
        "last_scan_summary": state.get("last_scan_summary"),
        "scan_interval_seconds": INTERVAL_SECONDS,
        "entry_rules": {"min_liquidity_usd":MIN_LIQUIDITY_USD,"min_volume_5m_usd":MIN_VOLUME_5M_USD,
                        "min_buys_5m":MIN_BUYS_5M,"smart_money_lookback_minutes":SMART_MONEY_LOOKBACK_MINUTES,"minimum_distinct_wallet_buys":MIN_DISTINCT_WALLET_BUYS,"market_only_min_score":MARKET_ONLY_MIN_SCORE,"single_wallet_min_score":SINGLE_WALLET_MIN_MARKET_SCORE,"max_open_positions":MAX_OPEN_POSITIONS,"position_equity_fraction":POSITION_EQUITY_FRACTION,"stop_loss_pct":STOP_LOSS_PCT,"take_profit_pct":None},
        "exit_rules": exit_rules.describe(EXIT_CONFIG),
        "strategy": "aggressive_smart_money_or_momentum",
        "smart_money_lookback_minutes": SMART_MONEY_LOOKBACK_MINUTES,
        "minimum_distinct_wallet_buys": MIN_DISTINCT_WALLET_BUYS,
        "single_wallet_market_score_threshold": SINGLE_WALLET_MIN_MARKET_SCORE,
        "market_only_min_score": MARKET_ONLY_MIN_SCORE,
        "max_open_positions": MAX_OPEN_POSITIONS,
        "position_equity_fraction": POSITION_EQUITY_FRACTION,
        "note": "AGGRESSIVE PAPER SIMULATION ONLY. Entries can qualify from one recent tracked-wallet buy OR strong independent market momentum. BubbleMaps is not a hard entry gate. Lower liquidity/volume thresholds, 10% equity position sizing, up to 20 open positions, and 30-second scans increase simulated trade frequency and risk. Existing exit protections remain active. No real funds or signatures are used."
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
            # Each timed run starts with a clean automatic paper portfolio.
            # Historical paper_trades remain untouched and are still auditable.
            account = conn.execute("SELECT * FROM paper_accounts WHERE strategy='automatic'").fetchone()
            old_assets = [row["asset_id"] for row in conn.execute(
                "SELECT asset_id FROM paper_positions WHERE account_id=?", (account["id"],)
            ).fetchall()]
            if old_assets:
                placeholders = ",".join("?" for _ in old_assets)
                conn.execute("DELETE FROM position_tracking WHERE asset_id IN (" + placeholders + ")", tuple(old_assets))
            conn.execute("DELETE FROM paper_positions WHERE account_id=?", (account["id"],))
            conn.execute("UPDATE paper_accounts SET cash_balance=starting_balance,updated_at=? WHERE id=?",
                         (utcnow(), account["id"]))
            started = datetime.now(timezone.utc)
            ends = started + timedelta(seconds=duration_seconds)
            conn.execute("UPDATE paper_bot_state SET enabled=1,run_started_at=?,run_until=?,run_stopped_at=NULL,run_session_id=?,session_seconds=?,last_error=NULL,last_scan_summary=NULL WHERE id=1",
                         (started.strftime("%Y-%m-%dT%H:%M:%SZ"),ends.strftime("%Y-%m-%dT%H:%M:%SZ"),uuid.uuid4().hex,duration_seconds))
        else:
            conn.execute("UPDATE paper_bot_state SET enabled=0,run_until=NULL,run_stopped_at=? WHERE id=1",(utcnow(),))
    conn.commit()
    return bot_status(conn)
