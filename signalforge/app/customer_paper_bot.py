"""Tenant-isolated automatic paper trading driven by the shared tracked-wallet feed."""
from __future__ import annotations
import asyncio, logging, time
from datetime import datetime, timedelta, timezone
import httpx
from . import customer_data, paper_bot
from .db import connect, utcnow
from .research import scan_assets
log = logging.getLogger("signalforce.customer_paper_bot")
INTERVAL_SECONDS = 30.0
MODE_RULES = {
 "gradual":{"position_fraction":0.03,"max_positions":4,"min_score":55.0,"stop_loss":-8.0,"take_profit":25.0},
 "normal":{"position_fraction":0.05,"max_positions":7,"min_score":45.0,"stop_loss":-10.0,"take_profit":35.0},
 "aggressive":{"position_fraction":0.08,"max_positions":10,"min_score":35.0,"stop_loss":-15.0,"take_profit":50.0},
}
def _equity(conn, account):
 rows=conn.execute("SELECT quantity,average_entry_price,last_price FROM customer_paper_positions WHERE paper_account_id=?",(account["id"],)).fetchall()
 return float(account["cash_balance_usd"])+sum(float(p["quantity"])*(float(p["last_price"]) or float(p["average_entry_price"])) for p in rows)
def _sell(conn, account, pos, price, reason):
 amount=float(pos["quantity"])*price
 if amount>0:
  customer_data.place_paper_trade(conn,account["account_id"],account["id"],asset_id=pos["asset_id"],token_name=pos["token_name"],side="sell",amount_usd=amount,price_usd=price,reason=reason)
def process_customer_accounts(conn,pairs,wallet_signals):
 summary={"customers":0,"buys":0,"sells":0,"targets_reached":0}
 accounts=conn.execute("SELECT * FROM customer_paper_accounts WHERE enabled=1 AND auto_status='running' ORDER BY created_at").fetchall()
 for raw in accounts:
  account=dict(raw); rules=MODE_RULES.get(account["strategy_mode"],MODE_RULES["normal"])
  positions=conn.execute("SELECT * FROM customer_paper_positions WHERE paper_account_id=?",(account["id"],)).fetchall()
  for raw_pos in positions:
   pos=dict(raw_pos); pair=pairs.get(pos["asset_id"])
   try: price=float((pair or {}).get("priceUsd"))
   except (TypeError,ValueError): price=0
   if price<=0: continue
   conn.execute("UPDATE customer_paper_positions SET last_price=?,updated_at=? WHERE id=? AND paper_account_id=?",(price,utcnow(),pos["id"],account["id"]))
   change=(price/float(pos["average_entry_price"])-1)*100
   if change<=rules["stop_loss"]:
    _sell(conn,account,pos,price,"Automatic "+account["strategy_mode"]+" stop-loss"); summary["sells"]+=1
   elif change>=rules["take_profit"]:
    _sell(conn,account,pos,price,"Automatic "+account["strategy_mode"]+" take-profit"); summary["sells"]+=1
  conn.commit()
  account=dict(conn.execute("SELECT * FROM customer_paper_accounts WHERE id=? AND account_id=?",(account["id"],account["account_id"])).fetchone())
  if _equity(conn,account)-float(account["starting_capital_usd"])>=float(account["profit_target_usd"]):
   # Close any remaining positions at the latest available simulated mark before pausing.
   open_positions=conn.execute("SELECT * FROM customer_paper_positions WHERE paper_account_id=?",(account["id"],)).fetchall()
   for open_pos in open_positions:
    pos=dict(open_pos); mark=float(pos["last_price"]) or float(pos["average_entry_price"])
    _sell(conn,account,pos,mark,"Profit target reached · automatic paper close")
   conn.execute("UPDATE customer_paper_accounts SET enabled=0,auto_status='target_reached',updated_at=? WHERE id=? AND account_id=?",(utcnow(),account["id"],account["account_id"]))
   conn.commit(); summary["targets_reached"]+=1; continue
  count=conn.execute("SELECT COUNT(*) n FROM customer_paper_positions WHERE paper_account_id=?",(account["id"],)).fetchone()["n"]
  if count<rules["max_positions"]:
   candidates=[]
   cooldown_since=(datetime.now(timezone.utc)-timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
   for mint,signal in wallet_signals.items():
    if int(signal.get("wallet_count") or 0)<1: continue
    if conn.execute("SELECT 1 FROM customer_paper_positions WHERE paper_account_id=? AND asset_id=?",(account["id"],mint)).fetchone(): continue
    if conn.execute("SELECT 1 FROM customer_paper_trades WHERE paper_account_id=? AND asset_id=? AND side='buy' AND created_at>=? LIMIT 1",(account["id"],mint,cooldown_since)).fetchone(): continue
    pair=pairs.get(mint)
    if not pair: continue
    score,_=paper_bot._score(pair)
    if score is not None and score>=rules["min_score"]: candidates.append((score+min(int(signal["wallet_count"])*10,25),mint,pair))
   candidates.sort(reverse=True,key=lambda item:item[0])
   if candidates:
    _,mint,pair=candidates[0]
    try: price=float(pair["priceUsd"])
    except (TypeError,ValueError): price=0
    positions_value=sum(float(p["quantity"])*(float(p["last_price"]) or float(p["average_entry_price"])) for p in conn.execute("SELECT quantity,last_price,average_entry_price FROM customer_paper_positions WHERE paper_account_id=?",(account["id"],)).fetchall())
    amount=min(float(account["cash_balance_usd"]), (float(account["cash_balance_usd"])+positions_value)*rules["position_fraction"])
    if price>0 and amount>=1:
     base=pair.get("baseToken") or {}
     customer_data.place_paper_trade(conn,account["account_id"],account["id"],asset_id=mint,token_name=base.get("name") or base.get("symbol") or mint[:8],side="buy",amount_usd=amount,price_usd=price,reason="Automated shared-wallet signal · "+account["strategy_mode"].title()+" mode")
     conn.execute("UPDATE customer_paper_positions SET last_price=? WHERE paper_account_id=? AND asset_id=?",(price,account["id"],mint)); summary["buys"]+=1
  conn.execute("UPDATE customer_paper_accounts SET last_scan_at=?,updated_at=? WHERE id=?",(utcnow(),utcnow(),account["id"]))
  conn.commit(); summary["customers"]+=1
 return summary
async def run_customer_paper_bot(db_path,database_url,stop_event):
 async with httpx.AsyncClient(timeout=10.0,headers={"Accept":"application/json"}) as client:
  while not stop_event.is_set():
   started=time.monotonic(); conn=None
   try:
    conn=connect(db_path,database_url)
    enabled=conn.execute("SELECT 1 FROM customer_paper_accounts WHERE enabled=1 AND auto_status='running' LIMIT 1").fetchone()
    if enabled:
     assets=scan_assets(conn,100); signals=paper_bot._recent_wallet_buy_signals(conn)
     held=[r["asset_id"] for r in conn.execute("SELECT DISTINCT asset_id FROM customer_paper_positions").fetchall()]
     mints=list(dict.fromkeys(held+list(signals.keys())+[a["asset_id"] for a in assets if a.get("asset_id")]))[:180]
     pairs=await paper_bot._fetch_pairs(client,mints) if mints else {}
     log.info("Customer paper scan: %s",process_customer_accounts(conn,pairs,signals))
   except asyncio.CancelledError: raise
   except Exception: log.exception("Customer paper trader scan failed")
   finally:
    if conn is not None: conn.close()
   try: await asyncio.wait_for(stop_event.wait(),timeout=max(0.0,INTERVAL_SECONDS-(time.monotonic()-started)))
   except asyncio.TimeoutError: pass
