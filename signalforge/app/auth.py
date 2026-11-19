"""API-key auth with two roles. Read keys (for Grok) can never call admin endpoints."""
import hashlib
import hmac
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request


@dataclass(frozen=True)
class Principal:
    role: str          # "read" or "admin"
    fingerprint: str   # short hash for audit logs; the key itself is never logged


def _fp(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def _extract(request: Request):
    key = request.headers.get("x-api-key")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key


def _match(key: str, candidates) -> bool:
    ok = False
    for c in candidates:  # compare against all, constant-time each, no early exit
        ok |= hmac.compare_digest(key.encode(), c.encode())
    return ok


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.hits = defaultdict(deque)

    def check(self, ident: str) -> None:
        now = time.monotonic()
        q = self.hits[ident]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        q.append(now)


def _authenticate(request: Request) -> Principal:
    settings = request.app.state.settings
    key = _extract(request)
    if not key:
        raise HTTPException(status_code=401, detail="Missing API key")
    if _match(key, settings.admin_api_keys):
        principal = Principal("admin", _fp(key))
    elif _match(key, settings.read_api_keys):
        principal = Principal("read", _fp(key))
    else:
        raise HTTPException(status_code=401, detail="Invalid API key")
    request.app.state.limiter.check(principal.fingerprint)
    return principal


def require_read(principal: Principal = Depends(_authenticate)) -> Principal:
    return principal  # admin keys also satisfy read


def require_admin(principal: Principal = Depends(_authenticate)) -> Principal:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail="Admin key required")
    return principal
