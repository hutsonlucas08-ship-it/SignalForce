"""SignalForge Core: read-only research backend. No wallet, no signing, no order execution here."""
import asyncio
import json
import logging
import os
import time
from urllib.parse import urlparse
from pathlib import Path

import httpx
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import store
from .auth import Principal, RateLimiter, _fp, require_admin, require_read
from .config import Settings, load_settings
from .db import connect, run_migrations, utcnow
from .risk import RiskSnapshot, assess_risk
from .research import analyze_asset, build_brain_report, scan_assets
from . import paper, paper_bot, telegram_alerts
from .sources.helius_worker import run_helius_sync

log = logging.getLogger("signalforge")
_price_history = {}
_bubble_cache = {}
_launch_cache = {}


class HaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


class PaperOrderRequest(BaseModel):
    strategy: str = Field(min_length=1, max_length=20)
    asset_id: str = Field(min_length=1, max_length=128)
    token_name: Optional[str] = Field(default=None, max_length=100)
    side: str = Field(min_length=1, max_length=10)
    amount_usd: float = Field(gt=0, le=10000)
    reference_price: float = Field(gt=0)
    reason: str = Field(default="paper strategy signal", min_length=1, max_length=200)


class PaperMarkRequest(BaseModel):
    prices: dict[str, float]


class PaperBotConfigRequest(BaseModel):
    enabled: Optional[bool] = None
    allocated_capital_usd: Optional[float] = Field(default=None, ge=100, le=100000)
    duration_seconds: Optional[int] = None


class WalletWatchRequest(BaseModel):
    wallet_address: str = Field(min_length=32, max_length=44)
    label: Optional[str] = Field(default=None, max_length=80)

class BulkWalletWatchRequest(BaseModel):
    wallet_addresses: list[str] = Field(min_length=1, max_length=1000)


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
    app.state.paper_bot_stop = asyncio.Event()
    app.state.paper_bot_task = None
    app.state.helius_sync_stop = asyncio.Event()
    app.state.helius_sync_task = None

    @app.on_event("startup")
    async def start_paper_bot():
        if settings.db_path != ":memory:":
            app.state.paper_bot_task = asyncio.create_task(
                paper_bot.run_paper_bot(settings.db_path, app.state.paper_bot_stop)
            )
            if os.getenv("HELIUS_API_KEY"):
                app.state.helius_sync_task = asyncio.create_task(
                    run_helius_sync(settings.db_path, app.state.helius_sync_stop)
                )

    @app.on_event("shutdown")
    async def stop_paper_bot():
        app.state.paper_bot_stop.set()
        app.state.helius_sync_stop.set()
        helius_task = app.state.helius_sync_task
        if helius_task is not None:
            try:
                await asyncio.wait_for(helius_task, timeout=25)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                helius_task.cancel()
        task = app.state.paper_bot_task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=6)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

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

    @app.get("/v1/auth/status")
    def auth_status():
        """Safe connection diagnostic; exposes short one-way fingerprints, never key values."""
        return {
            "read_key_configured": bool(settings.read_api_keys),
            "read_key_count": len(settings.read_api_keys),
            "read_key_fingerprints": sorted(_fp(key) for key in settings.read_api_keys),
            "admin_key_configured": bool(settings.admin_api_keys),
            "admin_key_count": len(settings.admin_api_keys),
        }

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
        async with httpx.AsyncClient(timeout=4.0) as client:
            async def fetch_batch(batch):
                if not batch:
                    return []
                url = "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch)
                try:
                    response = await client.get(url, headers={"Accept": "application/json"})
                    response.raise_for_status()
                    payload = response.json()
                    return payload if isinstance(payload, list) else []
                except (httpx.HTTPError, ValueError):
                    log.warning("DexScreener market-data batch failed")
                    return []

            batches = [mints[start:start + 30] for start in range(0, len(mints), 30)]
            batch_results = await __import__("asyncio").gather(
                *(fetch_batch(batch) for batch in batches)
            )
            for pairs in batch_results:
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
                        "image_url": ((pair.get("info") or {}).get("imageUrl") if isinstance(pair.get("info"), dict) else None),
                        "liquidity_usd": liquidity.get("usd"),
                        "volume_24h_usd": volume.get("h24"),
                        "buys_5m": (txns.get("m5") or {}).get("buys"),
                        "sells_5m": (txns.get("m5") or {}).get("sells"),
                        "volume_5m_usd": volume.get("m5"),
                        "buys_1h": (txns.get("h1") or {}).get("buys"),
                        "sells_1h": (txns.get("h1") or {}).get("sells"),
                        "volume_1h_usd": volume.get("h1"),
                        "price_change_5m_pct": price_change.get("m5"),
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
                    ranked.append({"asset_id": mint, "name": asset.get("token_name") or mint[:8], "image_url": item.get("image_url") or asset.get("image_url"), "price_change_pct": change, "market_cap_usd": item.get("market_cap_usd"), "volume_24h_usd": item.get("volume_24h_usd"), "liquidity_usd": item.get("liquidity_usd")})
            trending[str(hours) + "h"] = sorted(ranked, key=lambda row: row["price_change_pct"], reverse=True)[:10]
        return {"market_data": results, "trending": trending, "source": "DexScreener",
            "trend_source": "DexScreener + locally observed price history", "note": "1h and 24h changes use DexScreener. 5h requires five hours of continuous local snapshots and resets when the API restarts. Rankings cover discovered assets only. Holder counts are not provided by this market-data endpoint."}

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


    @app.get("/v1/research/new-launches")
    async def research_new_launches(_: Principal = Depends(require_read)):
        """Return the newest Solana pools and token artwork from public GeckoTerminal data."""
        now = time.time()
        cached = _launch_cache.get("solana")
        if cached and now - cached[0] < 45:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    "https://api.geckoterminal.com/api/v2/networks/solana/new_pools",
                    params={"include": "base_token,quote_token,dex", "page": 1},
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
            included = {
                item.get("id"): item.get("attributes") or {}
                for item in (payload.get("included") or [])
                if isinstance(item, dict) and item.get("type") == "token"
            }
            launches = []
            for pool in payload.get("data") or []:
                if not isinstance(pool, dict):
                    continue
                attrs = pool.get("attributes") or {}
                relationships = pool.get("relationships") or {}
                base_ref = ((relationships.get("base_token") or {}).get("data") or {})
                quote_ref = ((relationships.get("quote_token") or {}).get("data") or {})
                base_token = included.get(base_ref.get("id"), {})
                quote_token = included.get(quote_ref.get("id"), {})
                quote_like_addresses = {
                    "So11111111111111111111111111111111111111112",
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                }
                token = quote_token if base_token.get("address") in quote_like_addresses else base_token
                address = token.get("address")
                if not isinstance(address, str) or not address:
                    continue
                image = token.get("image_url")
                if not isinstance(image, str) or not image.startswith("https://"):
                    image = None
                dex_ref = ((relationships.get("dex") or {}).get("data") or {})
                pool_id = pool.get("id") or ""
                launches.append({
                    "asset_id": address,
                    "name": token.get("name") or address[:8],
                    "symbol": token.get("symbol") or "",
                    "image_url": image,
                    "pool_address": pool_id.split("_", 1)[-1],
                    "pool_created_at": attrs.get("pool_created_at"),
                    "price_usd": attrs.get("base_token_price_usd"),
                    "liquidity_usd": attrs.get("reserve_in_usd"),
                    "volume_24h_usd": (attrs.get("volume_usd") or {}).get("h24"),
                    "dex": dex_ref.get("id"),
                    "source": "GeckoTerminal",
                })
            result = {
                "launches": launches[:10],
                "count": len(launches[:10]),
                "source": "GeckoTerminal public Solana new-pools feed",
                "note": "New pool creation is a discovery signal, not proof of a legitimate launch or a safe token. Artwork is provider-supplied and may be unavailable.",
            }
            _launch_cache["solana"] = (now, result)
            return result
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            log.warning("GeckoTerminal new-pools feed unavailable")
            raise HTTPException(status_code=502, detail="GeckoTerminal's Solana new-pools feed is temporarily unavailable")

    @app.get("/v1/research/trending-now")
    async def trending_now(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        """Rank recently discovered Solana tokens using live DEX Screener activity."""
        assets = scan_assets(conn, 50)
        mints = [a["asset_id"] for a in assets if a.get("asset_id")]
        async with httpx.AsyncClient(timeout=5.0) as client:
            pairs = await paper_bot._fetch_pairs(client, mints) if mints else {}
        names = {a["asset_id"]: a.get("token_name") for a in assets}
        images = {a["asset_id"]: a.get("image_url") for a in assets}
        if not mints:
            return {
                "candidates": [], "scanned": 0, "priced": 0, "rejected": 0,
                "source": "DEX Screener market data",
                "note": "No locally discovered Solana tokens are available to rank yet.",
            }
        candidates, rejected = [], 0
        for mint, pair in pairs.items():
            score, reason = paper_bot._score(pair)
            if score is None:
                rejected += 1
                continue
            base = pair.get("baseToken") or {}
            info = pair.get("info") or {}
            candidates.append({
                "asset_id": mint,
                "name": names.get(mint) or base.get("name") or mint[:8],
                "symbol": base.get("symbol"),
                "image_url": info.get("imageUrl") or images.get(mint),
                "score": score,
                "price_usd": pair.get("priceUsd"),
                "market_cap_usd": pair.get("marketCap") or pair.get("fdv"),
                "liquidity_usd": (pair.get("liquidity") or {}).get("usd"),
                "volume_5m_usd": (pair.get("volume") or {}).get("m5"),
                "buys_5m": ((pair.get("txns") or {}).get("m5") or {}).get("buys"),
                "sells_5m": ((pair.get("txns") or {}).get("m5") or {}).get("sells"),
                "price_change_5m_pct": ((pair.get("priceChange") or {}).get("m5")),
                "source": "DEX Screener",
            })
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return {
            "candidates": candidates[:20], "scanned": len(mints),
            "priced": len(pairs), "rejected": rejected,
            "source": "DEX Screener live pair activity across SignalForge-discovered Solana tokens",
            "note": "Ranks only tokens SignalForge has discovered, using liquidity, volume, transaction activity and momentum. It is not a complete market-wide trending list. No score guarantees safety or profit.",
        }

    @app.get("/v1/integrations/helius/status")
    async def helius_integration_status(_: Principal = Depends(require_read)):
        """Verify both Helius services without exposing credentials or response bodies."""
        key = os.getenv("HELIUS_API_KEY")
        if not key:
            return {
                "configured": False,
                "healthy": False,
                "rpc": {"status": "not_configured", "http_status": None},
                "enhanced_transactions": {"status": "not_configured", "http_status": None},
            }

        async def check_rpc(client):
            try:
                response = await client.post(
                    "https://mainnet.helius-rpc.com/",
                    params={"api-key": key},
                    json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("result") != "ok":
                    return {"status": "unexpected_response", "http_status": response.status_code}
                return {"status": "connected", "http_status": response.status_code}
            except httpx.HTTPStatusError as exc:
                return {"status": "http_error", "http_status": exc.response.status_code}
            except (httpx.HTTPError, ValueError, AttributeError):
                return {"status": "unavailable", "http_status": None}

        async def check_enhanced_transactions(client):
            try:
                response = await client.get(
                    "https://api-mainnet.helius-rpc.com/v0/addresses/11111111111111111111111111111111/transactions",
                    params={"api-key": key, "limit": 1},
                )
                response.raise_for_status()
                if not isinstance(response.json(), list):
                    return {"status": "unexpected_response", "http_status": response.status_code}
                return {"status": "connected", "http_status": response.status_code}
            except httpx.HTTPStatusError as exc:
                return {"status": "http_error", "http_status": exc.response.status_code}
            except (httpx.HTTPError, ValueError, AttributeError):
                return {"status": "unavailable", "http_status": None}

        async with httpx.AsyncClient(timeout=8.0) as client:
            rpc, enhanced = await asyncio.gather(
                check_rpc(client), check_enhanced_transactions(client)
            )
        return {
            "configured": True,
            "healthy": rpc["status"] == "connected" and enhanced["status"] == "connected",
            "rpc": rpc,
            "enhanced_transactions": enhanced,
            "note": "Connectivity only. This does not mean wallet discovery or continuous trade ingestion is running.",
        }

    @app.get("/v1/integrations/helius/worker-status")
    def helius_worker_status(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        """Report actual in-process worker state separately from provider connectivity."""
        task = app.state.helius_sync_task
        configured = bool(os.getenv("HELIUS_API_KEY"))
        if not configured:
            worker_state = "disabled"
        elif task is None:
            worker_state = "not_started"
        elif task.cancelled():
            worker_state = "stopped"
        elif task.done():
            worker_state = "failed"
        else:
            worker_state = "running"

        source = conn.execute(
            "SELECT last_success_at, last_error_at, last_error, consecutive_failures "
            "FROM data_sources WHERE name=?",
            ("helius_wallet_sync",),
        ).fetchone()
        wallet_count = conn.execute("SELECT COUNT(*) FROM tracked_wallets").fetchone()[0]
        quarantined_wallets = conn.execute(
            "SELECT wallet_address, helius_404_count FROM tracked_wallets "
            "WHERE helius_status='quarantined' ORDER BY wallet_address"
        ).fetchall()
        last_wallet_sync = conn.execute(
            "SELECT MAX(last_seen_at) FROM tracked_wallets"
        ).fetchone()[0]
        latest_trade = conn.execute(
            "SELECT MAX(occurred_at) FROM wallet_trade_events"
        ).fetchone()[0]
        return {
            "configured": configured,
            "worker_state": worker_state,
            "worker_running": worker_state == "running",
            "tracked_wallet_count": wallet_count,
            "quarantined_wallet_count": len(quarantined_wallets),
            "quarantined_wallets": [
                {"wallet": f"{row['wallet_address'][:6]}…{row['wallet_address'][-4:]}",
                 "consecutive_404s": row["helius_404_count"]}
                for row in quarantined_wallets
            ],
            "last_success_at": source["last_success_at"] if source else None,
            "last_error_at": source["last_error_at"] if source else None,
            "last_error": source["last_error"] if source else None,
            "consecutive_failures": source["consecutive_failures"] if source else 0,
            "last_wallet_sync_at": last_wallet_sync,
            "latest_trade_at": latest_trade,
            "poll_interval_seconds": 60,
            "note": "Worker state is process-local. A running task does not guarantee the last poll succeeded; check last_success_at and consecutive_failures.",
        }

    @app.get("/v1/copytraders/wallets")
    def tracked_wallets(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        rows = conn.execute("SELECT * FROM tracked_wallets ORDER BY added_at DESC").fetchall()
        return {"wallets": [dict(row) for row in rows], "provider_configured": bool(os.getenv("HELIUS_API_KEY"))}

    @app.post("/v1/copytraders/wallets")
    def add_tracked_wallet(body: WalletWatchRequest, _: Principal = Depends(require_read), conn=Depends(get_conn)):
        import re
        address = body.wallet_address.strip()
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
            raise HTTPException(status_code=422, detail="Enter a valid-looking Solana public wallet address")
        conn.execute("INSERT OR IGNORE INTO tracked_wallets(wallet_address,label,added_at) VALUES(?,?,?)",
                     (address, body.label or address[:6]+"…"+address[-4:], utcnow()))
        conn.commit()
        return {"added": True, "wallet_address": address, "label": body.label or address[:6]+"…"+address[-4:]}

    @app.post("/v1/copytraders/wallets/bulk")
    def bulk_add_tracked_wallets(body: BulkWalletWatchRequest, _: Principal = Depends(require_read), conn=Depends(get_conn)):
        import re
        seen = set()
        added = 0
        duplicates = 0
        invalid_count = 0
        invalid = []
        for raw_address in body.wallet_addresses:
            address = raw_address.strip()
            if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", address):
                invalid_count += 1
                if len(invalid) < 20:
                    invalid.append(address[:80])
                continue
            if address in seen:
                duplicates += 1
                continue
            seen.add(address)
            cursor = conn.execute(
                "INSERT OR IGNORE INTO tracked_wallets(wallet_address,label,added_at) VALUES(?,?,?)",
                (address, address[:6] + "…" + address[-4:], utcnow()),
            )
            if cursor.rowcount:
                added += 1
            else:
                duplicates += 1
        conn.commit()
        return {
            "submitted_count": len(body.wallet_addresses),
            "unique_valid_count": len(seen),
            "added_count": added,
            "duplicate_count": duplicates,
            "invalid_count": invalid_count,
            "invalid_addresses": invalid,
        }

    @app.get("/v1/copytraders/activity")
    async def tracked_wallet_activity(wallet: str, _: Principal = Depends(require_read), conn=Depends(get_conn)):
        import re
        if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", wallet):
            raise HTTPException(status_code=422, detail="Invalid Solana wallet address")
        tracked = conn.execute("SELECT * FROM tracked_wallets WHERE wallet_address=?", (wallet,)).fetchone()
        if not tracked:
            raise HTTPException(status_code=404, detail="Wallet is not on your watchlist")
        key = os.getenv("HELIUS_API_KEY")
        if not key:
            return {"configured": False, "transactions": [],
                    "note": "Set HELIUS_API_KEY as a server-side secret to retrieve parsed wallet activity."}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get(
                    "https://api-mainnet.helius-rpc.com/v0/addresses/"+wallet+"/transactions",
                    params={"api-key":key,"limit":20,"sort-order":"desc","type":"SWAP","token-accounts":"balanceChanged"},
                )
                response.raise_for_status()
                payload = response.json()
            rows = payload if isinstance(payload,list) else []
            transactions = [{"signature":x.get("signature"),"timestamp":x.get("timestamp"),
                             "description":x.get("description"),"type":x.get("type"),
                             "source":x.get("source"),"token_transfers":x.get("tokenTransfers") or [],
                             "native_transfers":x.get("nativeTransfers") or []} for x in rows]
            return {"configured":True,"wallet":wallet,"transactions":transactions,
                    "note":"Parsed public on-chain swap history. This is not a verified profit leaderboard; token price history and complete position accounting are required before ranking wallet performance."}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            raise HTTPException(
                status_code=502,
                detail=f"Helius returned HTTP {status} while fetching wallet history.",
            )
        except httpx.TimeoutException:
            raise HTTPException(status_code=502, detail="Helius wallet history request timed out.")
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Could not connect to Helius wallet history.")

    @app.get("/v1/smart-money/overlap")
    def smart_money_overlap(
        hours_back: int = 24,
        minimum_wallets: int = 2,
        limit: int = 50,
        _: Principal = Depends(require_read),
        conn=Depends(get_conn),
    ):
        """Show mints bought by multiple tracked wallets in the selected window."""
        from .smart_money import find_wallet_overlap
        if hours_back < 1 or hours_back > 168:
            raise HTTPException(status_code=422, detail="hours_back must be between 1 and 168")
        if minimum_wallets < 2 or minimum_wallets > 50:
            raise HTTPException(status_code=422, detail="minimum_wallets must be between 2 and 50")
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
        rows = conn.execute(
            "SELECT wallet_address, signature, mint, token_name, token_symbol, side, "
            "price_usd, market_cap_usd, occurred_at, source "
            "FROM wallet_trade_events WHERE side='buy' "
            "AND occurred_at >= strftime('%Y-%m-%dT%H:%M:%SZ','now', ?) "
            "ORDER BY occurred_at DESC LIMIT 10000",
            (f"-{hours_back} hours",),
        ).fetchall()
        overlaps = find_wallet_overlap([dict(row) for row in rows], minimum_wallets)
        return {
            "overlaps": overlaps[:limit],
            "window_hours": hours_back,
            "minimum_wallets": minimum_wallets,
            "trades_scanned": len(rows),
            "source": "stored wallet trade observations",
            "note": "Only ingested and timestamped BUY events are counted. An empty result does not prove that no wallets are buying.",
        }

    @app.get("/v1/smart-money/tokens/{mint}/summary")
    def smart_money_token_summary(
        mint: str,
        _: Principal = Depends(require_read),
        conn=Depends(get_conn),
    ):
        """Summarize tracked wallet entries and observed token price multiples."""
        from .smart_money import summarize_token
        if not mint.strip() or len(mint) > 128:
            raise HTTPException(status_code=422, detail="Invalid token mint")
        trades = conn.execute(
            "SELECT wallet_address, signature, mint, token_name, token_symbol, side, "
            "token_amount, sol_amount, usd_amount, price_usd, market_cap_usd, "
            "occurred_at, source FROM wallet_trade_events WHERE mint=? "
            "ORDER BY occurred_at ASC LIMIT 10000",
            (mint,),
        ).fetchall()
        prices = conn.execute(
            "SELECT price_usd, market_cap_usd, observed_at, source "
            "FROM token_price_observations WHERE mint=? ORDER BY observed_at ASC LIMIT 10000",
            (mint,),
        ).fetchall()
        result = summarize_token([dict(row) for row in trades], [dict(row) for row in prices])
        result.update({
            "mint": mint,
            "token_name": next((row["token_name"] for row in trades if row["token_name"]), None),
            "token_symbol": next((row["token_symbol"] for row in trades if row["token_symbol"]), None),
            "trade_count": len(trades),
            "price_source_count": len({row["source"] for row in prices}),
        })
        return result

    @app.get("/v1/smart-money/wallet-rankings")
    def smart_money_wallet_rankings(
        leaderboard: str = "daily_pnl",
        limit: int = 50,
        _: Principal = Depends(require_read),
        conn=Depends(get_conn),
    ):
        """Return the latest stored daily, weekly, or callout leaderboard snapshot."""
        if leaderboard not in {"daily_pnl", "weekly_pnl", "callout"}:
            raise HTTPException(status_code=422, detail="leaderboard must be daily_pnl, weekly_pnl, or callout")
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
        rows = conn.execute(
            "SELECT r.wallet_address, w.label, r.leaderboard, r.rank, r.pnl_sol, "
            "r.observed_at, r.source FROM wallet_leaderboard_snapshots r "
            "LEFT JOIN tracked_wallets w ON w.wallet_address=r.wallet_address "
            "WHERE r.leaderboard=? AND r.observed_at=("
            "SELECT MAX(r2.observed_at) FROM wallet_leaderboard_snapshots r2 "
            "WHERE r2.leaderboard=r.leaderboard) "
            "ORDER BY CASE WHEN r.rank IS NULL THEN 1 ELSE 0 END, r.rank ASC LIMIT ?",
            (leaderboard, limit),
        ).fetchall()
        return {
            "leaderboard": leaderboard,
            "wallets": [dict(row) for row in rows],
            "count": len(rows),
            "note": "These are stored source snapshots. No leaderboard is represented as current until a fresh source snapshot has been ingested.",
        }

    @app.get("/v1/telegram/status")
    def telegram_status(_: Principal = Depends(require_read)):
        return telegram_alerts.status()

    @app.post("/v1/telegram/test")
    async def telegram_test(_: Principal = Depends(require_read)):
        try:
            return await telegram_alerts.send_test()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Telegram could not deliver the test message")

    @app.get("/v1/paper/bot")
    def paper_bot_status(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        return paper_bot.bot_status(conn)

    @app.post("/v1/paper/bot/config")
    def paper_bot_config(body: PaperBotConfigRequest, _: Principal = Depends(require_read), conn=Depends(get_conn)):
        try:
            return paper_bot.configure_bot(conn, body.enabled, body.allocated_capital_usd, body.duration_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/v1/paper/overview")
    def paper_overview(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        return paper.overview(conn)

    @app.get("/v1/paper/session")
    def paper_session(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        state = paper_bot._state(conn)
        return paper.session_overview(
            conn,
            state.get("run_started_at"),
            state.get("run_stopped_at"),
            bool(state.get("enabled")),
            state.get("run_session_id"),
        )

    @app.post("/v1/paper/orders")
    def paper_order(body: PaperOrderRequest, _: Principal = Depends(require_read), conn=Depends(get_conn)):
        try:
            return paper.place_order(conn, body.strategy, body.asset_id, body.token_name,
                                     body.side, body.amount_usd, body.reference_price, body.reason)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/v1/paper/mark")
    def paper_mark(body: PaperMarkRequest, _: Principal = Depends(require_read), conn=Depends(get_conn)):
        if len(body.prices) > 200:
            raise HTTPException(status_code=422, detail="Maximum 200 price marks per request")
        return paper.mark_positions(conn, body.prices)

    @app.get("/v1/paper/activity")
    def paper_activity(strategy: Optional[str] = None, limit: int = 100,
                       _: Principal = Depends(require_read), conn=Depends(get_conn)):
        try:
            return {"activity": paper.activity(conn, strategy, limit)}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

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
