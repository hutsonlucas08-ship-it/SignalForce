"""Tests for the guarded SQLite-to-PostgreSQL migration helper."""
import pytest

from scripts.migrate_sqlite_to_postgres import dependency_order


def test_dependency_order_places_parent_before_child():
    ordered = dependency_order(
        ["customer_accounts", "customer_sessions", "customer_integrations"],
        [
            ("customer_sessions", "customer_accounts"),
            ("customer_integrations", "customer_accounts"),
        ],
    )
    assert ordered.index("customer_accounts") < ordered.index("customer_sessions")
    assert ordered.index("customer_accounts") < ordered.index("customer_integrations")


def test_dependency_order_rejects_foreign_key_cycles():
    with pytest.raises(RuntimeError, match="Foreign-key cycle"):
        dependency_order(["alpha", "beta"], [("alpha", "beta"), ("beta", "alpha")])


def test_dependency_order_is_deterministic_for_unrelated_tables():
    assert dependency_order(["zeta", "alpha", "middle"], []) == [
        "alpha", "middle", "zeta"
    ]
