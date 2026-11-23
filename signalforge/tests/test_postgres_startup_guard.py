import pytest

from app.config import ConfigError, Settings
from app.main import create_app


def test_create_app_refuses_unwired_postgres_url():
    settings = Settings(
        db_path="./data/test.db",
        read_api_keys=frozenset(),
        admin_api_keys=frozenset(),
        database_url="postgresql://user:password@example.invalid/database",
    )

    with pytest.raises(ConfigError, match="PostgreSQL support is not complete"):
        create_app(settings)
