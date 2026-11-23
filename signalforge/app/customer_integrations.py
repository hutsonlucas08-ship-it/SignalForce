"""Account-scoped encrypted storage for private customer integrations.

This module deliberately returns only connection metadata to browser-facing callers.
Secrets are decrypted only for trusted backend provider adapters.
"""
import json
import sqlite3
from typing import Any

from .db import utcnow
from .security import decrypt_credential, encrypt_credential

SUPPORTED_PROVIDERS = frozenset({"helius", "telegram"})
SAFE_ERROR_CODES = frozenset({
    "invalid_credentials",
    "provider_unavailable",
    "permission_denied",
    "validation_failed",
})


class CustomerIntegrationError(ValueError):
    """Invalid integration operation or account/provider reference."""


def _validate_provider(provider: str) -> str:
    if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
        raise CustomerIntegrationError("Unsupported integration provider.")
    return provider


def _validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload:
        raise CustomerIntegrationError("Integration credentials must be a non-empty object.")
    if len(json.dumps(payload, ensure_ascii=False)) > 16_384:
        raise CustomerIntegrationError("Integration credentials exceed the allowed size.")
    if any(not isinstance(key, str) or not key for key in payload):
        raise CustomerIntegrationError("Integration credential fields must have names.")
    if any(not isinstance(value, str) or not value for value in payload.values()):
        raise CustomerIntegrationError("Integration credential values must be non-empty strings.")
    return payload


def save_customer_integration(
    conn: sqlite3.Connection,
    account_id: str,
    provider: str,
    payload: dict[str, Any],
    *,
    encryption_key: str | None = None,
) -> dict:
    """Encrypt and upsert credentials for one existing customer account."""
    provider = _validate_provider(provider)
    if not isinstance(account_id, str) or not account_id.strip():
        raise CustomerIntegrationError("Customer account is required.")
    _validate_payload(payload)
    account = conn.execute(
        "SELECT id,status FROM customer_accounts WHERE id=?", (account_id,)
    ).fetchone()
    if account is None or account["status"] != "active":
        raise CustomerIntegrationError("Customer account is not active.")

    encrypted = encrypt_credential(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        key=encryption_key,
    )
    now = utcnow()
    conn.execute(
        """
        INSERT INTO customer_integrations
            (account_id,provider,encrypted_payload,status,validated_at,last_error_code,created_at,updated_at)
        VALUES(?,?,?,'unverified',NULL,NULL,?,?)
        ON CONFLICT(account_id,provider) DO UPDATE SET
            encrypted_payload=excluded.encrypted_payload,
            status='unverified',
            validated_at=NULL,
            last_error_code=NULL,
            updated_at=excluded.updated_at
        """,
        (account_id, provider, encrypted, now, now),
    )
    conn.commit()
    return get_customer_integration_status(conn, account_id, provider)


def load_customer_integration(
    conn: sqlite3.Connection,
    account_id: str,
    provider: str,
    *,
    encryption_key: str | None = None,
) -> dict[str, str] | None:
    """Decrypt credentials for trusted server-side provider use only."""
    provider = _validate_provider(provider)
    row = conn.execute(
        "SELECT encrypted_payload FROM customer_integrations WHERE account_id=? AND provider=?",
        (account_id, provider),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(decrypt_credential(row["encrypted_payload"], key=encryption_key))
    except (ValueError, TypeError) as exc:
        raise CustomerIntegrationError("Stored integration credentials are invalid.") from exc
    if not isinstance(payload, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in payload.items()
    ):
        raise CustomerIntegrationError("Stored integration credentials are invalid.")
    return payload


def get_customer_integration_status(
    conn: sqlite3.Connection, account_id: str, provider: str
) -> dict:
    """Return safe status only; never return ciphertext or plaintext credentials."""
    provider = _validate_provider(provider)
    row = conn.execute(
        """
        SELECT provider,status,validated_at,last_error_code,created_at,updated_at
        FROM customer_integrations WHERE account_id=? AND provider=?
        """,
        (account_id, provider),
    ).fetchone()
    if row is None:
        return {"provider": provider, "configured": False, "status": "not_configured"}
    return {
        "provider": row["provider"],
        "configured": True,
        "status": row["status"],
        "validated_at": row["validated_at"],
        "last_error_code": row["last_error_code"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def set_customer_integration_status(
    conn: sqlite3.Connection,
    account_id: str,
    provider: str,
    status: str,
    *,
    error_code: str | None = None,
) -> dict:
    """Record provider validation without persisting provider error text or secrets."""
    provider = _validate_provider(provider)
    if status not in {"connected", "error", "disabled", "unverified"}:
        raise CustomerIntegrationError("Invalid integration status.")
    if error_code is not None and error_code not in SAFE_ERROR_CODES:
        raise CustomerIntegrationError("Invalid integration error code.")
    if status != "error":
        error_code = None
    now = utcnow()
    cursor = conn.execute(
        """
        UPDATE customer_integrations
        SET status=?, validated_at=?, last_error_code=?, updated_at=?
        WHERE account_id=? AND provider=?
        """,
        (status, now if status == "connected" else None, error_code, now, account_id, provider),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise CustomerIntegrationError("Integration is not configured.")
    return get_customer_integration_status(conn, account_id, provider)


def disconnect_customer_integration(
    conn: sqlite3.Connection, account_id: str, provider: str
) -> bool:
    """Permanently delete one customer's encrypted integration credentials."""
    provider = _validate_provider(provider)
    cursor = conn.execute(
        "DELETE FROM customer_integrations WHERE account_id=? AND provider=?",
        (account_id, provider),
    )
    conn.commit()
    return cursor.rowcount == 1
