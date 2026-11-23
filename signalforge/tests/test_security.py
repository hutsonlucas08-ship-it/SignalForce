import pytest
from cryptography.fernet import Fernet

from app.security import (
    CredentialEncryptionError,
    decrypt_credential,
    encrypt_credential,
)


def test_customer_credential_round_trip():
    key = Fernet.generate_key().decode("ascii")
    encrypted = encrypt_credential("private-api-token", key=key)
    assert encrypted != "private-api-token"
    assert decrypt_credential(encrypted, key=key) == "private-api-token"


def test_customer_credentials_use_randomized_ciphertext():
    key = Fernet.generate_key().decode("ascii")
    first = encrypt_credential("same-secret", key=key)
    second = encrypt_credential("same-secret", key=key)
    assert first != second
    assert decrypt_credential(first, key=key) == "same-secret"


def test_missing_or_invalid_encryption_key_fails_closed(monkeypatch):
    monkeypatch.delenv("SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    with pytest.raises(CredentialEncryptionError):
        encrypt_credential("secret")
    with pytest.raises(CredentialEncryptionError):
        encrypt_credential("secret", key="not-a-fernet-key")


def test_tampered_or_wrong_key_ciphertext_fails_closed():
    key = Fernet.generate_key().decode("ascii")
    other_key = Fernet.generate_key().decode("ascii")
    encrypted = encrypt_credential("secret", key=key)
    with pytest.raises(CredentialEncryptionError):
        decrypt_credential(encrypted[:-3] + "xyz", key=key)
    with pytest.raises(CredentialEncryptionError):
        decrypt_credential(encrypted, key=other_key)


def test_empty_credentials_are_rejected():
    with pytest.raises(ValueError):
        encrypt_credential("")
