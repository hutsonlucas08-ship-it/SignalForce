"""Persistent paper simulator. No wallet, signing, or live-order functionality."""
import json
import math
from datetime import datetime, timezone
from .db import utcnow

STARTING_BALANCE = 10000.0
FEE_RATE = 0.005
SLIPPAGE_BPS = 50
MAX_POSITION_FRACTION = 0.05
MAX_OPEN_POSITIONS = 10


def ensure_accounts(conn):
    now = utcnow()
    for strategy in ("automatic", "approval"):
        conn.execute("""INSERT INTO paper_accounts
            (id,strategy,starting_balance,cash_balance,enabled,created_at,updated_at)
            VALUES (?,?,?,?,1,?,?) ON CONFLICT DO NOTHING""", 
            ("paper-" + strategy, strategy, STARTING_BALANCE, STARTING_BALANCE, now, now))
    conn.commit()


def overview(conn):
    ensure_accounts(conn)
    accounts = []
    for strategy in ("automatic", "approval"):
        account = dict(conn.execute("SELECT * FROM paper_accounts WHERE strategy=?", (strategy,)).fetchone())
        positions = [dict(r) for r in conn.execute("SELECT * FROM paper_positions WHERE account_id=? ORDER BY opened_at DESC", (account["id"],))]
        unrealized = sum((p["last_price"] - p["avg_entry_price"]) * p["quantity"] for p in positions)
        realized = conn.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) v,COUNT(*) n FROM paper_trades WHERE account_id=? AND side='sell'", (account["id"],)).fetchone()
        wins = conn.execute("SELECT COUNT(*) n FROM paper_trades WHERE account_id=? AND side='sell' AND realized_pnl_usd>0", (account["id"],)).fetchone()["n"]
        equity = account["cash_balance"] + sum(p["last_price"] * p["quantity"] for p in positions)
        accounts.append({"strategy":strategy,"account_id":account["id"],"mode":"paper","enabled":bool(account["enabled"]),
            "starting_balance":account["starting_balance"],"cash_balance":round(account["cash_balance"],2),
            "equity":round(equity,2),"realized_pnl_usd":round(realized["v"],2),
            "unrealized_pnl_usd":round(unrealized,2),"total_pnl_usd":round(equity-account["starting_balance"],2),
            "return_pct":round((equity/account["starting_balance"]-1)*100,3),
            "closed_trades":realized["n"],"win_rate_pct":round(wins/realized["n"]*100,2) if realized["n"] else None,
            "open_positions":len(positions),"positions":positions,
            "risk_limits":{"max_position_pct_equity":10 if strategy=="automatic" else 5,"max_open_positions":20 if strategy=="automatic" else 10,"daily_realized_loss_limit_pct":5,"fee_pct":0.5,"slippage_bps":50}})
    return {"starting_balance_per_strategy":STARTING_BALANCE,"accounts":accounts,
        "note":"Paper simulation only; performance depends on supplied market-price snapshots."}


def _activity(conn, account_id, kind, message, details):
    conn.execute("INSERT INTO paper_activity(account_id,event_type,message,details_json,created_at) VALUES(?,?,?,?,?)",
        (account_id,kind,message,json.dumps(details,separators=(",",":")),utcnow()))


def place_order(conn, strategy, asset_id, token_name, side, amount_usd, reference_price, reason, session_id=None, max_position_fraction=None, max_open_positions=None):
    if strategy not in ("automatic","approval") or side not in ("buy","sell"):
        raise ValueError("Invalid strategy or side")
    if not isinstance(asset_id,str) or not asset_id.strip() or len(asset_id)>128:
        raise ValueError("Invalid token contract address")
    if not all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in (amount_usd,reference_price)):
        raise ValueError("Amount and reference price must be positive finite numbers")
    ensure_accounts(conn)
    account=dict(conn.execute("SELECT * FROM paper_accounts WHERE strategy=?",(strategy,)).fetchone())
    if not account["enabled"]:
        raise ValueError("Paper strategy is paused")
    aid=account["id"]; now=utcnow()
    position_fraction = MAX_POSITION_FRACTION if max_position_fraction is None else float(max_position_fraction)
    position_limit = MAX_OPEN_POSITIONS if max_open_positions is None else int(max_open_positions)
    if not 0 < position_fraction <= 0.25 or not 1 <= position_limit <= 50:
        raise ValueError("Invalid paper position risk limits")
    pos=conn.execute("SELECT * FROM paper_positions WHERE account_id=? AND asset_id=?",(aid,asset_id)).fetchone()
    fill=reference_price*(1+SLIPPAGE_BPS/10000 if side=="buy" else 1-SLIPPAGE_BPS/10000)
    fee=amount_usd*FEE_RATE
    if side=="buy":
        equity=account["cash_balance"]+conn.execute("SELECT COALESCE(SUM(last_price*quantity),0) v FROM paper_positions WHERE account_id=?",(aid,)).fetchone()["v"]
        if amount_usd>account["cash_balance"]: raise ValueError("Insufficient virtual cash")
        if amount_usd>equity*position_fraction: raise ValueError("Order exceeds the configured paper position limit")
        today=now[:10]
        daily=conn.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) v FROM paper_trades WHERE account_id=? AND side='sell' AND substr(created_at,1,10)=?",(aid,today)).fetchone()["v"]
        if daily <= -account["starting_balance"]*0.05: raise ValueError("Daily realized-loss limit reached; new paper entries are paused")
        count=conn.execute("SELECT COUNT(*) n FROM paper_positions WHERE account_id=?",(aid,)).fetchone()["n"]
        if pos is None and count>=position_limit: raise ValueError("Maximum configured open positions reached")
        qty=(amount_usd-fee)/fill
        if pos:
            newqty=pos["quantity"]+qty; cost=pos["cost_basis"]+amount_usd
            conn.execute("UPDATE paper_positions SET quantity=?,avg_entry_price=?,last_price=?,cost_basis=?,token_name=?,updated_at=? WHERE id=?",
                (newqty,cost/newqty,reference_price,cost,token_name,now,pos["id"]))
        else:
            conn.execute("INSERT INTO paper_positions(account_id,asset_id,token_name,quantity,avg_entry_price,last_price,cost_basis,opened_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (aid,asset_id,token_name,qty,fill,reference_price,amount_usd,now,now))
        conn.execute("UPDATE paper_accounts SET cash_balance=cash_balance-?,updated_at=? WHERE id=?",(amount_usd,now,aid))
        realized=0.0; gross=qty*fill
    else:
        if not pos: raise ValueError("No open paper position for this token")
        qty=min(pos["quantity"],amount_usd/reference_price)
        gross=qty*fill; fee=gross*FEE_RATE; proceeds=gross-fee
        realized=proceeds-pos["avg_entry_price"]*qty
        remain=pos["quantity"]-qty
        if remain<=1e-12: conn.execute("DELETE FROM paper_positions WHERE id=?",(pos["id"],))
        else: conn.execute("UPDATE paper_positions SET quantity=?,cost_basis=?,last_price=?,updated_at=? WHERE id=?",
            (remain,pos["cost_basis"]*remain/pos["quantity"],reference_price,now,pos["id"]))
        conn.execute("UPDATE paper_accounts SET cash_balance=cash_balance+?,updated_at=? WHERE id=?",(proceeds,now,aid))
    cur=conn.execute("""INSERT INTO paper_trades(account_id,asset_id,token_name,side,quantity,reference_price,fill_price,gross_usd,fee_usd,realized_pnl_usd,reason,created_at,session_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",(aid,asset_id,token_name,side,qty,reference_price,fill,gross,fee,realized,str(reason)[:200],now,session_id))
    trade_id = cur.fetchone()["id"]
    _activity(conn,aid,side,"Paper "+side.upper()+" "+(token_name or asset_id[:8])+" | USD "+format(gross,".2f"),
        {"trade_id":trade_id,"asset_id":asset_id,"quantity":qty,"reference_price":reference_price,"fill_price":fill,"fee_usd":fee,"realized_pnl_usd":realized,"reason":reason,"mode":"paper"})
    conn.commit()
    return {"trade_id":cur.lastrowid,"strategy":strategy,"mode":"paper","side":side,"asset_id":asset_id,"quantity":qty,
        "reference_price":reference_price,"fill_price":fill,"gross_usd":gross,"fee_usd":fee,"realized_pnl_usd":realized,
        "created_at":now,"note":"Simulated fill only. No funds moved or transaction submitted."}


def mark_positions(conn, prices):
    now=utcnow(); changed=0
    for mint,price in prices.items():
        if isinstance(price,(int,float)) and math.isfinite(price) and price>0:
            cur=conn.execute("UPDATE paper_positions SET last_price=?,updated_at=? WHERE asset_id=?",(price,now,mint))
            changed+=cur.rowcount
    conn.commit()
    return {"updated_positions":changed,"marked_at":now}


def activity(conn, strategy=None, limit=100):
    ensure_accounts(conn); limit=max(1,min(limit,200)); params=[]; where=""
    if strategy:
        if strategy not in ("automatic","approval"): raise ValueError("Unknown paper strategy")
        where="WHERE account_id=?"; params.append("paper-"+strategy)
    params.append(limit)
    rows=conn.execute("SELECT * FROM paper_activity "+where+" ORDER BY id DESC LIMIT ?",params)
    out=[]
    for row in rows:
        item=dict(row)
        try: item["details"]=json.loads(item.pop("details_json"))
        except (ValueError,TypeError): item["details"]={}
        out.append(item)
    return out



def session_overview(conn, session_started_at, session_stopped_at=None, active=False, session_id=None):
    """Return only executions and positions belonging to the latest timed session."""
    ensure_accounts(conn)
    account = conn.execute("SELECT * FROM paper_accounts WHERE strategy='automatic'").fetchone()
    if not session_started_at:
        return {
            "session_started_at": None, "active": bool(active), "runtime_seconds": 0,
            "invested_usd": 0.0, "realized_pnl_usd": 0.0, "unrealized_pnl_usd": 0.0, "session_profit_usd": 0.0,
            "open_positions": [], "trades": [], "trade_count": 0, "buy_count": 0, "sell_count": 0,
        }
    try:
        started = datetime.fromisoformat(session_started_at.replace("Z", "+00:00"))
        ended = (datetime.fromisoformat(session_stopped_at.replace("Z", "+00:00"))
                 if session_stopped_at else (datetime.now(timezone.utc) if active else started))
        runtime_seconds = max(0, int((ended - started).total_seconds()))
    except (TypeError, ValueError):
        runtime_seconds = 0

    if session_id:
        trade_filter = "account_id=? AND session_id=?"
        trade_params = (account["id"], session_id)
        position_buy_filter = "account_id=? AND session_id=? AND side='buy'"
        position_buy_params = (account["id"], session_id)
        position_filter = "account_id=? AND asset_id IN (SELECT DISTINCT asset_id FROM paper_trades WHERE " + position_buy_filter + ")"
        position_params = (account["id"], *position_buy_params)
    else:
        trade_filter = "account_id=? AND created_at>=?"
        trade_params = (account["id"], session_started_at)
        position_buy_filter = "account_id=? AND created_at>=? AND side='buy'"
        position_buy_params = (account["id"], session_started_at)
        position_filter = "account_id=? AND (opened_at>=? OR asset_id IN (SELECT DISTINCT asset_id FROM paper_trades WHERE " + position_buy_filter + "))"
        position_params = (account["id"], session_started_at, *position_buy_params)
    trades = [dict(row) for row in conn.execute(
        """SELECT id, asset_id, token_name, side, quantity, reference_price, fill_price,
                  gross_usd, fee_usd, realized_pnl_usd, reason, created_at
           FROM paper_trades WHERE """ + trade_filter + """
           ORDER BY created_at DESC, id DESC LIMIT 200""",
        trade_params,
    ).fetchall()]
    invested = sum(float(t["gross_usd"] or 0) + float(t["fee_usd"] or 0)
                   for t in trades if t["side"] == "buy")
    realized = sum(float(t["realized_pnl_usd"] or 0)
                   for t in trades if t["side"] == "sell")
    positions = [dict(row) for row in conn.execute(
        """SELECT asset_id, token_name, quantity, avg_entry_price, last_price,
                  cost_basis, opened_at, updated_at
           FROM paper_positions WHERE """ + position_filter + """
           ORDER BY opened_at DESC""",
        position_params,
    ).fetchall()]
    unrealized = sum((float(p["last_price"]) - float(p["avg_entry_price"])) * float(p["quantity"])
                     for p in positions)
    return {
        "session_started_at": session_started_at,
        "session_id": session_id,
        "active": bool(active),
        "runtime_seconds": runtime_seconds,
        "invested_usd": round(invested, 2),
        "realized_pnl_usd": round(realized, 2),
        "unrealized_pnl_usd": round(unrealized, 2),
        "session_profit_usd": round(realized + unrealized, 2),
        "open_positions": positions,
        "trades": trades,
        "trade_count": len(trades),
        "buy_count": sum(1 for t in trades if t["side"] == "buy"),
        "sell_count": sum(1 for t in trades if t["side"] == "sell"),
    }
