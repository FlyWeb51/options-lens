"""Massive.com REST client.

Handles the three things that actually matter when you are running on a
free API key and sharing the app with other people:

1. Rate limiting  - free tier is 5 requests/minute, hard.
2. Caching        - so ten friends hitting the same ticker costs one call.
3. Plan detection - the free tier does NOT include the options chain
                    snapshot, so we probe once and remember which data
                    paths are actually open to this key.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import httpx

from .config import settings


class MassiveError(Exception):
    """Any non-recoverable API problem."""

    def __init__(self, message: str, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class PlanRestricted(MassiveError):
    """The key is valid but this endpoint is not on the current plan."""


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class SqliteCache:
    """Tiny persistent response cache. Survives restarts, shared by all users."""

    SCHEMA = (
        "CREATE TABLE IF NOT EXISTS entries ("
        "  key TEXT PRIMARY KEY,"
        "  body TEXT NOT NULL,"
        "  stored_at REAL NOT NULL,"
        "  expires_at REAL NOT NULL"
        ")"
    )

    def __init__(self, path: str):
        self._lock = asyncio.Lock()
        self._path = path
        self._conn = self._connect(path)

    def _connect(self, path: str) -> sqlite3.Connection:
        """Open the cache, degrading gracefully if the location is unwritable.

        Network mounts and read-only container filesystems cannot host a
        SQLite file. Falling back to a temp directory (and finally to memory)
        keeps the app working; the only cost is a colder cache. The schema
        write is the real test - merely connecting always appears to succeed.
        """
        import tempfile

        candidates = [
            path,
            str(Path(tempfile.gettempdir()) / "options-lens-cache.sqlite"),
            ":memory:",
        ]
        for candidate in candidates:
            try:
                conn = sqlite3.connect(candidate, check_same_thread=False)
                conn.execute(self.SCHEMA)
                conn.commit()
            except sqlite3.Error:
                continue
            self._path = candidate
            if candidate != path:
                where = "in memory only" if candidate == ":memory:" else candidate
                print(f"[cache] {path} is not writable; caching {where}.")
            return conn
        raise RuntimeError("Could not open a cache database anywhere.")

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            row = self._conn.execute(
                "SELECT body, expires_at FROM entries WHERE key = ?", (key,)
            ).fetchone()
        if not row:
            return None
        body, expires_at = row
        if expires_at < time.time():
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        now = time.time()
        async with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO entries (key, body, stored_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (key, json.dumps(value), now, now + ttl),
            )
            self._conn.commit()

    async def age(self, key: str) -> Optional[float]:
        """Seconds since this key was stored, or None if absent."""
        async with self._lock:
            row = self._conn.execute(
                "SELECT stored_at FROM entries WHERE key = ?", (key,)
            ).fetchone()
        return time.time() - row[0] if row else None

    async def purge_expired(self) -> int:
        async with self._lock:
            cur = self._conn.execute(
                "DELETE FROM entries WHERE expires_at < ?", (time.time(),)
            )
            self._conn.commit()
            return cur.rowcount


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window limiter. `per_minute <= 0` means unlimited."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: List[float] = []
        self._lock = asyncio.Lock()

    @property
    def unlimited(self) -> bool:
        return self.per_minute <= 0

    async def acquire(self) -> None:
        if self.unlimited:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                self._hits = [t for t in self._hits if now - t < 60.0]
                if len(self._hits) < self.per_minute:
                    self._hits.append(now)
                    return
                wait = 60.0 - (now - self._hits[0]) + 0.05
            await asyncio.sleep(max(wait, 0.05))

    def slots_free(self) -> int:
        if self.unlimited:
            return 9999
        now = time.monotonic()
        recent = [t for t in self._hits if now - t < 60.0]
        return max(self.per_minute - len(recent), 0)


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------

@dataclass
class Capabilities:
    """What this API key is actually allowed to do."""

    checked: bool = False
    chain_snapshot: bool = False     # /v3/snapshot/options/{t}  (paid options plan)
    stock_snapshot: bool = False     # /v2/snapshot/.../tickers/{t}
    short_interest: bool = False
    short_volume: bool = False
    ticker_overview: bool = False
    treasury_yields: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return "snapshot" if self.chain_snapshot else "endofday"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "checked": self.checked,
            "mode": self.mode,
            "chain_snapshot": self.chain_snapshot,
            "stock_snapshot": self.stock_snapshot,
            "short_interest": self.short_interest,
            "short_volume": self.short_volume,
            "ticker_overview": self.ticker_overview,
            "treasury_yields": self.treasury_yields,
            "notes": self.notes,
        }

    def as_dict_for_cache(self) -> Dict[str, Any]:
        """Only the fields accepted by ``Capabilities(**data)``."""
        return {
            "chain_snapshot": self.chain_snapshot,
            "stock_snapshot": self.stock_snapshot,
            "short_interest": self.short_interest,
            "short_volume": self.short_volume,
            "ticker_overview": self.ticker_overview,
            "treasury_yields": self.treasury_yields,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class MassiveClient:
    def __init__(self) -> None:
        self.cache = SqliteCache(settings.cache_path)
        self.limiter = RateLimiter(settings.rate_limit_per_min)
        self.caps = Capabilities()
        self._sem = asyncio.Semaphore(settings.max_concurrency)
        self._client: Optional[httpx.AsyncClient] = None
        self._caps_lock = asyncio.Lock()
        self.calls_made = 0
        self.cache_hits = 0

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            try:
                self._client = httpx.AsyncClient(
                    base_url=settings.base_url,
                    timeout=settings.request_timeout,
                    headers={
                        "Authorization": f"Bearer {settings.api_key}",
                        "User-Agent": "options-lens/1.0",
                    },
                )
            except Exception as exc:  # proxy misconfiguration, missing extras
                raise MassiveError(
                    f"Could not open an HTTP connection: {exc}"
                ) from exc
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- core request ----------------------------------------------------

    @staticmethod
    def _cache_key(path: str, params: Dict[str, Any] | None) -> str:
        items = sorted((params or {}).items())
        return path + "?" + "&".join(f"{k}={v}" for k, v in items)

    async def get(
        self,
        path: str,
        params: Dict[str, Any] | None = None,
        ttl: int = 600,
        allow_cache: bool = True,
    ) -> Dict[str, Any]:
        if not settings.has_key:
            raise MassiveError(
                "No API key configured. Set MASSIVE_API_KEY in your .env file."
            )

        key = self._cache_key(path, params)
        if allow_cache:
            cached = await self.cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                return cached

        attempts = 0
        while True:
            attempts += 1
            await self.limiter.acquire()
            async with self._sem:
                client = await self._http()
                try:
                    resp = await client.get(path, params=params or {})
                except httpx.HTTPError as exc:
                    if attempts >= 3:
                        raise MassiveError(f"Network error: {exc}", url=path) from exc
                    await asyncio.sleep(1.5 * attempts)
                    continue

            self.calls_made += 1

            if resp.status_code == 429:
                # Respect the server even though we self-throttle.
                if attempts >= 4:
                    raise MassiveError(
                        "Rate limited by Massive. The free tier allows 5 calls per "
                        "minute; try again shortly.",
                        status=429,
                        url=path,
                    )
                await asyncio.sleep(12 * attempts)
                continue

            if resp.status_code in (401, 403):
                detail = _extract_message(resp)
                if resp.status_code == 401:
                    raise MassiveError(
                        f"API key rejected (401). {detail}", status=401, url=path
                    )
                raise PlanRestricted(
                    f"Not available on your plan (403). {detail}",
                    status=403,
                    url=path,
                )

            if resp.status_code == 404:
                raise MassiveError("Not found (404).", status=404, url=path)

            if resp.status_code >= 500:
                if attempts >= 3:
                    raise MassiveError(
                        f"Massive returned {resp.status_code}.",
                        status=resp.status_code,
                        url=path,
                    )
                await asyncio.sleep(2 * attempts)
                continue

            if resp.status_code >= 400:
                raise MassiveError(
                    f"Request failed ({resp.status_code}). {_extract_message(resp)}",
                    status=resp.status_code,
                    url=path,
                )

            data = resp.json()
            if allow_cache:
                await self.cache.set(key, data, ttl)
            return data

    async def paginate(
        self,
        path: str,
        params: Dict[str, Any] | None = None,
        ttl: int = 600,
        max_pages: int = 6,
    ) -> List[Dict[str, Any]]:
        """Follow next_url, returning the concatenated `results` arrays."""
        out: List[Dict[str, Any]] = []
        page_params = dict(params or {})
        page = 0
        cursor: Optional[str] = None
        while page < max_pages:
            page += 1
            if cursor:
                data = await self.get(path, {**page_params, "cursor": cursor}, ttl=ttl)
            else:
                data = await self.get(path, page_params, ttl=ttl)
            results = data.get("results") or []
            out.extend(results)
            next_url = data.get("next_url")
            if not next_url or not results:
                break
            cursor = _cursor_from(next_url)
            if not cursor:
                break
        return out

    # -- capability probe -------------------------------------------------

    async def detect_capabilities(self, probe_ticker: str = "AAPL") -> Capabilities:
        """Figure out what the key can reach. Cached for a day."""
        async with self._caps_lock:
            if self.caps.checked:
                return self.caps

            cached = await self.cache.get("__caps__")
            if cached:
                self.caps = Capabilities(**cached)
                self.caps.checked = True
                return self.caps

            caps = Capabilities(checked=True)

            async def probe(path: str, params: Dict[str, Any]) -> bool:
                try:
                    await self.get(path, params, ttl=settings.ttl_reference)
                    return True
                except PlanRestricted:
                    return False
                except MassiveError:
                    return False

            caps.chain_snapshot = await probe(
                f"/v3/snapshot/options/{probe_ticker}", {"limit": 1}
            )
            caps.ticker_overview = await probe(
                f"/v3/reference/tickers/{probe_ticker}", {}
            )
            caps.short_interest = await probe(
                "/stocks/v1/short-interest", {"ticker": probe_ticker, "limit": 1}
            )

            if not caps.chain_snapshot:
                caps.notes.append(
                    "Your key does not include the options chain snapshot "
                    "(that endpoint starts at the Options Starter plan). Running in "
                    "end-of-day mode: prices come from previous-day bars, implied "
                    "volatility and greeks are computed locally, and open interest "
                    "is unavailable."
                )
            if not caps.short_interest:
                caps.notes.append(
                    "Short interest is unavailable, so the short-squeeze half of the "
                    "squeeze score is skipped."
                )

            self.caps = caps
            await self.cache.set("__caps__", caps.as_dict_for_cache(), settings.ttl_reference)
            return caps

    def stats(self) -> Dict[str, Any]:
        return {
            "api_calls_this_process": self.calls_made,
            "cache_hits_this_process": self.cache_hits,
            "rate_limit_per_min": self.limiter.per_minute,
            "rate_slots_free": self.limiter.slots_free(),
        }


def _extract_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except Exception:
        return resp.text[:200]
    for field_name in ("message", "error", "detail"):
        if field_name in body:
            return str(body[field_name])[:300]
    return str(body)[:200]


def _cursor_from(next_url: str) -> Optional[str]:
    if "cursor=" not in next_url:
        return None
    return next_url.split("cursor=", 1)[1].split("&", 1)[0]


client = MassiveClient()
