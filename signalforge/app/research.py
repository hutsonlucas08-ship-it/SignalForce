"""Read-only discovery and analysis over persisted market observations."""

import json
import math
from datetime import datetime, timezone
from typing import Optional


_METADATA_FIELDS = ("name", "symbol", "uri", "pool")
_IMAGE_FIELDS = ("image", "imageUri", "image_uri", "imageUrl", "image_url", "tokenImage", "token_image")


def _safe_image_url(value):
    """Accept only absolute HTTPS image URLs from provider metadata."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if len(value) > 2048 or not value.startswith("https://"):
        return None
    return value
_METRIC_FIELDS = (
    "marketCapSol",
    "vSolInBondingCurve",
    "vTokensInBondingCurve",
    "initialBuy",
)


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
    assets = [dict(row) for row in rows]
    for asset in assets:
        first_row = conn.execute(
            "SELECT kind, payload, observed_at FROM observations WHERE asset_id = ? ORDER BY id ASC LIMIT 1",
            (asset["asset_id"],),
        ).fetchone()
        if first_row:
            try:
                first_payload = json.loads(first_row["payload"])
            except (json.JSONDecodeError, TypeError):
                first_payload = {}
            if isinstance(first_payload, dict):
                launch_cap = first_payload.get("marketCapSol")
                if isinstance(launch_cap, (int, float)) and not isinstance(launch_cap, bool) and math.isfinite(launch_cap):
                    asset["first_seen_market_cap_sol"] = launch_cap
                    asset["first_seen_market_cap_at"] = first_row["observed_at"]
    for asset in assets:
        image_rows = conn.execute(
            "SELECT payload FROM observations WHERE asset_id = ? ORDER BY id DESC LIMIT 200",
            (asset["asset_id"],),
        ).fetchall()
        for image_row in image_rows:
            try:
                payload = json.loads(image_row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                metadata_uri = payload.get("uri") or payload.get("metadataUri") or payload.get("metadata_uri")
                if not asset.get("metadata_uri") and isinstance(metadata_uri, str) and metadata_uri.strip():
                    asset["metadata_uri"] = metadata_uri.strip()[:2048]
                if not asset.get("token_name"):
                    name = payload.get("name")
                    symbol = payload.get("symbol")
                    if isinstance(name, str) and name.strip():
                        asset["token_name"] = name.strip()[:128]
                    elif isinstance(symbol, str) and symbol.strip():
                        asset["token_name"] = symbol.strip()[:32]
                image_url = next(
                    (_safe_image_url(payload.get(field)) for field in _IMAGE_FIELDS
                     if _safe_image_url(payload.get(field))),
                    None,
                )
                if image_url:
                    asset["image_url"] = image_url
                    break
    return assets


def analyze_asset(
    conn,
    asset_id: str,
    stale_after_seconds: int = 300,
    now: Optional[datetime] = None,
) -> dict:
    """Summarize evidence, extract explicitly observed token fields, and check freshness."""
    rows = conn.execute(
        """
        SELECT source, kind, payload, observed_at, collected_at
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
            "token_metadata": {},
            "image_url": None,
            "observed_metrics": {},
            "payload_fields": [],
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
    token_metadata = {}
    observed_metrics = {}
    image_url = None
    payload_fields = set()

    # Rows are newest first. Keep the newest valid value for each recognized field.
    for row in rows:
        kind_counts[row["kind"]] = kind_counts.get(row["kind"], 0) + 1
        try:
            payload = json.loads(row["payload"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue

        payload_fields.update(key for key in payload if isinstance(key, str))
        for field in _METADATA_FIELDS:
            value = payload.get(field)
            if field not in token_metadata and isinstance(value, str) and value.strip():
                token_metadata[field] = value.strip()[:256]
        if image_url is None:
            image_url = next(
                (_safe_image_url(payload.get(field)) for field in _IMAGE_FIELDS
                 if _safe_image_url(payload.get(field))),
                None,
            )
        for field in _METRIC_FIELDS:
            value = payload.get(field)
            if (
                field not in observed_metrics
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            ):
                observed_metrics[field] = {
                    "value": value,
                    "source": row["source"],
                    "kind": row["kind"],
                    "observed_at": row["observed_at"],
                }

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
        "token_metadata": token_metadata,
        "image_url": image_url,
        "observed_metrics": observed_metrics,
        "payload_fields": sorted(payload_fields),
        "flags": flags,
        "note": (
            "Extracted values are provider-reported observations, not independently verified. "
            "Source count does not establish source independence or asset safety."
        ),
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
            "Provider-reported market metrics are not equivalent to independently verified liquidity or holder-distribution data.",
            "This is research only, not a buy/sell recommendation or execution instruction.",
        ],
    }
