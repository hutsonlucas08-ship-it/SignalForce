"""SignalForge Core: read-only research backend. No wallet, no signing, no order execution here."""
import json
import logging
import time
import time
from urllib.parse import urlparse
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
_price_history = {}
_bubble_cache = {}


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

    @app.get("/v1/research/token-images")
    async def research_token_images(
        limit: int = 50,
        _: Principal = Depends(require_read),
        conn=Depends(get_conn),
    ):
        """Resolve original launch metadata URIs to their token artwork."""
        if limit < 1 or limit > 50:
            raise HTTPException(status_code=422, detail="Limit must be between 1 and 50")
        assets = scan_assets(conn, limit)
        allowed_hosts = {
            "ipfs.io", "gateway.pinata.cloud", "cloudflare-ipfs.com",
            "cf-ipfs.com", "arweave.net", "www.arweave.net",
            "pump.fun", "metadata.pump.fun",
        }

        def metadata_url(value):
            if not isinstance(value, str) or len(value) > 2048:
                return None
            value = value.strip()
            if value.startswith("ipfs://"):
                value = "https://ipfs.io/ipfs/" + value[7:].lstrip("/")
            elif value.startswith("ar://"):
                value = "https://arweave.net/" + value[5:].lstrip("/")
            parsed = urlparse(value)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not any(
                host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts
            ):
                return None
            return value

        async def resolve(client, asset):
            uri = metadata_url(asset.get("metadata_uri"))
            if not uri:
                return None
            try:
                response = await client.get(uri, headers={"Accept": "application/json"}, follow_redirects=False)
                response.raise_for_status()
                if len(response.content) > 1_000_000:
                    return None
                metadata = response.json()
                if not isinstance(metadata, dict):
                    return None
                image = metadata.get("image") or metadata.get("imageUrl") or metadata.get("image_uri")
                if not isinstance(image, str):
                    return None
                image = image.strip()
                if image.startswith("ipfs://"):
                    image = "https://ipfs.io/ipfs/" + image[7:].lstrip("/")
                elif image.startswith("ar://"):
                    image = "https://arweave.net/" + image[5:].lstrip("/")
                parsed_image = urlparse(image)
                if parsed_image.scheme != "https" or not parsed_image.hostname:
                    return None
                return asset["asset_id"], image[:2048]
            except (httpx.HTTPError, ValueError, TypeError):
                return None

        async with httpx.AsyncClient(timeout=4.0) as client:
            resolved = await __import__("asyncio").gather(
                *(resolve(client, asset) for asset in assets if asset.get("metadata_uri"))
            )
        return {
            "images": {mint: image for item in resolved if item for mint, image in [item]},
            "source": "original token metadata",
            "note": "Artwork is read from the token's metadata URI when that URI is hosted on a supported public metadata gateway. Some tokens may not expose resolvable metadata.",
        }

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
                        "price_change_1h_pct": price_change.get("h1"),
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
        # Five-hour return is computed only from five-hour-old local price observations.
        now_ts = time.time()
        for mint, item in results.items():
            price = item.get("price_usd")
            if isinstance(price, (int, float)) and price > 0:
                history = _price_history.setdefault(mint, [])
                history.append((now_ts, float(price)))
                history = [point for point in history if point[0] >= now_ts - 25 * 3600]
                _price_history[mint] = history
                for hours in (1, 5, 24):
                    target = now_ts - hours * 3600
                    tolerance = 180 if hours == 5 else 120
                    candidates = [point for point in history if abs(point[0] - target) <= tolerance]
                    if candidates:
                        baseline = min(candidates, key=lambda point: abs(point[0] - target))[1]
                        item["price_change_" + str(hours) + "h_pct"] = (float(price) / baseline - 1) * 100
                    elif hours == 5:
                        item["price_change_5h_pct"] = None
        trending = {}
        for hours in (1, 5, 24):
            field = "price_change_" + str(hours) + "h_pct"
            ranked = []
            for asset in assets:
                mint = asset["asset_id"]
                item = results.get(mint) or {}
                change = item.get(field)
                if isinstance(change, (int, float)):
                    ranked.append({"asset_id": mint, "name": asset.get("token_name") or mint[:8], "image_url": asset.get("image_url"), "price_change_pct": change, "market_cap_usd": item.get("market_cap_usd"), "volume_24h_usd": item.get("volume_24h_usd"), "liquidity_usd": item.get("liquidity_usd")})
            trending[str(hours) + "h"] = sorted(ranked, key=lambda row: row["price_change_pct"], reverse=True)[:10]
        return {"market_data": results, "trending": trending, "source": "DexScreener + locally observed price history", "note": "1h and 24h changes use DexScreener. 5h requires five hours of continuous local snapshots and resets when the API restarts. Rankings cover discovered assets only."}

    @app.get("/v1/research/assets/{asset_id}/launch-health")
    async def asset_launch_health(asset_id: str, _: Principal = Depends(require_read)):
        """Return Bubblemaps launch-health evidence, not a rug probability."""
        if not asset_id.strip() or len(asset_id) > 128:
            raise HTTPException(status_code=422, detail="Invalid asset ID")
        cached = _bubble_cache.get(asset_id)
        now = time.time()
        if cached and now - cached[0] < 300:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get("https://bubblemaps.dev/api/map/" + asset_id)
                response.raise_for_status()
                payload = response.json()
            launch = ((payload.get("map") or {}).get("launch") or {})
            score = launch.get("score")
            if launch.get("status") != "ok" or not isinstance(score, (int, float)):
                result = {"status": "INSUFFICIENT_DATA", "health_score": None, "risk_index": None, "note": "No Bubblemaps launch score is available. Missing data is not evidence of safety."}
            else:
                health = max(0.0, min(100.0, float(score)))
                result = {"status": "AVAILABLE", "health_score": health, "risk_index": 100.0 - health, "breakdown": launch.get("breakdown") or [], "sniper_share": launch.get("sniperShare"), "bundle_share": launch.get("bundleShare"), "dev_cluster_share": launch.get("devClusterShare"), "top10_share": launch.get("top10Share"), "top_cluster_share": launch.get("topClusterShare"), "source": "Bubblemaps", "note": "Risk index is the inverse of Bubblemaps launch health. It is a screening indicator, not a rug probability or guarantee."}
        except (httpx.HTTPError, ValueError, TypeError):
            result = {"status": "UNAVAILABLE", "health_score": None, "risk_index": None, "note": "Bubblemaps data is temporarily unavailable or this token has not been mapped."}
        _bubble_cache[asset_id] = (now, result)
        return result

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
