"""Tenant-safe data access for customer watchlists and paper accounts.

Every lookup includes the authenticated account ID. Never accept an account ID
from an untrusted browser request; callers must take it from the server session.
"""
import re
import secrets
import sqlite3

from .db import utcnow

_SOLANA_ADDRESS = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class CustomerDataError(ValueError):
    pass


def add_wallet(conn: sqlite3.Connection, account_id: str, wallet_address: str, label: str | None = None) -> dict:
    if not isinstance(account_id, str) or not account_id:
        raise CustomerDataError("Customer account is required.")
    address = wallet_address.strip() if isinstance(wallet_address, str) else ""
    if not _SOLANA_ADDRESS.fullmatch(address):
        raise CustomerDataError("Enter a valid-looking Solana public wallet address.")
    safe_label = (label or address[:6] + "…" + address[-4:]).strip()
    if len(safe_label) > 80:
        raise CustomerDataError("Wallet label must be 80 characters or fewer.")
    now = utcnow()
    try:
        conn.execute(
            """INSERT INTO customer_tracked_wallets(account_id,wallet_address,label,created_at,updated_at)
               VALUES(?,?,?,?,?)""",
            (account_id, address, safe_label, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise CustomerDataError("Wallet is already on this account's watchlist.") from exc
    return {"wallet_address": address, "label": safe_label, "status": "active"}


def list_wallets(conn: sqlite3.Connection, account_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT wallet_address,label,status,created_at,updated_at
           FROM customer_tracked_wallets WHERE account_id=? ORDER BY created_at DESC""",
        (account_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def remove_wallet(conn: sqlite3.Connection, account_id: str, wallet_address: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM customer_tracked_wallets WHERE account_id=? AND wallet_address=?",
        (account_id, wallet_address),
    )
    conn.commit()
    return cursor.rowcount == 1


def create_paper_account(
    conn: sqlite3.Connection, account_id: str, starting_capital_usd: float,
    name: str = "Main Paper Account",
) -> dict:
    if not isinstance(account_id, str) or not account_id:
        raise CustomerDataError("Customer account is required.")
    if isinstance(starting_capital_usd, bool) or not isinstance(starting_capital_usd, (int, float)):
        raise CustomerDataError("Starting capital must be a number.")
    capital = float(starting_capital_usd)
    if not 5 <= capital <= 100_000_000:
        raise CustomerDataError("Starting capital must be between $5 and $100,000,000.")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise CustomerDataError("Paper account name must be 1–80 characters.")
    account = conn.execute(
        "SELECT id FROM customer_accounts WHERE id=? AND status='active'", (account_id,)
    ).fetchone()
    if account is None:
        raise CustomerDataError("Customer account is not active.")
    paper_id = secrets.token_urlsafe(18)
    now = utcnow()
    conn.execute(
        """INSERT INTO customer_paper_accounts
           (id,account_id,name,starting_capital_usd,cash_balance_usd,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?)""",
        (paper_id, account_id, name.strip(), capital, capital, now, now),
    )
    conn.commit()
    return get_paper_account(conn, account_id, paper_id)


def get_paper_account(conn: sqlite3.Connection, account_id: str, paper_account_id: str) -> dict | None:
    row = conn.execute(
        """SELECT id,name,starting_capital_usd,cash_balance_usd,enabled,created_at,updated_at
           FROM customer_paper_accounts WHERE id=? AND account_id=?""",
        (paper_account_id, account_id),
    ).fetchone()
    return dict(row) if row else None


def list_paper_accounts(conn: sqlite3.Connection, account_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT id,name,starting_capital_usd,cash_balance_usd,enabled,created_at,updated_at
           FROM customer_paper_accounts WHERE account_id=? ORDER BY created_at""",
        (account_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_paper_positions(conn: sqlite3.Connection, account_id: str, paper_account_id: str) -> list[dict]:
    if get_paper_account(conn, account_id, paper_account_id) is None:
        raise CustomerDataError("Paper account not found.")
    rows = conn.execute(
        """SELECT p.id,p.asset_id,p.token_name,p.quantity,p.average_entry_price,p.opened_at,p.updated_at
           FROM customer_paper_positions p
           JOIN customer_paper_accounts a ON a.id=p.paper_account_id
           WHERE a.account_id=? AND a.id=? ORDER BY p.opened_at DESC""",
        (account_id, paper_account_id),
    ).fetchall()
    return [dict(row) for row in rows]


def list_paper_trades(conn: sqlite3.Connection, account_id: str, paper_account_id: str, limit: int = 100) -> list[dict]:
    if get_paper_account(conn, account_id, paper_account_id) is None:
        raise CustomerDataError("Paper account not found.")
    if not 1 <= limit <= 250:
        raise CustomerDataError("Trade limit must be between 1 and 250.")
    rows = conn.execute(
        """SELECT t.id,t.asset_id,t.token_name,t.side,t.quantity,t.price_usd,t.amount_usd,
                  t.realized_pnl_usd,t.reason,t.created_at
           FROM customer_paper_trades t
           JOIN customer_paper_accounts a ON a.id=t.paper_account_id
           WHERE a.account_id=? AND a.id=? ORDER BY t.created_at DESC,t.id DESC LIMIT ?""",
        (account_id, paper_account_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def place_paper_trade(
    conn: sqlite3.Connection, account_id: str, paper_account_id: str, *,
    asset_id: str, side: str, amount_usd: float, price_usd: float,
    token_name: str | None = None, reason: str = "manual paper trade",
) -> dict:
    if not isinstance(asset_id, str) or not asset_id.strip() or len(asset_id) > 128:
        raise CustomerDataError("A valid token mint is required.")
    if side not in {"buy", "sell"}:
        raise CustomerDataError("Trade side must be buy or sell.")
    if isinstance(amount_usd, bool) or not isinstance(amount_usd, (int, float)) or amount_usd <= 0:
        raise CustomerDataError("Trade amount must be greater than zero.")
    if isinstance(price_usd, bool) or not isinstance(price_usd, (int, float)) or price_usd <= 0:
        raise CustomerDataError("Reference price must be greater than zero.")
    if amount_usd > 10000000 or price_usd > 1e15:
        raise CustomerDataError("Trade amount or price exceeds the supported limit.")
    if token_name is not None and (not isinstance(token_name, str) or len(token_name) > 100):
        raise CustomerDataError("Token name must be 100 characters or fewer.")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 200:
        raise CustomerDataError("Trade reason must be 1–200 characters.")
    account = conn.execute(
        """SELECT id,cash_balance_usd FROM customer_paper_accounts
           WHERE id=? AND account_id=?""",
        (paper_account_id, account_id),
    ).fetchone()
    if account is None:
        raise CustomerDataError("Paper account not found.")
    now = utcnow()
    position = conn.execute(
        "SELECT id,quantity,average_entry_price FROM customer_paper_positions WHERE paper_account_id=? AND asset_id=?",
        (paper_account_id, asset_id.strip()),
    ).fetchone()
    quantity = float(amount_usd) / float(price_usd)
    realized = None
    if side == "buy":
        if float(account["cash_balance_usd"]) + 1e-9 < float(amount_usd):
            raise CustomerDataError("Insufficient paper cash balance.")
        new_cash = float(account["cash_balance_usd"]) - float(amount_usd)
        if position:
            old_qty = float(position["quantity"])
            new_qty = old_qty + quantity
            avg = ((old_qty * float(position["average_entry_price"])) + float(amount_usd)) / new_qty
            conn.execute(
                "UPDATE customer_paper_positions SET quantity=?,average_entry_price=?,token_name=COALESCE(?,token_name),updated_at=? WHERE id=?",
                (new_qty, avg, token_name, now, position["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO customer_paper_positions
                   (paper_account_id,asset_id,token_name,quantity,average_entry_price,opened_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (paper_account_id, asset_id.strip(), token_name, quantity, float(price_usd), now, now),
            )
    else:
        if position is None or quantity > float(position["quantity"]) + 1e-9:
            raise CustomerDataError("Insufficient token quantity in this paper position.")
        held_qty = float(position["quantity"])
        realized = (float(price_usd) - float(position["average_entry_price"])) * quantity
        new_cash = float(account["cash_balance_usd"]) + float(amount_usd)
        remaining = max(0.0, held_qty - quantity)
        if remaining <= 1e-9:
            conn.execute("DELETE FROM customer_paper_positions WHERE id=?", (position["id"],))
        else:
            conn.execute(
                "UPDATE customer_paper_positions SET quantity=?,updated_at=? WHERE id=?",
                (remaining, now, position["id"]),
            )
    conn.execute(
        "UPDATE customer_paper_accounts SET cash_balance_usd=?,updated_at=? WHERE id=? AND account_id=?",
        (new_cash, now, paper_account_id, account_id),
    )
    cursor = conn.execute(
        """INSERT INTO customer_paper_trades
           (paper_account_id,asset_id,token_name,side,quantity,price_usd,amount_usd,realized_pnl_usd,reason,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING id""",
        (paper_account_id, asset_id.strip(), token_name, side, quantity, float(price_usd),
         float(amount_usd), realized, reason.strip(), now),
    )
    trade_id = cursor.fetchone()["id"]
    conn.commit()
    return {
        "trade_id": trade_id, "side": side, "asset_id": asset_id.strip(),
        "quantity": quantity, "price_usd": float(price_usd), "amount_usd": float(amount_usd),
        "realized_pnl_usd": realized, "cash_balance_usd": new_cash,
    }


def get_customer_paper_trader(conn, account_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM customer_paper_accounts WHERE account_id=? ORDER BY created_at LIMIT 1", (account_id,)).fetchone()
    if row is None or row["auto_status"] == "setup":
        return None
    account = dict(row)
    positions = conn.execute(
        "SELECT asset_id,token_name,quantity,average_entry_price,last_price,opened_at FROM customer_paper_positions WHERE paper_account_id=? ORDER BY opened_at DESC",
        (account["id"],),
    ).fetchall()
    market_value = sum(float(p["quantity"]) * (float(p["last_price"]) or float(p["average_entry_price"])) for p in positions)
    equity = float(account["cash_balance_usd"]) + market_value
    realized = conn.execute("SELECT COALESCE(SUM(realized_pnl_usd),0) FROM customer_paper_trades WHERE paper_account_id=? AND side='sell'", (account["id"],)).fetchone()[0]
    account.update({
        "equity_usd": round(equity, 2),
        "profit_usd": round(equity - float(account["starting_capital_usd"]), 2),
        "target_progress_pct": round(max(0.0, min(100.0, (equity - float(account["starting_capital_usd"])) / float(account["profit_target_usd"]) * 100.0)), 1),
        "realized_pnl_usd": round(float(realized or 0), 2),
        "positions": [dict(p) for p in positions],
        "is_running": bool(account["enabled"]) and account["auto_status"] == "running",
    })
    return account


def start_customer_paper_trader(conn, account_id: str, starting_capital_usd: float, profit_target_usd: float, mode: str) -> dict:
    if mode not in {"gradual", "normal", "aggressive"}:
        raise CustomerDataError("Choose Gradual, Normal, or Aggressive mode.")
    if isinstance(starting_capital_usd, bool) or not isinstance(starting_capital_usd, (int, float)) or not 5 <= float(starting_capital_usd) <= 100000000:
        raise CustomerDataError("Starting balance must be between $5 and $100,000,000.")
    if isinstance(profit_target_usd, bool) or not isinstance(profit_target_usd, (int, float)) or not 1 <= float(profit_target_usd) <= 100000000:
        raise CustomerDataError("Profit target must be between $1 and $100,000,000.")
    if conn.execute("SELECT id FROM customer_accounts WHERE id=? AND status='active'", (account_id,)).fetchone() is None:
        raise CustomerDataError("Customer account is not active.")
    existing = conn.execute("SELECT * FROM customer_paper_accounts WHERE account_id=? ORDER BY created_at LIMIT 1", (account_id,)).fetchone()
    now = utcnow()
    if existing is None:
        paper_id = secrets.token_urlsafe(18)
        conn.execute(
            """INSERT INTO customer_paper_accounts
               (id,account_id,name,starting_capital_usd,cash_balance_usd,enabled,strategy_mode,profit_target_usd,auto_status,created_at,updated_at)
               VALUES(?,?,'Automated Paper Trader',?,?,1,?,?,'running',?,?)""",
            (paper_id, account_id, float(starting_capital_usd), float(starting_capital_usd), mode, float(profit_target_usd), now, now),
        )
    else:
        paper_id = existing["id"]
        if existing["auto_status"] in {"setup", "target_reached"}:
            conn.execute("DELETE FROM customer_paper_positions WHERE paper_account_id=?", (paper_id,))
            conn.execute(
                """UPDATE customer_paper_accounts SET starting_capital_usd=?,cash_balance_usd=?,strategy_mode=?,
                   profit_target_usd=?,enabled=1,auto_status='running',updated_at=? WHERE id=? AND account_id=?""",
                (float(starting_capital_usd), float(starting_capital_usd), mode, float(profit_target_usd), now, paper_id, account_id),
            )
        else:
            trade_count = conn.execute("SELECT COUNT(*) FROM customer_paper_trades WHERE paper_account_id=?", (paper_id,)).fetchone()[0]
            has_positions = conn.execute("SELECT 1 FROM customer_paper_positions WHERE paper_account_id=? LIMIT 1", (paper_id,)).fetchone()
            if not trade_count and not has_positions:
                conn.execute("UPDATE customer_paper_accounts SET starting_capital_usd=?,cash_balance_usd=? WHERE id=? AND account_id=?",
                             (float(starting_capital_usd), float(starting_capital_usd), paper_id, account_id))
            conn.execute(
                """UPDATE customer_paper_accounts SET strategy_mode=?,profit_target_usd=?,enabled=1,auto_status='running',updated_at=?
                   WHERE id=? AND account_id=?""",
                (mode, float(profit_target_usd), now, paper_id, account_id),
            )
    conn.commit()
    return get_customer_paper_trader(conn, account_id)


def pause_customer_paper_trader(conn, account_id: str) -> dict | None:
    conn.execute(
        "UPDATE customer_paper_accounts SET enabled=0,auto_status='paused',updated_at=? WHERE account_id=? AND auto_status='running'",
        (utcnow(), account_id),
    )
    conn.commit()
    return get_customer_paper_trader(conn, account_id)
