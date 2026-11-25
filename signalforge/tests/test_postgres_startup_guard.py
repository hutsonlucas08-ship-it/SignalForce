import pytest

from app.config import Settings
from app.main import create_app


def test_create_app_passes_configured_database_url_to_connection(monkeypatch):
    calls = []

    def fake_connect(db_path, database_url=None):
        calls.append((db_path, database_url))
        raise RuntimeError("connection intercepted by test")

    monkeypatch.setattr("app.main.connect", fake_connect)
    settings = Settings(
        db_path=":memory:",
        read_api_keys=frozenset(),
        admin_api_keys=frozenset(),
        database_url="postgresql://user:password@example.invalid/database",
    )

    with pytest.raises(RuntimeError, match="connection intercepted"):
        create_app(settings)

    assert calls == [
        (":memory:", "postgresql://user:password@example.invalid/database")
    ]
