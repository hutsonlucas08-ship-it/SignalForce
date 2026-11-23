import pytest

from app.smart_money import calculate_multiple, find_wallet_overlap, summarize_token


def test_calculate_multiple_uses_current_divided_by_entry():
    assert calculate_multiple(0.01, 0.025) == pytest.approx(2.5)


@pytest.mark.parametrize("entry,current", [(None, 1), (1, None), (0, 1), (-1, 2), ("bad", 2)])
def test_calculate_multiple_returns_none_for_missing_or_invalid_prices(entry, current):
    assert calculate_multiple(entry, current) is None


def test_overlap_counts_distinct_wallets_not_repeated_buys():
    trades = [
        {"mint": "mintA", "wallet_address": "wallet1", "side": "buy", "occurred_at": "2026-01-01T00:00:00Z"},
        {"mint": "mintA", "wallet_address": "wallet1", "side": "buy", "occurred_at": "2026-01-01T00:01:00Z"},
        {"mint": "mintA", "wallet_address": "wallet2", "side": "buy", "occurred_at": "2026-01-01T00:02:00Z"},
        {"mint": "mintA", "wallet_address": "wallet3", "side": "sell", "occurred_at": "2026-01-01T00:03:00Z"},
    ]
    result = find_wallet_overlap(trades)
    assert len(result) == 1
    assert result[0]["wallet_count"] == 2
    assert result[0]["wallets"] == ["wallet1", "wallet2"]


def test_summary_calculates_current_and_peak_x_from_observed_prices():
    trades = [
        {"wallet_address": "wallet1", "side": "buy", "price_usd": 0.01,
         "occurred_at": "2026-01-01T00:00:00Z", "usd_amount": 100, "source": "helius"},
        {"wallet_address": "wallet2", "side": "buy", "price_usd": 0.02,
         "occurred_at": "2026-01-01T00:05:00Z", "usd_amount": 50, "source": "helius"},
    ]
    prices = [
        {"price_usd": 0.015, "observed_at": "2026-01-01T00:03:00Z"},
        {"price_usd": 0.04, "observed_at": "2026-01-01T00:10:00Z"},
        {"price_usd": 0.03, "observed_at": "2026-01-01T00:15:00Z"},
    ]
    result = summarize_token(trades, prices)
    assert result["wallet_count"] == 2
    assert result["average_entry_to_current_x"] == pytest.approx(2.25)
    assert result["best_peak_x"] == pytest.approx(4.0)
    assert result["data_quality"]["wallets_with_current_multiple"] == 2


def test_summary_does_not_invent_multiple_when_entry_is_missing():
    result = summarize_token(
        [{"wallet_address": "wallet1", "side": "buy", "price_usd": None,
          "occurred_at": "2026-01-01T00:00:00Z"}],
        [{"price_usd": 1.0, "observed_at": "2026-01-01T00:01:00Z"}],
    )
    assert result["wallets"][0]["current_x"] is None
    assert result["average_entry_to_current_x"] is None
