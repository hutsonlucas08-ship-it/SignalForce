"""SignalForge Core: read-only research backend. No wallet, no signing, no order execution here."""
import json
import logging
from pathlib import Path

import httpx
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import store
from .auth import Principal, RateLimiter, require_admin, require_read
from .config import Settings, load_settings
from .db import connect, run_migrations, utcnow
from .risk import RiskSnapshot, assess_risk
from .research import analyze_asset, build_brain_report, scan_assets

log = logging.getLogger("signalforge")


class HaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


class RiskScreenRequest(BaseModel):
    liquidity_usd: Optional[float] = Field(default=None, ge=0)
    top_10_holder_pct: Optional[float] = Field(default=None, ge=0, le=100)
    mint_authority_active: Optional[bool] = None
    freeze_authority_active: Optional[bool] = None
    data_age_seconds: Optional[float] = Field(default=None, ge=0)
    metadata_mutable: Optional[bool] = None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="SignalForge Core", version=settings.version)
    app.state.settings = settings
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)

    # One shared connection for ":memory:" (tests); a fresh connection per request otherwise.
    shared = connect(settings.db_path) if settings.db_path == ":memory:" else None
    boot = shared or connect(settings.db_path)
    applied = run_migrations(boot)
    store.log_event(boot, "info", "startup", f"started v{settings.version}; migrations applied: {applied or 'none'}")
    if not (settings.read_api_keys or settings.admin_api_keys):
        store.log_event(boot, "warning", "startup", "no API keys configured; all protected endpoints will reject requests")
    if shared is None:
        boot.close()

    def get_conn():
        if shared is not None:
            yield shared
            return
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/", include_in_schema=False)
    def dashboard():
        """Serve the SignalForge browser dashboard."""
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/health")
    def liveness():
        """Public, minimal: is the process up. Reveals nothing else."""
        return {"status": "ok", "time": utcnow()}

    @app.get("/v1/system/health")
    def system_health(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        sources = store.list_sources(conn, settings.stale_after_seconds)
        problems = [s["name"] for s in sources if s["status"] in ("failing", "stale")]
        halt = store.get_halt(conn)
        return {
            "status": "degraded" if problems else "ok",
            "version": settings.version,
            "time": utcnow(),
            "database": "ok",
            "trading_halted": bool(halt["trading_halted"]),
            "last_observation_collected_at": store.last_observation_time(conn),
            "sources_with_problems": problems,
            "source_count": len(sources),
            "note": "No data sources are registered yet." if not sources else None,
        }

    @app.get("/v1/system/sources")
    def sources(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        return {"sources": store.list_sources(conn, settings.stale_after_seconds)}

    @app.get("/v1/system/events")
    def events(limit: int = 50, _: Principal = Depends(require_read), conn=Depends(get_conn)):
        return {"events": store.recent_events(conn, limit)}

    @app.get("/v1/observations/{asset_id}")
    def observations(
        asset_id: str,
        limit: int = 50,
        offset: int = 0,
        source: Optional[str] = None,
        kind: Optional[str] = None,
        _: Principal = Depends(require_read),
        conn=Depends(get_conn),
    ):
        if not asset_id.strip() or len(asset_id) > 128:
            raise HTTPException(status_code=422, detail="Invalid asset ID")
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=422, detail="Limit must be between 1 and 200")
        if offset < 0:
            raise HTTPException(status_code=422, detail="Offset cannot be negative")
        if source is not None and (not source.strip() or len(source) > 100):
            raise HTTPException(status_code=422, detail="Invalid source filter")
        if kind is not None and (not kind.strip() or len(kind) > 100):
            raise HTTPException(status_code=422, detail="Invalid kind filter")

        records = store.get_observations(
            conn,
            asset_id,
            limit=limit,
            source=source,
            kind=kind,
            offset=offset,
        )

        for record in records:
            try:
                record["payload"] = json.loads(record["payload"])
            except (json.JSONDecodeError, TypeError):
                record["payload"] = None

        return {
            "observations": records,
            "limit": limit,
            "offset": offset,
            "count": len(records),
        }

    @app.post("/v1/research/risk-screen")
    def risk_screen(
        body: RiskScreenRequest,
        _: Principal = Depends(require_read),
    ):
        """Explainable heuristic screening; never emits an execution instruction."""
        return assess_risk(RiskSnapshot(**body.model_dump()))

    @app.get("/v1/research/assets")
    def research_assets(
        limit: int = 50,
        _: Principal = Depends(require_read),
        conn=Depends(get_conn),
    ):
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=422, detail="Limit must be between 1 and 200")
        return {"assets": scan_assets(conn, limit)}

    @app.get("/v1/research/market-data")
    async def research_market_data(
        limit: int = 50,
        _: Principal = Depends(require_read),
        conn=Depends(get_conn),
    ):
        """Fetch current public DEX Screener snapshots for recently discovered Solana tokens."""
        if limit < 1 or limit > 50:
            raise HTTPException(status_code=422, detail="Limit must be between 1 and 50")
        assets = scan_assets(conn, limit)
        mints = [asset["asset_id"] for asset in assets]
        results = {}
        async with httpx.AsyncClient(timeout=8.0) as client:
            for start in range(0, len(mints), 30):
                batch = mints[start:start + 30]
                if not batch:
                    continue
                url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch)
                try:
                    response = await client.get(url, headers={"Accept": "application/json"})
                    response.raise_for_status()
                    pairs = response.json()
                except (httpx.HTTPError, ValueError):
                    continue
                if not isinstance(pairs, list):
                    continue
                for pair in pairs:
                    if not isinstance(pair, dict) or pair.get("chainId") != "solana":
                        continue
                    base = pair.get("baseToken") or {}
                    mint = base.get("address")
                    if not isinstance(mint, str) or mint not in mints:
                        continue
                    liquidity = pair.get("liquidity") or {}
                    txns = pair.get("txns") or {}
                    volume = pair.get("volume") or {}
                    price_change = pair.get("priceChange") or {}
                    current = {
                        "market_cap_usd": pair.get("marketCap"),
                        "fdv_usd": pair.get("fdv"),
                        "price_usd": pair.get("priceUsd"),
                        "liquidity_usd": liquidity.get("usd"),
                        "volume_24h_usd": volume.get("h24"),
                        "buys_24h": (txns.get("h24") or {}).get("buys"),
                        "sells_24h": (txns.get("h24") or {}).get("sells"),
                        "price_change_24h_pct": price_change.get("h24"),
                        "observed_at": utcnow(),
                        "source": "DexScreener",
                    }
                    previous = results.get(mint)
                    # Prefer the most liquid pool when a token has multiple pairs.
                    pair_liquidity = liquidity.get("usd") or 0
                    previous_liquidity = (previous or {}).get("liquidity_usd") or 0
                    if previous is None or pair_liquidity > previous_liquidity:
                        results[mint] = current
        return {"market_data": results, "source": "DexScreener", "note": "Current public DEX snapshot; values can be delayed or unavailable for tokens without indexed pools. Holder counts are not provided by this endpoint."}

    @app.get("/v1/research/assets/{asset_id}/analysis")
    def asset_analysis(
        asset_id: str,
        _: Principal = Depends(require_read),
        conn=Depends(get_conn),
    ):
        if not asset_id.strip() or len(asset_id) > 128:
            raise HTTPException(status_code=422, detail="Invalid asset ID")
        return analyze_asset(conn, asset_id, settings.stale_after_seconds)

    @app.get("/v1/research/assets/{asset_id}/report")
    def brain_report(
        asset_id: str,
        _: Principal = Depends(require_read),
        conn=Depends(get_conn),
    ):
        """Return a deterministic, evidence-grounded research report."""
        if not asset_id.strip() or len(asset_id) > 128:
            raise HTTPException(status_code=422, detail="Invalid asset ID")
        return build_brain_report(conn, asset_id, settings.stale_after_seconds)

    @app.get("/v1/control/halt")
    def get_halt(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        return store.get_halt(conn)

    @app.post("/v1/control/halt")
    def halt(body: HaltRequest, who: Principal = Depends(require_admin), conn=Depends(get_conn)):
        return store.set_halt(conn, True, body.reason, f"admin:{who.fingerprint}")

    @app.post("/v1/control/resume")
    def resume(body: HaltRequest, who: Principal = Depends(require_admin), conn=Depends(get_conn)):
        return store.set_halt(conn, False, body.reason, f"admin:{who.fingerprint}")

    @app.exception_handler(Exception)
    async def unhandled(_, exc: Exception):
        log.exception("unhandled error")  # details go to logs, never to the client
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Internal error"}, status_code=500)

    return app


def app_factory() -> FastAPI:  # uvicorn app.main:app_factory --factory
    return create_app()
