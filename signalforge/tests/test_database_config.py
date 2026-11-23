import pytest

from app.config import ConfigError, load_settings


def test_database_url_is_optional_and_defaults_to_local_sqlite(monkeypatch):
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SIGNALFORGE_READ_KEYS", "")
    monkeypatch.setenv("SIGNALFORGE_ADMIN_KEYS", "")

    settings = load_settings()

    assert settings.database_url is None
    assert settings.db_path.endswith("signalforge.db")


@pytest.mark.parametrize(
    "url",
    [
        "postgres://user:password@localhost/database",
        "postgresql://user:password@localhost/database",
    ],
)
def test_database_url_accepts_postgres_schemes_without_logging_it(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SIGNALFORGE_READ_KEYS", "")
    monkeypatch.setenv("SIGNALFORGE_ADMIN_KEYS", "")

    settings = load_settings()

    assert settings.database_url == url
    assert url not in repr(settings)


@pytest.mark.parametrize("url", ["sqlite:///tmp/app.db", "mysql://localhost/app", "not-a-url"])
def test_database_url_rejects_non_postgres_schemes(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SIGNALFORGE_READ_KEYS", "")
    monkeypatch.setenv("SIGNALFORGE_ADMIN_KEYS", "")

    with pytest.raises(ConfigError, match="DATABASE_URL must use"):
        load_settings()



def test_render_requires_postgres_url_instead_of_ephemeral_sqlite(monkeypatch):
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SIGNALFORGE_READ_KEYS", "")
    monkeypatch.setenv("SIGNALFORGE_ADMIN_KEYS", "")

    with pytest.raises(ConfigError, match="DATABASE_URL is required on Render"):
        load_settings()



def test_render_requires_valid_customer_credential_encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost/database")
    monkeypatch.delenv("SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("SIGNALFORGE_READ_KEYS", "")
    monkeypatch.setenv("SIGNALFORGE_ADMIN_KEYS", "")
    with pytest.raises(ConfigError, match="SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY is required"):
        load_settings()

    monkeypatch.setenv("SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY", "not-a-valid-fernet-key")
    with pytest.raises(ConfigError, match="must be a valid Fernet key"):
        load_settings()

    monkeypatch.setenv(
        "SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    settings = load_settings()
    assert settings.database_url == "postgresql://user:password@localhost/database"


def test_render_rejects_insecure_customer_session_cookie(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@localhost/database")
    monkeypatch.setenv(
        "SIGNALFORCE_CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setenv("SIGNALFORGE_COOKIE_SECURE", "false")
    monkeypatch.setenv("SIGNALFORGE_READ_KEYS", "")
    monkeypatch.setenv("SIGNALFORGE_ADMIN_KEYS", "")
    with pytest.raises(ConfigError, match="SIGNALFORGE_COOKIE_SECURE must remain true"):
        load_settings()
