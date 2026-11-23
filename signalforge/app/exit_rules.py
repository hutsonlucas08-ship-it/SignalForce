"""Exit rules for paper positions. Pure functions: no database, network, or wallet access."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class ExitConfig:
    stop_loss_pct: float = -10.0
    # (peak gain >= threshold, allowed retracement from peak), highest threshold first.
    trail_tiers: tuple = ((500.0, 30.0), (100.0, 20.0), (20.0, 12.0))
    partial_at_pct: float = 100.0
    partial_fraction: float = 0.5
    liquidity_drop_pct: float = 50.0
    max_hold_seconds: float = 24 * 3600


DEFAULT_CONFIG = ExitConfig()


@dataclass(frozen=True)
class ExitDecision:
    action: str
    reason: str = ""
    fraction: float = 1.0


HOLD = ExitDecision("hold")


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def trail_pct_for(peak_gain_pct: float, config: ExitConfig = DEFAULT_CONFIG) -> Optional[float]:
    for threshold, trail in config.trail_tiers:
        if peak_gain_pct >= threshold:
            return trail
    return None


def decide_exit(*, entry_price: float, price: float, peak_price: float,
                entry_liquidity_usd: Optional[float] = None,
                liquidity_usd: Optional[float] = None, held_seconds: float = 0.0,
                partial_taken: bool = False, config: ExitConfig = DEFAULT_CONFIG) -> ExitDecision:
    """Evaluate exits in priority order; missing price/liquidity never causes a guessed exit."""
    if not (_positive(entry_price) and _positive(price)):
        return HOLD
    peak = max(peak_price if _positive(peak_price) else price, price, entry_price)
    gain = (price / entry_price - 1) * 100
    peak_gain = (peak / entry_price - 1) * 100

    if gain <= config.stop_loss_pct:
        return ExitDecision("sell_all", f"stop loss ({gain:.1f}% from entry)")

    if _positive(entry_liquidity_usd) and _positive(liquidity_usd):
        drop = (1 - liquidity_usd / entry_liquidity_usd) * 100
        if drop >= config.liquidity_drop_pct:
            return ExitDecision("sell_all", f"liquidity down {drop:.0f}% since entry")

    trail = trail_pct_for(peak_gain, config)
    if trail is not None and price <= peak * (1 - trail / 100):
        fell = (1 - price / peak) * 100
        return ExitDecision("sell_all", f"trailing stop: fell {fell:.1f}% from peak (peak was +{peak_gain:.0f}%)")

    if not partial_taken and gain >= config.partial_at_pct:
        return ExitDecision("sell_partial", f"partial profit at +{gain:.0f}%", config.partial_fraction)

    if trail is None and held_seconds >= config.max_hold_seconds:
        return ExitDecision("sell_all", "max hold time reached without a profit trend")

    return HOLD


def describe(config: ExitConfig = DEFAULT_CONFIG) -> dict:
    return {
        "stop_loss_pct": config.stop_loss_pct,
        "take_profit": "none (no cap); trailing stop instead",
        "trailing_stop": [
            {"once_peak_gain_pct_at_least": threshold, "sell_if_price_falls_pct_from_peak": trail}
            for threshold, trail in config.trail_tiers
        ],
        "partial_profit": {"at_gain_pct": config.partial_at_pct, "fraction": config.partial_fraction},
        "liquidity_drop_exit_pct": config.liquidity_drop_pct,
        "max_hold_hours_if_no_profit_trend": config.max_hold_seconds / 3600,
    }
