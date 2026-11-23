"""Customer password and opaque-session primitives; not yet exposed as public routes."""
import base64
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from .db import utcnow

_PASSWORD_N = 1 << 14
_PASSWORD_R = 8
_PASSWORD_P = 1
_PASSWORD_DKLEN = 32
_PASSWORD_SALT_BYTES = 16


class CustomerAuthError(RuntimeError):
    pass


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must be between 12 and 1024 characters.")
    salt = secrets.token_bytes(_PASSWORD_SALT_BYTES)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_PASSWORD_N,
                             r=_PASSWORD_R, p=_PASSWORD_P, dklen=_PASSWORD_DKLEN)
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    hash_text = base64.urlsafe_b64encode(derived).decode("ascii").rstrip("=")
    return "scrypt$" + str(_PASSWORD_N) + "$" + str(_PASSWORD_R) + "$" + str(_PASSWORD_P) + "$" + salt_text + "$" + hash_text


def verify_password(password: str, encoded: str) -> bool:
    # Bound attacker-controlled login input before invoking the memory-hard KDF.
    if not isinstance(password, str) or len(password) > 1024 or not isinstance(encoded, str):
        return False
    try:
        algorithm, n, r, p, salt_text, hash_text = encoded.split("$")
        if algorithm != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        if n != _PASSWORD_N or r != _PASSWORD_R or p != _PASSWORD_P:
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(hash_text + "=" * (-len(hash_text) % 4))
        if len(salt) != _PASSWORD_SALT_BYTES or len(expected) != _PASSWORD_DKLEN:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, UnicodeEncodeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_customer_session(conn: sqlite3.Connection, account_id: str, *, lifetime_hours: int = 24) -> dict:
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("Account ID is required.")
    if not isinstance(lifetime_hours, int) or not 1 <= lifetime_hours <= 24 * 30:
        raise ValueError("Session lifetime must be between 1 hour and 30 days.")
    account = conn.execute("SELECT id,status FROM customer_accounts WHERE id=?", (account_id,)).fetchone()
    if account is None or account["status"] != "active":
        raise CustomerAuthError("Account is not active.")
    now = datetime.now(timezone.utc)
    created = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires = (now + timedelta(hours=lifetime_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO customer_sessions(token_hash,account_id,created_at,expires_at,last_seen_at,csrf_token_hash) VALUES(?,?,?,?,?,?)",
        (_token_hash(session_token), account_id, created, expires, created, _token_hash(csrf_token)),
    )
    conn.commit()
    return {"session_token": session_token, "csrf_token": csrf_token, "expires_at": expires}


def authenticate_customer_session(conn: sqlite3.Connection, session_token: str) -> dict | None:
    if not isinstance(session_token, str) or not 20 <= len(session_token) <= 256:
        return None
    digest = _token_hash(session_token)
    row = conn.execute(
        "SELECT s.account_id,s.csrf_token_hash,s.expires_at,a.email,a.status "
        "FROM customer_sessions s JOIN customer_accounts a ON a.id=s.account_id "
        "WHERE s.token_hash=? AND s.revoked_at IS NULL", (digest,)
    ).fetchone()
    if row is None or row["status"] != "active" or row["expires_at"] <= utcnow():
        return None
    conn.execute("UPDATE customer_sessions SET last_seen_at=? WHERE token_hash=?", (utcnow(), digest))
    conn.commit()
    return {"account_id": row["account_id"], "email": row["email"],
            "csrf_token_hash": row["csrf_token_hash"], "expires_at": row["expires_at"]}


def verify_csrf_token(session: dict, csrf_token: str) -> bool:
    if not isinstance(session, dict) or not isinstance(csrf_token, str):
        return False
    expected = session.get("csrf_token_hash")
    return isinstance(expected, str) and hmac.compare_digest(_token_hash(csrf_token), expected)


def revoke_customer_session(conn: sqlite3.Connection, session_token: str) -> bool:
    if not isinstance(session_token, str) or not 20 <= len(session_token) <= 256:
        return False
    cursor = conn.execute(
        "UPDATE customer_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
        (utcnow(), _token_hash(session_token)),
    )
    conn.commit()
    return cursor.rowcount == 1


def register_customer_account(conn: sqlite3.Connection, email: str, password: str) -> str:
    """Create a pending account; it cannot sign in until verified/activated."""
    import re
    import uuid

    if not isinstance(email, str) or len(email) > 254:
        raise ValueError("Enter a valid email address.")
    normalized = email.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", normalized):
        raise ValueError("Enter a valid email address.")
    password_hash = hash_password(password)
    account_id = uuid.uuid4().hex
    now = utcnow()
    try:
        conn.execute(
            "INSERT INTO customer_accounts(id,email,password_hash,status,created_at,updated_at) VALUES(?,?,?,'pending',?,?)",
            (account_id, normalized, password_hash, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise CustomerAuthError("An account with that email already exists.") from exc
    return account_id
