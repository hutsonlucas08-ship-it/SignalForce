import pytest

from app.risk import RiskSnapshot, assess_risk


def test_missing_critical_data_fails_closed():
    result = assess_risk(RiskSnapshot(liquidity_usd=50_000))
    assert result["status"] == "INSUFFICIENT_DATA"
    assert "top_10_holder_pct" in result["missing_fields"]


def test_high_risk_flags_are_explainable():
    result = assess_risk(RiskSnapshot(
        liquidity_usd=2_000,
        top_10_holder_pct=72,
        mint_authority_active=True,
        freeze_authority_active=False,
        data_age_seconds=10,
    ))
    assert result["status"] == "HIGH_RISK"
    assert "low_liquidity" in result["flags"]
    assert "high_holder_concentration" in result["flags"]
    assert "mint_authority_active" in result["flags"]


def test_clean_snapshot_has_no_flags():
    result = assess_risk(RiskSnapshot(
        liquidity_usd=100_000,
        top_10_holder_pct=20,
        mint_authority_active=False,
        freeze_authority_active=False,
        data_age_seconds=5,
    ))
    assert result["status"] == "NO_FLAGS"
    assert result["flags"] == []


def test_boundary_values_are_not_flagged():
    result = assess_risk(RiskSnapshot(
        liquidity_usd=10_000,
        top_10_holder_pct=50,
        mint_authority_active=False,
        freeze_authority_active=False,
        data_age_seconds=120,
        metadata_mutable=False,
    ))
    assert result["status"] == "NO_FLAGS"


def test_mutable_metadata_requires_review():
    result = assess_risk(RiskSnapshot(
        liquidity_usd=100_000,
        top_10_holder_pct=20,
        mint_authority_active=False,
        freeze_authority_active=False,
        data_age_seconds=5,
        metadata_mutable=True,
    ))
    assert result["status"] == "REVIEW"
    assert result["flags"] == ["mutable_metadata"]
