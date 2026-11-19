"""Conservative, explainable token-risk screening; never an execution signal."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RiskSnapshot:
    liquidity_usd: Optional[float] = None
    top_10_holder_pct: Optional[float] = None
    mint_authority_active: Optional[bool] = None
    freeze_authority_active: Optional[bool] = None
    data_age_seconds: Optional[float] = None
    metadata_mutable: Optional[bool] = None


def assess_risk(snapshot: RiskSnapshot) -> dict:
    """Return transparent flags. Missing critical data fails closed as INSUFFICIENT_DATA."""
    required = (
        snapshot.liquidity_usd,
        snapshot.top_10_holder_pct,
        snapshot.mint_authority_active,
        snapshot.freeze_authority_active,
        snapshot.data_age_seconds,
    )
    missing = [
        name for name, value in zip(
            ("liquidity_usd", "top_10_holder_pct", "mint_authority_active",
             "freeze_authority_active", "data_age_seconds"),
            required,
        ) if value is None
    ]
    if missing:
        return {
            "status": "INSUFFICIENT_DATA",
            "flags": ["missing:" + name for name in missing],
            "missing_fields": missing,
            "note": "Incomplete data is not evidence of safety. No trading decision is produced.",
        }

    flags = []
    if snapshot.liquidity_usd < 10_000:
        flags.append("low_liquidity")
    if snapshot.top_10_holder_pct > 50:
        flags.append("high_holder_concentration")
    if snapshot.mint_authority_active:
        flags.append("mint_authority_active")
    if snapshot.freeze_authority_active:
        flags.append("freeze_authority_active")
    if snapshot.data_age_seconds > 120:
        flags.append("stale_data")
    if snapshot.metadata_mutable is True:
        flags.append("mutable_metadata")

    severe = {
        "low_liquidity", "high_holder_concentration",
        "mint_authority_active", "freeze_authority_active", "stale_data",
    }
    status = "HIGH_RISK" if severe.intersection(flags) else (
        "REVIEW" if flags else "NO_FLAGS"
    )
    return {
        "status": status,
        "flags": flags,
        "missing_fields": [],
        "note": (
            "Heuristic screening only; thresholds are configurable research defaults, "
            "not guarantees, investment advice, or a buy/sell signal."
        ),
    }
