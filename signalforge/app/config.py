"""Settings loaded from environment variables. No secrets live in code."""
import os
from dataclasses import dataclass, field

MIN_KEY_LENGTH = 24
VERSION = "0.1.0"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    db_path: str
    read_api_keys: frozenset
    admin_api_keys: frozenset
    rate_limit_per_minute: int = 300
    stale_after_seconds: int = 300
    version: str = VERSION
    database_url: str | None = field(default=None, repr=False)


def _keys(name: str) -> frozenset:
    keys = frozenset(
        k.strip() for k in os.getenv(name, "").split(",")
        if k.strip() and not k.strip().upper().startswith("REPLACE_WITH_")
    )
    for k in keys:
        if len(k) < MIN_KEY_LENGTH:
            raise ConfigError(f"{name} contains a key shorter than {MIN_KEY_LENGTH} characters")
    return keys


def _database_url() -> str | None:
    """Read the optional hosted PostgreSQL URL without exposing it in settings logs."""
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        return None
    if not value.startswith(("postgres://", "postgresql://")):
        raise ConfigError("DATABASE_URL must use the postgres:// or postgresql:// scheme")
    return value


def load_settings() -> Settings:
    read_keys = _keys("SIGNALFORGE_READ_KEYS")
    admin_keys = _keys("SIGNALFORGE_ADMIN_KEYS")
    if read_keys & admin_keys:
        raise ConfigError("A key cannot be both a read key and an admin key")
    database_url = _database_url()
    if os.getenv("RENDER_SERVICE_ID") and not database_url:
        raise ConfigError(
            "DATABASE_URL is required on Render. Refusing to start with ephemeral SQLite storage."
        )
    return Settings(
        db_path=os.getenv("SIGNALFORGE_DB_PATH", "./data/signalforge.db"),
        read_api_keys=read_keys,
        admin_api_keys=admin_keys,
        rate_limit_per_minute=int(os.getenv("SIGNALFORGE_RATE_LIMIT_PER_MINUTE", "300")),
        stale_after_seconds=int(os.getenv("SIGNALFORGE_STALE_AFTER_SECONDS", "300")),
        database_url=database_url,
    )
