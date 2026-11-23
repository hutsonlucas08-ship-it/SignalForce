"""Small security primitives for customer credentials; keys are supplied only by deployment secrets."""
import os

from cryptography.fernet import Fernet, InvalidToken


class CredentialEncryptionError(RuntimeError):
    """Raised when credential encryption is unavailable or ciphertext is invalid."""


def _fernet(key: str | None = None) -> Fernet:
    value = key if key is not None else os.getenv("SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY")
    if not value:
        raise CredentialEncryptionError(
            "Customer credential encryption is not configured on this deployment."
        )
    try:
        return Fernet(value.encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise CredentialEncryptionError("Customer credential encryption key is invalid.") from exc


def encrypt_credential(plaintext: str, *, key: str | None = None) -> str:
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("Credential value must be a non-empty string.")
    return _fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_credential(ciphertext: str, *, key: str | None = None) -> str:
    try:
        return _fernet(key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError, UnicodeEncodeError, UnicodeDecodeError) as exc:
        raise CredentialEncryptionError("Stored customer credential could not be decrypted.") from exc
