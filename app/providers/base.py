"""Shared plumbing for third-party data sources.

These providers are not rate limited by Massive, so they get their own
client. Everything is cached through the same SQLite store so a page
refresh never re-hits an upstream API.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import httpx

from ..config import settings
from ..massive import client as massive_client


class ProviderError(Exception):
    """Upstream failed in a way the caller should surface, not crash on."""


_client: Optional[httpx.AsyncClient] = None
_lock = asyncio.Lock()


async def _http() -> httpx.AsyncClient:
    global _client
    async with _lock:
        if _client is None:
            _client = httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers={"Accept": "application/json"},
            )
        return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def fetch_json(
    url: str,
    params: Dict[str, Any] | None = None,
    headers: Dict[str, str] | None = None,
    ttl: int | None = None,
    cache_key: str | None = None,
) -> Any:
    """GET JSON with caching. Raises ProviderError on any failure."""
    ttl = ttl if ttl is not None else settings.ttl_external
    key = cache_key or (
        "ext:" + url + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    )

    cached = await massive_client.cache.get(key)
    if cached is not None:
        return cached

    try:
        http = await _http()
        resp = await http.get(url, params=params or {}, headers=headers or {})
    except httpx.HTTPError as exc:
        raise ProviderError(f"Could not reach {_host(url)}: {exc}") from exc

    if resp.status_code == 404:
        raise ProviderError(f"{_host(url)} has no record for that query.")
    if resp.status_code == 429:
        raise ProviderError(f"{_host(url)} rate limited this request. Try again shortly.")
    if resp.status_code >= 400:
        raise ProviderError(f"{_host(url)} returned {resp.status_code}.")

    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderError(f"{_host(url)} returned a non-JSON response.") from exc

    await massive_client.cache.set(key, data, ttl)
    return data


def _host(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return url


def ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"available": True, **payload}


def unavailable(reason: str, **extra: Any) -> Dict[str, Any]:
    return {"available": False, "reason": reason, **extra}
