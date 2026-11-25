"""Read-only J7Tracker live X callout feed.

Only consumes posts and metadata. It never invokes J7 trade/deploy endpoints.
"""
from __future__ import annotations

import asyncio
import re
import time
import json
from pathlib import Path
from collections import OrderedDict
from datetime import datetime, timezone

from .fomo_signals import is_solana_mint

_EVENTS: OrderedDict[str, dict] = OrderedDict()
_PENDING_META: dict[str, list[dict]] = {}
_STATUS = {
    "configured": False,
    "connected": False,
    "upstream_connected": None,
    "state": "not_configured",
    "last_event_at": None,
    "last_error": None,
}
_MAX_EVENTS = 500
_WATCHLIST_DATA = json.loads(Path(__file__).with_name("axiom_x_watchlist.json").read_text(encoding="utf-8"))
_WATCHLIST = {row["handle"].lower(): row for row in _WATCHLIST_DATA.get("accounts", [])}
_ALLOWED_POST_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}


def _watchlist_row(handle: str) -> dict | None:
    return _WATCHLIST.get(str(handle or "").lstrip("@").lower())


def watchlist_summary() -> dict:
    return {"account_count": len(_WATCHLIST), "tweets_enabled": sum(bool(x.get("tweets")) for x in _WATCHLIST.values()), "profile_updates_enabled": sum(bool(x.get("profile_updates")) for x in _WATCHLIST.values()), "follows_enabled": sum(bool(x.get("follows")) for x in _WATCHLIST.values())}


def _post_url(item: dict) -> str | None:
    url = item.get("tweetUrl") or item.get("url")
    if isinstance(url, str):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.hostname in _ALLOWED_POST_HOSTS:
                return url[:2048]
        except ValueError:
            pass
    post_id = str(item.get("id") or "")
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    handle = str(author.get("handle") or "").lstrip("@")
    if post_id.isdigit() and re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return f"https://x.com/{handle}/status/{post_id}"
    return None


def _contracts(item: dict) -> list[str]:
    values = []
    for key in ("contractAddress", "mint", "address"):
        value = item.get(key)
        if is_solana_mint(value) and value not in values:
            values.append(value)
    extras = item.get("extraContractAddresses")
    if isinstance(extras, list):
        for value in extras:
            if is_solana_mint(value) and value not in values:
                values.append(value)
    metas = item.get("token_meta") or item.get("tokenMetas") or []
    if isinstance(metas, list):
        for meta in metas:
            if isinstance(meta, dict):
                value = meta.get("mint")
                if is_solana_mint(value) and value not in values:
                    values.append(value)
    return values[:5]


def _normalize(item: dict, kind: str = "x_post") -> dict | None:
    if not isinstance(item, dict):
        return None
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    webhook = item.get("webhook") if isinstance(item.get("webhook"), dict) else {}
    handle = str(author.get("handle") or "").lstrip("@")[:80]
    if kind == "x_post" and not _watchlist_row(handle):
        return None
    text = item.get("text") or webhook.get("content") or ""
    if not isinstance(text, str):
        text = str(text)
    post_id = str(item.get("id") or "")
    if not post_id:
        return None
    created = item.get("createdAt") or item.get("timestamp")
    if isinstance(created, (int, float)):
        created = datetime.fromtimestamp(created / 1000 if created > 10_000_000_000 else created, timezone.utc).isoformat()
    elif not isinstance(created, str):
        created = None
    contracts = _contracts(item)
    if not contracts and kind == "webhook":
        embeds = webhook.get("embeds") if isinstance(webhook.get("embeds"), list) else []
        for embed in embeds:
            if isinstance(embed, dict) and is_solana_mint(embed.get("address")):
                contracts.append(embed["address"])
    return {
        "id": post_id[:180],
        "kind": kind,
        "platform": "X" if kind == "x_post" else "J7 launch alert",
        "author": handle or str(author.get("name") or webhook.get("displayName") or "J7Tracker")[:80],
        "author_name": str(author.get("name") or webhook.get("displayName") or "")[:120],
        "author_avatar": author.get("avatar") if isinstance(author.get("avatar"), str) else None,
        "text": text[:6000],
        "url": _post_url(item),
        "created_at": created,
        "contracts": contracts,
        "verified": bool(author.get("verified")),
        "followers": author.get("followersCount") if isinstance(author.get("followersCount"), (int, float)) else None,
        "likes": (item.get("metrics") or {}).get("likes") if isinstance(item.get("metrics"), dict) else None,
        "retweets": (item.get("metrics") or {}).get("retweets") if isinstance(item.get("metrics"), dict) else None,
        "replies": (item.get("metrics") or {}).get("replies") if isinstance(item.get("metrics"), dict) else None,
        "source": "J7Tracker",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _upsert(item: dict, kind: str = "x_post") -> None:
    normalized = _normalize(item, kind)
    if not normalized:
        return
    post_id = normalized["id"]
    previous = _EVENTS.get(post_id, {})
    # Keep useful fields from the first fast copy when later patches omit them.
    merged = {**previous, **{k: v for k, v in normalized.items() if v not in (None, "", [])}}
    pending = _PENDING_META.pop(post_id, [])
    merged["token_meta"] = (previous.get("token_meta") or []) + pending
    merged["contracts"] = list(dict.fromkeys((previous.get("contracts") or []) + (merged.get("contracts") or [])))
    for meta in merged["token_meta"]:
        if isinstance(meta, dict) and is_solana_mint(meta.get("mint")) and meta["mint"] not in merged["contracts"]:
            merged["contracts"].append(meta["mint"])
    _EVENTS[post_id] = merged
    _EVENTS.move_to_end(post_id, last=False)
    while len(_EVENTS) > _MAX_EVENTS:
        _EVENTS.popitem(last=True)
    _STATUS["last_event_at"] = merged["observed_at"]


def _token_meta(payload: dict) -> None:
    if not isinstance(payload, dict):
        return
    parent_id = payload.get("id")
    meta = payload.get("tokenMeta") if isinstance(payload.get("tokenMeta"), dict) else {}
    mint = payload.get("mint") or meta.get("mint")
    if not is_solana_mint(mint):
        return
    clean = {
        "mint": mint,
        "symbol": str(meta.get("symbol") or "")[:32],
        "name": str(meta.get("name") or "")[:100],
        "image": meta.get("image") if isinstance(meta.get("image"), str) else None,
        "market": str(meta.get("market") or "")[:40],
        "market_cap_usd": meta.get("marketCapUsd"),
    }
    if parent_id and str(parent_id) in _EVENTS:
        item = _EVENTS[str(parent_id)]
        item.setdefault("token_meta", []).append(clean)
        if mint not in item.setdefault("contracts", []):
            item["contracts"].append(mint)
    elif parent_id:
        _PENDING_META.setdefault(str(parent_id), []).append(clean)


def snapshot(limit: int = 100) -> dict:
    return {
        **_STATUS,
        "events": list(_EVENTS.values())[:max(1, min(int(limit), 200))],
        "event_count": len(_EVENTS),
        "watchlist": watchlist_summary(),
        "source": "J7Tracker live X feed",
        "read_only": True,
        "execution_enabled": False,
    }


async def run_feed(stop_event: asyncio.Event, jwt: str) -> None:
    """Maintain the J7 Socket.IO feed while the Render service is running."""
    _STATUS.update({"configured": True, "state": "connecting", "last_error": None})
    try:
        import socketio
    except ImportError:
        _STATUS.update({"state": "dependency_missing", "last_error": "python-socketio client is not installed"})
        return

    client = socketio.AsyncClient(reconnection=True, logger=False, engineio_logger=False)

    @client.event
    async def connect():
        _STATUS.update({"connected": True, "state": "connected", "last_error": None})
        await client.emit("user_connected", jwt)

    @client.event
    async def disconnect():
        _STATUS.update({"connected": False, "state": "reconnecting"})

    @client.on("initialTweets")
    async def initial_tweets(items):
        _EVENTS.clear()
        for item in (reversed(items) if isinstance(items, list) else []):
            if isinstance(item, dict) and item.get("isWebhook"):
                _upsert(item, "webhook")
            elif isinstance(item, dict) and item.get("source") != "external":
                _upsert(item)

    @client.on("tweet")
    async def tweet(item):
        _upsert(item)

    @client.on("tweet_update")
    async def tweet_update(item):
        _upsert(item)

    @client.on("external_message")
    async def external_message(item):
        if isinstance(item, dict) and item.get("isWebhook"):
            _upsert(item, "webhook")

    @client.on("following_update")
    async def following_update(item):
        if not isinstance(item, dict):
            return
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        followed = item.get("following") if isinstance(item.get("following"), dict) else {}
        handle = str(user.get("handle") or "").lstrip("@")
        row = _watchlist_row(handle)
        if not row or not row.get("follows"):
            return
        target = str(followed.get("handle") or "an account").lstrip("@")
        event_id = str(item.get("id") or f"follow:{handle}:{target}:{time.time_ns()}")
        now = datetime.now(timezone.utc).isoformat()
        _EVENTS[event_id] = {"id": event_id, "kind": "follow_update", "platform": "X", "author": handle, "author_name": str(user.get("name") or handle), "text": f"@{handle} followed @{target}", "url": f"https://x.com/{handle}", "created_at": now, "contracts": [], "source": "J7Tracker", "observed_at": now}
        _EVENTS.move_to_end(event_id, last=False)
        while len(_EVENTS) > _MAX_EVENTS:
            _EVENTS.popitem(last=True)

    @client.on("follow_scan")
    async def follow_scan(item):
        if not isinstance(item, dict):
            return
        event_id = str(item.get("id") or "")
        event = _EVENTS.get(event_id)
        if not event or event.get("kind") != "follow_update":
            return
        scan = item.get("scan") if isinstance(item.get("scan"), dict) else {}
        contracts = scan.get("contracts") if isinstance(scan.get("contracts"), dict) else {}
        rows = contracts.get("rows") if isinstance(contracts.get("rows"), list) else []
        found = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            mint = row.get("ca")
            chain = str(row.get("chain") or "").lower()
            if chain in ("sol", "solana") and is_solana_mint(mint) and mint not in found:
                found.append(mint)
        event["contracts"] = list(dict.fromkeys((event.get("contracts") or []) + found))[:5]
        event["scan_status"] = "indexing" if contracts.get("indexing") else ("matched" if found else "no_contract_found")
        event["observed_at"] = datetime.now(timezone.utc).isoformat()

    @client.on("profile_update")
    async def profile_update(item):
        if not isinstance(item, dict):
            return
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        before = item.get("before") if isinstance(item.get("before"), dict) else {}
        handle = str(user.get("handle") or before.get("handle") or "").lstrip("@")
        row = _watchlist_row(handle)
        if not row or not row.get("profile_updates"):
            return
        changes = item.get("modifications") if isinstance(item.get("modifications"), list) else []
        if not changes:
            for field in ("name", "description", "avatar", "banner", "location", "url"):
                if user.get(field) != before.get(field):
                    changes.append("profile." + field)
        event_id = str(item.get("id") or f"profile:{handle}:{time.time_ns()}")
        now = datetime.now(timezone.utc).isoformat()
        _EVENTS[event_id] = {"id": event_id, "kind": "profile_update", "platform": "X", "author": handle, "author_name": str(user.get("name") or handle), "text": f"@{handle} profile changed: " + (", ".join(changes) if changes else "profile updated"), "url": f"https://x.com/{handle}", "created_at": now, "contracts": [], "changes": changes, "source": "J7Tracker", "observed_at": now}
        _EVENTS.move_to_end(event_id, last=False)
        while len(_EVENTS) > _MAX_EVENTS:
            _EVENTS.popitem(last=True)

    @client.on("token_meta")
    async def token_meta(item):
        _token_meta(item)

    @client.on("tweet_deleted")
    async def tweet_deleted(item):
        post_id = str((item or {}).get("id") or "")
        if post_id:
            _EVENTS.pop(post_id, None)

    @client.on("status")
    async def upstream_status(item):
        if isinstance(item, dict):
            _STATUS["upstream_connected"] = bool(item.get("connected"))

    @client.on("auth_error")
    async def auth_error(item):
        _STATUS.update({"state": "auth_error", "connected": False, "last_error": "J7Tracker rejected the configured session. Refresh the J7 JWT in Render."})

    while not stop_event.is_set():
        try:
            _STATUS.update({"state": "connecting", "connected": False})
            await client.connect(
                "https://nyc.j7tracker.io",
                transports=["websocket"],
                auth={"token": jwt},
                wait_timeout=15,
            )
            wait_task = asyncio.create_task(client.wait())
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait({wait_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            if stop_task in done and stop_event.is_set():
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not log or return the JWT or request headers.
            _STATUS.update({"state": "reconnecting", "connected": False, "last_error": type(exc).__name__})
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
        finally:
            if client.connected:
                await client.disconnect()
    _STATUS.update({"connected": False, "state": "stopped"})
