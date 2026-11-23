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
