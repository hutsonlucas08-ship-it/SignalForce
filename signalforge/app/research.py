"""Read-only discovery and analysis over persisted market observations."""

from datetime import datetime, timezone
from typing import Optional


def scan_assets(conn, limit: int = 50) -> list[dict]:
    """List recently observed assets; this is discovery, not a trade ranking."""
    limit = max(1, min(int(limit), 200))
    rows = conn.execute(
        """
        SELECT asset_id,
               COUNT(*) AS observation_count,
               COUNT(DISTINCT source) AS source_count,
               MAX(collected_at) AS latest_collected_at,
               MAX(observed_at) AS latest_observed_at
        FROM observations
        GROUP BY asset_id
        ORDER BY latest_collected_at DESC, asset_id ASC
        LIMIT ?
        """,
        (limit,),
    )
    return [dict(row) for row in rows]


def analyze_asset(
    conn,
    asset_id: str,
    stale_after_seconds: int = 300,
    now: Optional[datetime] = None,
) -> dict:
    """Summarize evidence volume, source diversity, and freshness for one asset."""
    rows = conn.execute(
        """
        SELECT source, kind, observed_at, collected_at
        FROM observations
        WHERE asset_id = ?
        ORDER BY id DESC
        LIMIT 200
        """,
        (asset_id,),
    ).fetchall()

    if not rows:
        return {
            "asset_id": asset_id,
            "status": "NO_DATA",
            "observation_count": 0,
            "source_count": 0,
            "sources": [],
            "kind_counts": {},
            "latest_collected_at": None,
            "age_seconds": None,
            "flags": ["no_observations"],
        }

    now = now or datetime.now(timezone.utc)
    latest = max(row["collected_at"] for row in rows)
    try:
        latest_dt = datetime.strptime(
            latest, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        age_seconds = max(0.0, (now - latest_dt).total_seconds())
    except (TypeError, ValueError):
        age_seconds = None

    sources = sorted({row["source"] for row in rows})
    kind_counts = {}
    for row in rows:
        kind_counts[row["kind"]] = kind_counts.get(row["kind"], 0) + 1

    flags = []
    if len(sources) < 2:
        flags.append("single_source_only")
    if age_seconds is None:
        flags.append("invalid_collection_timestamp")
    elif age_seconds > stale_after_seconds:
        flags.append("stale_observations")

    return {
        "asset_id": asset_id,
        "status": "STALE" if "stale_observations" in flags else "OBSERVED",
        "observation_count": len(rows),
        "source_count": len(sources),
        "sources": sources,
        "kind_counts": kind_counts,
        "latest_collected_at": latest,
        "age_seconds": age_seconds,
        "flags": flags,
        "note": "Evidence summary only; source count does not establish source independence or asset safety.",
    }


def build_brain_report(conn, asset_id: str, stale_after_seconds: int = 300) -> dict:
    """Compose a deterministic, explainable research report from stored evidence."""
    from .risk import RiskSnapshot, assess_risk

    analysis = analyze_asset(conn, asset_id, stale_after_seconds)
    risk = assess_risk(RiskSnapshot())

    if analysis["status"] == "NO_DATA":
        summary = "No observations are available. There is not enough evidence to assess this asset."
        conclusion = "INSUFFICIENT_EVIDENCE"
    elif analysis["status"] == "STALE":
        summary = "Observations exist, but the latest collected data is stale."
        conclusion = "DATA_REVIEW_REQUIRED"
    elif analysis["source_count"] < 2:
        summary = "Observations exist, but evidence currently comes from only one source."
        conclusion = "LIMITED_EVIDENCE"
    else:
        summary = "Observations from multiple named sources are recorded; this does not establish source independence."
        conclusion = "EVIDENCE_AVAILABLE"

    return {
        "asset_id": asset_id,
        "report_type": "deterministic_research_summary",
        "conclusion": conclusion,
        "executive_summary": summary,
        "evidence": analysis,
        "risk_screen": risk,
        "limitations": [
            "This report summarizes stored observations; it does not verify claims made by data providers.",
            "Multiple source names do not prove independent data or corroboration.",
            "Risk screening is INSUFFICIENT_DATA until the required on-chain risk fields are collected.",
            "This is research only, not a buy/sell recommendation or execution instruction.",
        ],
    }
