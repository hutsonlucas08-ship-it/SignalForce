"""SignalForge Core: read-only research backend. No wallet, no signing, no order execution here."""
import asyncio
import hashlib
import secrets
import json
import logging
import os
import time
from urllib.parse import urlparse
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import store
from .auth import Principal, RateLimiter, _fp, require_admin, require_read
from .config import Settings, load_settings
from .db import connect, run_migrations, utcnow
from . import customer_auth, customer_data, customer_integrations
from .security import CredentialEncryptionError
from .risk import RiskSnapshot, assess_risk
from .research import analyze_asset, build_brain_report, scan_assets
from . import paper, paper_bot, customer_paper_bot, telegram_alerts, fomo_copilot, fomo_signals, whale_selection
from .sources.helius_worker import run_helius_sync

log = logging.getLogger("signalforge")
_price_history = {}
_bubble_cache = {}
_launch_cache = {}
_launch_retry_after = {}
_callout_market_cache = {}


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
    allocated_capital_usd: Optional[float] = Field(default=None, ge=5, le=100000000)
    duration_seconds: Optional[int] = None


class WalletWatchRequest(BaseModel):
    wallet_address: str = Field(min_length=32, max_length=44)
    label: Optional[str] = Field(default=None, max_length=80)

class BulkWalletWatchRequest(BaseModel):
    wallet_addresses: list[str] = Field(min_length=1, max_length=1000)


class CustomerRegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=12, max_length=1024)


class CustomerLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class CustomerActivateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class CustomerWalletRequest(BaseModel):
    wallet_address: str = Field(min_length=32, max_length=44)
    label: Optional[str] = Field(default=None, max_length=80)


class CustomerPaperAccountRequest(BaseModel):
    starting_capital_usd: float = Field(ge=5, le=100000000)
    name: str = Field(default="Main Paper Account", min_length=1, max_length=80)


class CustomerPaperTraderStartRequest(BaseModel):
    starting_capital_usd: float = Field(ge=5, le=100000000)
    profit_target_usd: float = Field(ge=1, le=100000000)
    mode: str = Field(pattern="^(gradual|normal|aggressive|ultra_aggressive)$")
    new_session: bool = False


class CustomerPaperTradeRequest(BaseModel):
    asset_id: str = Field(min_length=1, max_length=128)
    token_name: Optional[str] = Field(default=None, max_length=100)
    side: str = Field(min_length=3, max_length=4)
    amount_usd: float = Field(gt=0, le=10000000)
    reference_price: float = Field(gt=0, le=1e15)
    reason: str = Field(default="manual paper trade", min_length=1, max_length=200)


class CustomerIntegrationRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=20)
    credentials: dict[str, str]


class CustomerProviderRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=20)


class CustomerFomoSnapshotRequest(BaseModel):
    snapshot_type: str = Field(pattern="^(spot_swaps|clans_24h|clans_7d|clans_30d|trader_leaderboard|trader_leaderboard_24h|trader_leaderboard_7d|trader_leaderboard_30d)$")
    observed_at: Optional[str] = None
    payload: dict


class CustomerFomoPairRequest(BaseModel):
    pairing_code: str = Field(min_length=8, max_length=100)


class RiskScreenRequest(BaseModel):
    liquidity_usd: Optional[float] = Field(default=None, ge=0)
    top_10_holder_pct: Optional[float] = Field(default=None, ge=0, le=100)
    mint_authority_active: Optional[bool] = None
    freeze_authority_active: Optional[bool] = None
    data_age_seconds: Optional[float] = Field(default=None, ge=0)
    metadata_mutable: Optional[bool] = None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="SignalForce", version=settings.version)
    app.state.settings = settings
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.paper_bot_stop = asyncio.Event()
    app.state.paper_bot_task = None
    app.state.helius_sync_stop = asyncio.Event()
    app.state.helius_sync_task = None
    app.state.customer_paper_bot_stop = asyncio.Event()
    app.state.customer_paper_bot_task = None

    @app.on_event("startup")
    async def start_paper_bot():
        if settings.db_path != ":memory:":
            app.state.paper_bot_task = asyncio.create_task(
                paper_bot.run_paper_bot(settings.db_path, app.state.paper_bot_stop)
            )
            app.state.customer_paper_bot_task = asyncio.create_task(
                customer_paper_bot.run_customer_paper_bot(settings.db_path, settings.database_url, app.state.customer_paper_bot_stop)
            )
            log.warning("Customer paper trader background task scheduled")
            if os.getenv("HELIUS_API_KEY"):
                app.state.helius_sync_task = asyncio.create_task(
                    run_helius_sync(settings.db_path, app.state.helius_sync_stop)
                )

    @app.on_event("shutdown")
    async def stop_paper_bot():
        app.state.paper_bot_stop.set()
        app.state.helius_sync_stop.set()
        app.state.customer_paper_bot_stop.set()
        helius_task = app.state.helius_sync_task
        if helius_task is not None:
            try:
                await asyncio.wait_for(helius_task, timeout=25)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                helius_task.cancel()
        customer_task = app.state.customer_paper_bot_task
        if customer_task is not None:
            try:
                await asyncio.wait_for(customer_task, timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                customer_task.cancel()
        task = app.state.paper_bot_task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=6)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()

    # One shared connection for ":memory:" (tests); a fresh connection per request otherwise.
    shared = connect(settings.db_path, settings.database_url) if settings.db_path == ":memory:" else None
    boot = shared or connect(settings.db_path, settings.database_url)
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
        conn = connect(settings.db_path, settings.database_url)
        try:
            yield conn
        finally:
            conn.close()

    # Customer accounts use opaque HttpOnly cookies. These endpoints deliberately
    # expose only tenant-scoped data; the legacy operator dashboard remains API-key gated.
    app.state.customer_auth_limiter = RateLimiter(12)

    def require_customer(request: Request, conn=Depends(get_conn)):
        token = request.cookies.get("sf_customer_session")
        session = customer_auth.authenticate_customer_session(conn, token or "")
        if session is None:
            raise HTTPException(status_code=401, detail="Customer sign-in required")
        return session

    def require_customer_write(request: Request, session=Depends(require_customer)):
        if not customer_auth.verify_csrf_token(session, request.headers.get("x-csrf-token", "")):
            raise HTTPException(status_code=403, detail="CSRF validation failed")
        return session

    def customer_rate_limit(request: Request, action: str):
        host = request.client.host if request.client else "unknown"
        app.state.customer_auth_limiter.check(action + ":" + host)

    @app.post("/v1/customer/register", status_code=201)
    def customer_register(body: CustomerRegisterRequest, request: Request, response: Response, conn=Depends(get_conn)):
        """Create an account and sign the customer in immediately; no email delivery is required."""
        customer_rate_limit(request, "register")
        try:
            account_id = customer_auth.register_customer_account(conn, body.email, body.password)
            session = customer_auth.create_customer_session(conn, account_id)
        except customer_auth.CustomerAuthError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        email = body.email.strip().lower()
        secure_cookie = os.getenv("SIGNALFORCE_COOKIE_SECURE", "true").lower() != "false"
        response.set_cookie("sf_customer_session", session["session_token"], httponly=True,
                            secure=secure_cookie, samesite="strict", max_age=86400, path="/")
        return {"status": "active", "authenticated": True, "email": email,
                "csrf_token": session["csrf_token"], "expires_at": session["expires_at"],
                "message": "Account created. You're signed in to SignalForce."}

    @app.post("/v1/customer/login")
    def customer_login(body: CustomerLoginRequest, request: Request, response: Response, conn=Depends(get_conn)):
        customer_rate_limit(request, "login")
        email = body.email.strip().lower()
        row = conn.execute("SELECT id,password_hash,status FROM customer_accounts WHERE email=?", (email,)).fetchone()
        if row is None or not customer_auth.verify_password(body.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Email or password is incorrect")
        if row["status"] == "pending":
            raise HTTPException(status_code=403, detail="Verify your email using the link we sent before signing in.")
        if row["status"] != "active":
            raise HTTPException(status_code=403, detail="This account is unavailable")
        session = customer_auth.create_customer_session(conn, row["id"])
        secure_cookie = os.getenv("SIGNALFORCE_COOKIE_SECURE", "true").lower() != "false"
        response.set_cookie("sf_customer_session", session["session_token"], httponly=True,
                            secure=secure_cookie, samesite="strict", max_age=86400, path="/")
        return {"authenticated": True, "email": email, "csrf_token": session["csrf_token"],
                "expires_at": session["expires_at"]}

    @app.get("/v1/customer/me")
    def customer_me(session=Depends(require_customer)):
        return {"authenticated": True, "email": session["email"], "expires_at": session["expires_at"]}

    @app.get("/v1/customer/csrf")
    def customer_csrf(request: Request, session=Depends(require_customer), conn=Depends(get_conn)):
        """Issue a fresh CSRF token to the same-origin session after a page reload."""
        token = __import__("secrets").token_urlsafe(32)
        digest = customer_auth._token_hash(request.cookies.get("sf_customer_session", ""))
        conn.execute("UPDATE customer_sessions SET csrf_token_hash=? WHERE token_hash=? AND revoked_at IS NULL",
                     (customer_auth._token_hash(token), digest))
        conn.commit()
        return {"csrf_token": token}

    @app.post("/v1/customer/logout")
    def customer_logout(request: Request, response: Response, session=Depends(require_customer_write), conn=Depends(get_conn)):
        customer_auth.revoke_customer_session(conn, request.cookies.get("sf_customer_session", ""))
        response.delete_cookie("sf_customer_session", path="/", httponly=True, samesite="strict")
        return {"authenticated": False}

    @app.post("/v1/admin/customers/activate")
    def activate_customer(body: CustomerActivateRequest, _: Principal = Depends(require_admin), conn=Depends(get_conn)):
        cursor = conn.execute("UPDATE customer_accounts SET status='active', email_verified_at=?, updated_at=? WHERE LOWER(email)=LOWER(?) AND status='pending'",
                              (utcnow(), utcnow(), body.email.strip()))
        conn.commit()
        if cursor.rowcount != 1:
            raise HTTPException(status_code=404, detail="Pending customer account not found")
        return {"activated": True, "email": body.email.strip().lower()}

    @app.get("/v1/customer/wallets")
    def customer_wallets(session=Depends(require_customer), conn=Depends(get_conn)):
        return {"wallets": customer_data.list_wallets(conn, session["account_id"])}

    @app.post("/v1/customer/wallets")
    def customer_add_wallet(body: CustomerWalletRequest, session=Depends(require_customer_write), conn=Depends(get_conn)):
        try:
            wallet = customer_data.add_wallet(conn, session["account_id"], body.wallet_address, body.label)
            return {"wallet": wallet}
        except customer_data.CustomerDataError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.delete("/v1/customer/wallets/{wallet_address}")
    def customer_remove_wallet(wallet_address: str, session=Depends(require_customer_write), conn=Depends(get_conn)):
        if not customer_data.remove_wallet(conn, session["account_id"], wallet_address):
            raise HTTPException(status_code=404, detail="Wallet not found on this account")
        return {"removed": True}

    @app.get("/v1/customer/paper-trader")
    def customer_paper_trader(session=Depends(require_customer), conn=Depends(get_conn)):
        return {"paper_trader": customer_data.get_customer_paper_trader(conn, session["account_id"])}

    @app.post("/v1/customer/paper-trader/start")
    def customer_paper_trader_start(body: CustomerPaperTraderStartRequest,
                                    session=Depends(require_customer_write), conn=Depends(get_conn)):
        try:
            return {"paper_trader": customer_data.start_customer_paper_trader(
                conn, session["account_id"], body.starting_capital_usd,
                body.profit_target_usd, body.mode, new_session=body.new_session)}
        except customer_data.CustomerDataError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.post("/v1/customer/paper-trader/pause")
    def customer_paper_trader_pause(session=Depends(require_customer_write), conn=Depends(get_conn)):
        return {"paper_trader": customer_data.pause_customer_paper_trader(conn, session["account_id"])}

    @app.get("/v1/customer/paper-accounts")
    def customer_paper_accounts(session=Depends(require_customer), conn=Depends(get_conn)):
        return {"paper_accounts": customer_data.list_paper_accounts(conn, session["account_id"])}

    @app.post("/v1/customer/paper-accounts", status_code=201)
    def customer_create_paper_account(body: CustomerPaperAccountRequest, session=Depends(require_customer_write), conn=Depends(get_conn)):
        try:
            return {"paper_account": customer_data.create_paper_account(
                conn, session["account_id"], body.starting_capital_usd, body.name)}
        except customer_data.CustomerDataError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/v1/customer/paper-accounts/{paper_account_id}/positions")
    def customer_paper_positions(paper_account_id: str, session=Depends(require_customer), conn=Depends(get_conn)):
        try:
            return {"positions": customer_data.list_paper_positions(conn, session["account_id"], paper_account_id)}
        except customer_data.CustomerDataError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.get("/v1/customer/paper-accounts/{paper_account_id}/trades")
    def customer_paper_trades(paper_account_id: str, limit: int = 100, session=Depends(require_customer), conn=Depends(get_conn)):
        try:
            return {"trades": customer_data.list_paper_trades(conn, session["account_id"], paper_account_id, limit)}
        except customer_data.CustomerDataError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/v1/customer/paper-accounts/{paper_account_id}/trades", status_code=201)
    def customer_place_paper_trade(paper_account_id: str, body: CustomerPaperTradeRequest,
                                   session=Depends(require_customer_write), conn=Depends(get_conn)):
        try:
            return {"trade": customer_data.place_paper_trade(
                conn, session["account_id"], paper_account_id, asset_id=body.asset_id,
                token_name=body.token_name, side=body.side, amount_usd=body.amount_usd,
                price_usd=body.reference_price, reason=body.reason)}
        except customer_data.CustomerDataError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/v1/customer/integrations")
    def customer_integrations_status(session=Depends(require_customer), conn=Depends(get_conn)):
        return {"integrations": [customer_integrations.get_customer_integration_status(
            conn, session["account_id"], provider) for provider in sorted(customer_integrations.SUPPORTED_PROVIDERS)]}

    @app.post("/v1/customer/integrations")
    def customer_save_integration(body: CustomerIntegrationRequest, session=Depends(require_customer_write), conn=Depends(get_conn)):
        try:
            status = customer_integrations.save_customer_integration(
                conn, session["account_id"], body.provider, body.credentials)
            return {"integration": status}
        except customer_integrations.CustomerIntegrationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        except CredentialEncryptionError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

    @app.post("/v1/customer/integrations/test")
    async def customer_test_integration(body: CustomerProviderRequest, session=Depends(require_customer_write), conn=Depends(get_conn)):
        try:
            credentials = customer_integrations.load_customer_integration(conn, session["account_id"], body.provider)
            if not credentials:
                raise HTTPException(status_code=404, detail="Integration is not configured")
            async with httpx.AsyncClient(timeout=8.0) as client:
                if body.provider == "helius":
                    api_key = credentials.get("api_key", "")
                    response = await client.get("https://api.helius.xyz/v0/addresses/11111111111111111111111111111111/transactions",
                                                params={"api-key": api_key, "limit": 1})
                    if response.status_code in (401, 403):
                        raise customer_integrations.CustomerIntegrationError("invalid_credentials")
                    response.raise_for_status()
                elif body.provider == "telegram":
                    token, chat_id = credentials.get("bot_token", ""), credentials.get("chat_id", "")
                    if not token or not chat_id:
                        raise customer_integrations.CustomerIntegrationError("validation_failed")
                    response = await client.get("https://api.telegram.org/bot" + token + "/getMe")
                    payload = response.json()
                    if not response.is_success or not payload.get("ok"):
                        raise customer_integrations.CustomerIntegrationError("invalid_credentials")
                    sent = await client.post("https://api.telegram.org/bot" + token + "/sendMessage",
                                             json={"chat_id": chat_id, "text": "SignalForce connection test successful."})
                    if not sent.is_success or not sent.json().get("ok"):
                        raise customer_integrations.CustomerIntegrationError("validation_failed")
                else:
                    raise customer_integrations.CustomerIntegrationError("Unsupported integration provider.")
            status = customer_integrations.set_customer_integration_status(conn, session["account_id"], body.provider, "connected")
            return {"integration": status, "test": "passed"}
        except customer_integrations.CustomerIntegrationError as exc:
            code = str(exc) if str(exc) in customer_integrations.SAFE_ERROR_CODES else "validation_failed"
            try:
                customer_integrations.set_customer_integration_status(conn, session["account_id"], body.provider, "error", error_code=code)
            except customer_integrations.CustomerIntegrationError:
                pass
            raise HTTPException(status_code=422, detail="Integration test failed: " + code)
        except httpx.HTTPError:
            try:
                customer_integrations.set_customer_integration_status(conn, session["account_id"], body.provider, "error", error_code="provider_unavailable")
            except customer_integrations.CustomerIntegrationError:
                pass
            raise HTTPException(status_code=502, detail="The provider could not be reached")
        except CredentialEncryptionError:
            raise HTTPException(status_code=503, detail="Customer credential encryption is unavailable")

    @app.post("/v1/customer/integrations/telegram/discover")
    async def customer_discover_telegram_chat(session=Depends(require_customer_write), conn=Depends(get_conn)):
        """Discover the customer's private chat after they press Start on their own bot."""
        credentials = customer_integrations.load_customer_integration(conn, session["account_id"], "telegram")
        if not credentials or not credentials.get("bot_token"):
            raise HTTPException(status_code=404, detail="Save your Telegram bot token first.")
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.get("https://api.telegram.org/bot" + credentials["bot_token"] + "/getUpdates")
                payload = response.json()
            if not response.is_success or not payload.get("ok"):
                raise HTTPException(status_code=422, detail="Telegram could not read updates. Check your bot token and press Start in your bot.")
            chats = []
            for update in payload.get("result") or []:
                message = update.get("message") or update.get("my_chat_member") or {}
                chat = message.get("chat") or {}
                if chat.get("id") is not None:
                    chats.append(str(chat["id"]))
            if not chats:
                raise HTTPException(status_code=404, detail="No Telegram chat found yet. Open your bot, press Start, send it a message, then try again.")
            credentials["chat_id"] = chats[-1]
            status = customer_integrations.save_customer_integration(conn, session["account_id"], "telegram", credentials)
            return {"chat_id": chats[-1], "integration": status}
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Telegram could not be reached. Try again shortly.")

    @app.delete("/v1/customer/integrations/{provider}")
    def customer_disconnect_integration(provider: str, session=Depends(require_customer_write), conn=Depends(get_conn)):
        try:
            return {"disconnected": customer_integrations.disconnect_customer_integration(conn, session["account_id"], provider)}
        except customer_integrations.CustomerIntegrationError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

    @app.get("/v1/customer/fomo-copilot")
    def customer_fomo_copilot(session=Depends(require_customer), conn=Depends(get_conn)):
        snapshots = fomo_copilot.list_snapshots(conn, session["account_id"])
        now = utcnow()
        bridge = conn.execute(
            "SELECT bridge_expires_at FROM customer_fomo_bridge_pairs WHERE account_id=?",
            (session["account_id"],),
        ).fetchone()
        paired = bool(bridge and bridge["bridge_expires_at"] and bridge["bridge_expires_at"] > now)
        return {
            "connected": bool(snapshots),
            "bridge_paired": paired,
            "snapshot_count": len(snapshots),
            "snapshots": snapshots,
            "mode": "read_only_manual",
            "execution_enabled": False,
        }

    async def _customer_callout_data(account_id, conn, limit):
        assets = scan_assets(conn, limit)
        known_mints = {item.get("asset_id") for item in assets}
        # Add newly created public Solana pools so the scanner does not depend
        # solely on historical wallet observations being present in the DB.
        launch_data = await research_new_launches()
        launch_rows = launch_data.get("launches") or []
        for launch in (launch_rows if not launch_data.get("stale") else []):
            mint = launch.get("asset_id")
            if isinstance(mint, str) and mint and mint not in known_mints:
                assets.append({
                    "asset_id": mint,
                    "token_name": launch.get("name") or launch.get("symbol") or mint[:8],
                    "latest_collected_at": utcnow(),
                    "discovery_source": "GeckoTerminal new pools",
                })
                known_mints.add(mint)
        mints = [item["asset_id"] for item in assets if isinstance(item.get("asset_id"), str)]
        cache_key = tuple(sorted(set(mints)))
        cached_market = _callout_market_cache.get(cache_key)
        if cached_market and time.time() - cached_market[0] < 30:
            pairs = cached_market[1]
        else:
            pairs = []
            async with httpx.AsyncClient(timeout=6.0) as client:
                for offset in range(0, len(mints), 30):
                    batch = mints[offset:offset + 30]
                    if not batch:
                        continue
                    try:
                        response = await client.get(
                            "https://api.dexscreener.com/tokens/v1/solana/" + ",".join(batch),
                            headers={"Accept": "application/json"},
                        )
                        response.raise_for_status()
                        payload = response.json()
                        if isinstance(payload, list):
                            pairs.extend(payload)
                    except (httpx.HTTPError, ValueError):
                        log.warning("Customer callout market-data request failed")
            _callout_market_cache[cache_key] = (time.time(), pairs)
            if len(_callout_market_cache) > 64:
                oldest_key = min(_callout_market_cache, key=lambda key: _callout_market_cache[key][0])
                _callout_market_cache.pop(oldest_key, None)
        result = fomo_signals.build_callouts(assets, pairs)
        result["gecko_terminal_new_pools_seen"] = len(launch_rows)
        snapshots = fomo_copilot.list_snapshots(conn, account_id)
        spot = next((item for item in snapshots if item["snapshot_type"] == "spot_swaps"), None)
        swaps = ((spot or {}).get("payload") or {}).get("swaps") or []
        own_activity = {}
        for swap in swaps:
            if not isinstance(swap, dict):
                continue
            side = swap.get("display", {}).get("side") if isinstance(swap.get("display"), dict) else swap.get("display")
            timestamp = swap.get("createdAt")
            for field in ("inTokenAddress", "outTokenAddress"):
                mint = swap.get(field)
                if not isinstance(mint, str) or not mint:
                    continue
                entry = own_activity.setdefault(mint, {"swap_count": 0, "latest_swap_at": None, "sides": []})
                entry["swap_count"] += 1
                if isinstance(timestamp, str) and (
                    entry["latest_swap_at"] is None or timestamp > entry["latest_swap_at"]
                ):
                    entry["latest_swap_at"] = timestamp
                if isinstance(side, str) and side.lower() in {"buy", "sell"} and side.lower() not in entry["sides"]:
                    entry["sides"].append(side.lower())
        for item in result["callouts"]:
            activity = own_activity.get(item["asset_id"])
            if activity:
                item["your_fomo_activity"] = activity
        trader_snapshots = [
            item for item in snapshots
            if item["snapshot_type"].startswith("trader_leaderboard")
        ]
        trader_rows = sum(
            len(((item.get("payload") or {}).get("traders") or []))
            for item in trader_snapshots
        )
        whale_gate = whale_selection.qualify_traders(trader_snapshots)
        result["whale_selection"] = whale_gate
        result["fomo_context"] = {
            "connected_spot_swap_snapshot": bool(spot),
            "observed_spot_swaps": len(swaps),
            "observed_at": (spot or {}).get("observed_at"),
            "tokens_with_your_activity": len(own_activity),
            "trader_leaderboard_captured": bool(trader_snapshots),
            "trader_leaderboard_rows": trader_rows,
            "trader_leaderboard_windows": sorted(
                item["snapshot_type"].removeprefix("trader_leaderboard_")
                for item in trader_snapshots
            ),
            "leaderboard_qualified_traders": whale_gate["qualified_count"],
            "interpretation": "Your imported swaps are personal account history. Trader leaderboard rows are captured from FOMO's page but still require performance and identity validation before whale signals can be generated.",
        }
        return result

    @app.get("/v1/customer/fomo-copilot/callouts")
    async def customer_fomo_callouts(
        limit: int = 50,
        session=Depends(require_customer),
        conn=Depends(get_conn),
    ):
        """Generate manual-review candidates from fresh, observed Solana assets."""
        if limit < 1 or limit > 50:
            raise HTTPException(status_code=422, detail="Limit must be between 1 and 50")
        return await _customer_callout_data(session["account_id"], conn, limit)

    @app.post("/v1/customer/fomo-copilot/callouts/telegram")
    async def customer_fomo_callouts_telegram(
        session=Depends(require_customer_write),
        conn=Depends(get_conn),
    ):
        """Send the user-requested top callouts to their own connected Telegram bot."""
        credentials = customer_integrations.load_customer_integration(
            conn, session["account_id"], "telegram"
        )
        if not credentials or not credentials.get("bot_token") or not credentials.get("chat_id"):
            raise HTTPException(status_code=404, detail="Connect and test your own Telegram bot first.")
        integration_status = customer_integrations.get_customer_integration_status(
            conn, session["account_id"], "telegram"
        )
        if integration_status.get("status") != "connected":
            raise HTTPException(status_code=409, detail="Test your Telegram connection before sending callouts.")
        data = await _customer_callout_data(session["account_id"], conn, 50)
        rows = data["callouts"][:5]
        if not rows:
            raise HTTPException(status_code=409, detail="No candidates passed the current scan filters.")
        lines = ["SignalForce · Manual Solana watchlist", "Research only · no orders placed"]
        for item in rows:
            lines.extend([
                "",
                "%s ($%s) · %s/100" % (
                    item["name"], item["symbol"] or "—", item["score"]
                ),
                "Price: $%s · Liquidity: $%s · 1h volume: $%s" % (
                    item["price_usd"], round(item["liquidity_usd"]), round(item["volume_1h_usd"])
                ),
                "5m buys/sells: %s/%s" % (item["buys_5m"], item["sells_5m"]),
                item["market_url"],
                "FOMO app: " + item["fomo_phone_url"],
                "FOMO web: " + item["fomo_web_url"],
                "Token: " + item["asset_id"],
            ])
        lines.append("\nNo entry/stop/target is supplied. Verify token safety and slippage yourself.")
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                response = await client.post(
                    "https://api.telegram.org/bot" + credentials["bot_token"] + "/sendMessage",
                    json={"chat_id": credentials["chat_id"], "text": "\n".join(lines)[:3900]},
                )
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                if not response.is_success or not payload.get("ok"):
                    raise HTTPException(status_code=502, detail="Telegram did not accept the callout message.")
        except httpx.HTTPError:
            raise HTTPException(status_code=502, detail="Telegram could not be reached.")
        return {"sent": True, "count": len(rows), "destination": "your connected Telegram"}
 
    @app.post("/v1/customer/fomo-copilot/pairing")
    def customer_fomo_copilot_pairing(session=Depends(require_customer_write), conn=Depends(get_conn)):
        pairing_code = secrets.token_urlsafe(12)
        now = utcnow()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "INSERT INTO customer_fomo_bridge_pairs "
            "(account_id, pairing_hash, pairing_expires_at, bridge_hash, bridge_expires_at, updated_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET pairing_hash=excluded.pairing_hash, "
            "pairing_expires_at=excluded.pairing_expires_at, bridge_hash=NULL, "
            "bridge_expires_at=NULL, updated_at=excluded.updated_at",
            (session["account_id"], hashlib.sha256(pairing_code.encode()).hexdigest(), expires, now),
        )
        conn.commit()
        return {"pairing_code": pairing_code, "expires_at": expires, "expires_in_seconds": 300}

    @app.post("/v1/customer/fomo-copilot/pair")
    def customer_fomo_copilot_pair(body: CustomerFomoPairRequest, conn=Depends(get_conn)):
        now = utcnow()
        supplied_hash = hashlib.sha256(body.pairing_code.encode()).hexdigest()
        matched = conn.execute(
            "SELECT account_id FROM customer_fomo_bridge_pairs "
            "WHERE pairing_hash=? AND pairing_expires_at>?",
            (supplied_hash, now),
        ).fetchone()
        if not matched:
            raise HTTPException(status_code=401, detail="Pairing code is invalid or expired. Generate a new code in SignalForce.")
        bridge_key = secrets.token_urlsafe(32)
        expires = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE customer_fomo_bridge_pairs SET pairing_hash='', pairing_expires_at=?, "
            "bridge_hash=?, bridge_expires_at=?, updated_at=? WHERE account_id=?",
            (now, hashlib.sha256(bridge_key.encode()).hexdigest(), expires, now, matched["account_id"]),
        )
        conn.commit()
        return {"bridge_key": bridge_key, "expires_at": expires}

    @app.post("/v1/customer/fomo-copilot/snapshot")
    def customer_fomo_copilot_snapshot(
        body: CustomerFomoSnapshotRequest,
        session=Depends(require_customer_write),
        conn=Depends(get_conn),
    ):
        try:
            encoded = fomo_copilot.validate_snapshot(body.snapshot_type, body.payload)
            observed_at = fomo_copilot.parse_observed_at(body.observed_at)
        except fomo_copilot.FomoSnapshotError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        now = utcnow()
        conn.execute(
            "INSERT INTO customer_fomo_snapshots "
            "(account_id, snapshot_type, payload_json, source, observed_at, created_at) "
            "VALUES (?, ?, ?, 'fomo-browser-bridge', ?, ?) "
            "ON CONFLICT(account_id, snapshot_type) DO UPDATE SET "
            "payload_json=excluded.payload_json, source=excluded.source, "
            "observed_at=excluded.observed_at, created_at=excluded.created_at",
            (session["account_id"], body.snapshot_type, encoded, observed_at, now),
        )
        conn.commit()
        return {"saved": True, "snapshot_type": body.snapshot_type, "observed_at": observed_at}

    @app.post("/v1/customer/fomo-copilot/bridge-snapshot")
    def customer_fomo_copilot_bridge_snapshot(
        body: CustomerFomoSnapshotRequest,
        request: Request,
        conn=Depends(get_conn),
    ):
        bridge_key = request.headers.get("X-Fomo-Bridge-Key", "")
        if not bridge_key:
            raise HTTPException(status_code=401, detail="Local bridge key required")
        supplied_hash = hashlib.sha256(bridge_key.encode()).hexdigest()
        row = conn.execute(
            "SELECT account_id FROM customer_fomo_bridge_pairs "
            "WHERE bridge_hash=? AND bridge_expires_at>?",
            (supplied_hash, utcnow()),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=401, detail="Local bridge key is invalid or expired")
        try:
            encoded = fomo_copilot.validate_snapshot(body.snapshot_type, body.payload)
            observed_at = fomo_copilot.parse_observed_at(body.observed_at)
        except fomo_copilot.FomoSnapshotError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        now = utcnow()
        conn.execute(
            "INSERT INTO customer_fomo_snapshots "
            "(account_id, snapshot_type, payload_json, source, observed_at, created_at) "
            "VALUES (?, ?, ?, 'fomo-local-bridge', ?, ?) "
            "ON CONFLICT(account_id, snapshot_type) DO UPDATE SET "
            "payload_json=excluded.payload_json, source=excluded.source, "
            "observed_at=excluded.observed_at, created_at=excluded.created_at",
            (row["account_id"], body.snapshot_type, encoded, observed_at, now),
        )
        conn.commit()
        return {"saved": True, "snapshot_type": body.snapshot_type, "observed_at": observed_at}

    @app.delete("/v1/customer/fomo-copilot")
    def customer_fomo_copilot_disconnect(
        session=Depends(require_customer_write),
        conn=Depends(get_conn),
    ):
        conn.execute("DELETE FROM customer_fomo_snapshots WHERE account_id=?", (session["account_id"],))
        conn.execute("DELETE FROM customer_fomo_bridge_pairs WHERE account_id=?", (session["account_id"],))
        conn.commit()
        return {"disconnected": True}

    @app.get("/account", include_in_schema=False)
    def customer_workspace():
        """Serve the separate customer account and paper-trading workspace."""
        return FileResponse(Path(__file__).parent / "static" / "customer.html")

    @app.get("/terminal", include_in_schema=False)
    def public_terminal():
        """Serve the customer-facing paper-trading terminal without operator API keys."""
        return FileResponse(Path(__file__).parent / "static" / "customer.html")

    @app.get("/operator", include_in_schema=False)
    def operator_terminal():
        """Serve the legacy operator research terminal; its APIs remain read-key gated."""
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.get("/", include_in_schema=False)
    def customer_home():
        """Public customer sign-in and paper-trading entry point."""
        return FileResponse(Path(__file__).parent / "static" / "customer.html")

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
    async def research_new_launches():
        """Return the newest Solana pools and token artwork from public GeckoTerminal data."""
        now = time.time()
        cached = _launch_cache.get("solana")
        if cached and now - cached[0] < 45:
            return cached[1]
        if now < _launch_retry_after.get("solana", 0):
            if cached:
                stale = dict(cached[1])
                stale["stale"] = True
                stale["note"] = "Showing cached launch data while GeckoTerminal recovers."
                return stale
            return {"launches": [], "count": 0, "source": "fallback", "stale": True,
                    "note": "GeckoTerminal is temporarily unavailable; wallet and market discovery continue."}
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
            _launch_retry_after["solana"] = 0
            return result
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            retry_after = 300 if status == 429 else 90 if status >= 500 else 180
            _launch_retry_after["solana"] = now + retry_after
            log.warning("GeckoTerminal new-pools feed unavailable (HTTP %s); retrying in %ss", status, retry_after)
            if cached:
                stale = dict(cached[1])
                stale["stale"] = True
                stale["note"] = "Showing cached launch data while GeckoTerminal recovers."
                return stale
            return {"launches": [], "count": 0, "source": "fallback", "stale": True,
                    "note": "GeckoTerminal is temporarily unavailable; wallet and market discovery continue."}
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            _launch_retry_after["solana"] = now + 120
            log.warning("GeckoTerminal new-pools feed unavailable (%s); retrying in 120s",
                        type(exc).__name__)
            if cached:
                stale = dict(cached[1])
                stale["stale"] = True
                stale["note"] = "Showing cached launch data while GeckoTerminal recovers."
                return stale
            return {"launches": [], "count": 0, "source": "fallback", "stale": True,
                    "note": "GeckoTerminal is temporarily unavailable; wallet and market discovery continue."}

    @app.get("/v1/research/trending-now")
    async def trending_now(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        """Rank current Solana trending pools from GeckoTerminal, enriched by DEX Screener."""
        now = time.time()
        cached = _launch_cache.get("trending")
        if cached and now - cached[0] < 30:
            return cached[1]
        try:
            async with httpx.AsyncClient(timeout=8.0, headers={"Accept": "application/json"}) as client:
                response = await client.get(
                    "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
                    params={"include": "base_token,quote_token,dex", "page": 1},
                )
                response.raise_for_status()
                payload = response.json()
                included = {
                    item.get("id"): item.get("attributes") or {}
                    for item in (payload.get("included") or [])
                    if isinstance(item, dict) and item.get("type") == "token"
                }
                stable = {
                    "So11111111111111111111111111111111111111112",
                    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
                }
                tokens = {}
                for pool in payload.get("data") or []:
                    if not isinstance(pool, dict):
                        continue
                    attrs = pool.get("attributes") or {}
                    rel = pool.get("relationships") or {}
                    base_ref = ((rel.get("base_token") or {}).get("data") or {})
                    quote_ref = ((rel.get("quote_token") or {}).get("data") or {})
                    base = included.get(base_ref.get("id"), {})
                    quote = included.get(quote_ref.get("id"), {})
                    token = quote if base.get("address") in stable else base
                    mint = token.get("address")
                    if not isinstance(mint, str) or not mint:
                        continue
                    token_image = token.get("image_url") or token.get("image")
                    if not isinstance(token_image, str) or not token_image.startswith("https://"):
                        token_image = None
                    tokens.setdefault(mint, {
                        "asset_id": mint,
                        "name": token.get("name") or token.get("symbol") or mint[:8],
                        "symbol": token.get("symbol") or "",
                        "image_url": token_image,
                        "price_usd": attrs.get("base_token_price_usd"),
                        "liquidity_usd": attrs.get("reserve_in_usd"),
                        "volume_5m_usd": (attrs.get("volume_usd") or {}).get("m5"),
                        "volume_1h_usd": (attrs.get("volume_usd") or {}).get("h1"),
                        "volume_24h_usd": (attrs.get("volume_usd") or {}).get("h24"),
                        "price_change_1h_pct": (attrs.get("price_change_percentage") or {}).get("h1"),
                        "price_change_24h_pct": (attrs.get("price_change_percentage") or {}).get("h24"),
                        "pool_address": (pool.get("id") or "").split("_", 1)[-1],
                        "source": "GeckoTerminal",
                    })
                mints = list(tokens)
                pairs = await paper_bot._fetch_pairs(client, mints) if mints else {}
            candidates = []
            for mint, item in tokens.items():
                pair = pairs.get(mint) or {}
                base = pair.get("baseToken") or {}
                info = pair.get("info") or {}
                txns = pair.get("txns") or {}
                volume = pair.get("volume") or {}
                changes = pair.get("priceChange") or {}
                liquidity = (pair.get("liquidity") or {}).get("usd")
                liquidity = liquidity if liquidity is not None else item.get("liquidity_usd")
                volume5 = volume.get("m5") if volume.get("m5") is not None else item.get("volume_5m_usd")
                volume1 = volume.get("h1") if volume.get("h1") is not None else item.get("volume_1h_usd")
                volume24 = volume.get("h24") if volume.get("h24") is not None else item.get("volume_24h_usd")
                change1 = changes.get("h1") if changes.get("h1") is not None else item.get("price_change_1h_pct")
                change24 = changes.get("h24") if changes.get("h24") is not None else item.get("price_change_24h_pct")
                buys5 = (txns.get("m5") or {}).get("buys")
                sells5 = (txns.get("m5") or {}).get("sells")
                # Broad discovery ranking: never hide a currently trending pool merely
                # because one optional metric is unavailable from a provider.
                score = 0.0
                score += min(25.0, max(0.0, float(liquidity or 0)) / 10000 * 5)
                score += min(25.0, max(0.0, float(volume5 or 0)) / 1000 * 5)
                score += min(20.0, max(0.0, float(volume1 or 0)) / 10000 * 5)
                score += min(15.0, max(0.0, float(buys5 or 0)) * 1.5)
                score += min(15.0, max(0.0, float(change1 or 0)) * 0.5)
                image_url = info.get("imageUrl") or item.get("image_url")
                if not isinstance(image_url, str) or not image_url.startswith("https://"):
                    image_url = None
                candidates.append({
                    **item,
                    "name": item.get("name") or base.get("name") or mint[:8],
                    "symbol": item.get("symbol") or base.get("symbol") or "",
                    "image_url": image_url,
                    "score": round(score, 2),
                    "price_usd": pair.get("priceUsd") or item.get("price_usd"),
                    "market_cap_usd": pair.get("marketCap") or pair.get("fdv"),
                    "liquidity_usd": liquidity,
                    "volume_5m_usd": volume5,
                    "volume_1h_usd": volume1,
                    "volume_24h_usd": volume24,
                    "buys_5m": buys5,
                    "sells_5m": sells5,
                    "price_change_5m_pct": changes.get("m5"),
                    "price_change_1h_pct": change1,
                    "price_change_24h_pct": change24,
                    "source": "GeckoTerminal + DEX Screener" if pair else "GeckoTerminal",
                })
            candidates.sort(key=lambda row: row["score"], reverse=True)
            result = {
                "candidates": candidates[:20],
                "scanned": len(mints),
                "priced": len(pairs),
                "rejected": max(0, len(mints) - len(candidates)),
                "source": "GeckoTerminal trending Solana pools, enriched with DEX Screener market data",
                "note": "Market-wide trending-pool discovery; missing optional market metrics do not remove a token from the list. Rankings are informational, not a safety or profitability guarantee.",
            }
            _launch_cache["trending"] = (now, result)
            return result
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
            log.warning("GeckoTerminal trending feed unavailable")
            raise HTTPException(status_code=502, detail="GeckoTerminal's Solana trending feed is temporarily unavailable")

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
        conn.execute("INSERT INTO tracked_wallets(wallet_address,label,added_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",
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
                "INSERT INTO tracked_wallets(wallet_address,label,added_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",
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
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "SELECT wallet_address, signature, mint, token_name, token_symbol, side, "
            "price_usd, market_cap_usd, occurred_at, source "
            "FROM wallet_trade_events WHERE side='buy' "
            "AND occurred_at >= ? "
            "ORDER BY occurred_at DESC LIMIT 10000",
            (cutoff,),
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

    @app.get("/v1/paper/live-prices")
    async def paper_live_prices(_: Principal = Depends(require_read), conn=Depends(get_conn)):
        """Fetch fresh display-only quotes for currently open automatic paper positions."""
        rows = conn.execute(
            "SELECT asset_id FROM paper_positions WHERE account_id='paper-automatic' ORDER BY opened_at DESC LIMIT 50"
        ).fetchall()
        mints = [row["asset_id"] for row in rows]
        observed_at = utcnow()
        if not mints:
            return {"prices": {}, "market_caps": {}, "images": {}, "observed_at": observed_at, "source": "DexScreener", "position_count": 0}
        async with httpx.AsyncClient(timeout=4.0, headers={"Accept": "application/json"}) as client:
            pairs = await paper_bot._fetch_pairs(client, mints)
        prices = {}
        market_caps = {}
        images = {}
        for mint, pair in pairs.items():
            info = pair.get("info") or {}
            image_url = info.get("imageUrl")
            if isinstance(image_url, str) and image_url.startswith("https://"):
                images[mint] = image_url
            try:
                price = float(pair.get("priceUsd"))
                if price > 0:
                    prices[mint] = price
            except (TypeError, ValueError):
                pass
            # Only label an explicit provider marketCap as market cap.
            # FDV is not interchangeable and must not be presented as MC.
            try:
                market_cap = float(pair.get("marketCap"))
                if market_cap > 0:
                    market_caps[mint] = market_cap
            except (TypeError, ValueError):
                pass
        return {
            "prices": prices,
            "market_caps": market_caps,
            "images": images,
            "observed_at": utcnow(),
            "source": "DexScreener",
            "position_count": len(mints),
            "priced_count": len(prices),
            "market_cap_count": len(market_caps),
            "note": "Live display-only price and market-cap quotes. Market cap is shown only when the provider supplies it; FDV is not substituted. Automated paper exit rules still run on the bot's 30-second strategy cycle.",
        }

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
