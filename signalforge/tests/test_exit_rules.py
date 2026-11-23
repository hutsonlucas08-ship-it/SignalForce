from app.exit_rules import HOLD, ExitConfig, decide_exit, trail_pct_for


def test_hard_stop_loss_exits_entire_position():
    decision = decide_exit(entry_price=100, price=89, peak_price=105)
    assert decision.action == "sell_all"
    assert "stop loss" in decision.reason


def test_liquidity_collapse_exits_when_both_values_are_known():
    decision = decide_exit(entry_price=1, price=1.1, peak_price=1.1,
                           entry_liquidity_usd=20000, liquidity_usd=9000)
    assert decision.action == "sell_all"
    assert "liquidity down" in decision.reason


def test_missing_liquidity_does_not_guess_an_exit():
    decision = decide_exit(entry_price=1, price=1.05, peak_price=1.05,
                           entry_liquidity_usd=20000, liquidity_usd=None)
    assert decision == HOLD


def test_trailing_stop_has_no_fixed_profit_cap_and_uses_gain_tiers():
    config = ExitConfig()
    assert trail_pct_for(20, config) == 12
    assert trail_pct_for(100, config) == 20
    assert trail_pct_for(500, config) == 30
    decision = decide_exit(entry_price=1, price=13, peak_price=20, config=config)
    assert decision.action == "sell_all"
    assert "trailing stop" in decision.reason


def test_partial_profit_is_taken_once_at_one_hundred_percent():
    decision = decide_exit(entry_price=1, price=2, peak_price=2)
    assert decision.action == "sell_partial"
    assert decision.fraction == 0.5
    assert decide_exit(entry_price=1, price=2, peak_price=2, partial_taken=True) == HOLD


def test_dead_money_timer_only_applies_before_trailing_stop_activates():
    config = ExitConfig(max_hold_seconds=100)
    assert decide_exit(entry_price=1, price=1.01, peak_price=1.01,
                       held_seconds=101, config=config).action == "sell_all"
    assert decide_exit(entry_price=1, price=1.15, peak_price=1.25,
                       held_seconds=1000, config=config) == HOLD


def test_invalid_price_never_triggers_exit():
    assert decide_exit(entry_price=1, price=0, peak_price=2).action == "hold"
