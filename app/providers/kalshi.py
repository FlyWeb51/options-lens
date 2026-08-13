"""Kalshi prediction markets.

Kalshi's read endpoints are public and need no key. A Kalshi price *is* a
probability: a contract trading at 63 cents means the market puts a 63%
chance on that outcome. That makes it directly comparable to the
option-implied probabilities this app already computes, which is the
interesting part.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from .base import ProviderError, fetch_json, ok, unavailable

API = "https://api.elections.kalshi.com/trade-api/v2"

# Curated series that actually matter to someone trading equities.
# Sports markets dominate Kalshi by volume, so an unfiltered list is noise.
FINANCE_SERIES = [
    ("KXFEDDECISION", "Fed rate decision", "rates"),
    ("KXFED", "Fed funds target", "rates"),
    ("KXCPIYOY", "CPI inflation year over year", "inflation"),
    ("KXCPI", "CPI monthly", "inflation"),
    ("KXU3", "Unemployment rate", "jobs"),
    ("KXPAYROLLS", "Nonfarm payrolls", "jobs"),
    ("KXGDP", "GDP growth", "growth"),
    ("KXRECSSNBER", "Recession called", "growth"),
    ("KXINXY", "S&P 500 year end", "equities"),
    ("KXNASDAQ100Y", "Nasdaq 100 year end", "equities"),
    ("KXBTCY", "Bitcoin year end", "crypto"),
    ("KXETHY", "Ethereum year end", "crypto"),
]


def _money(value: Any) -> Optional[float]:
    """Kalshi returns prices as dollar strings; some fields are cents."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_market(m: Dict[str, Any]) -> Dict[str, Any]:
    yes_bid = _money(m.get("yes_bid_dollars"))
    yes_ask = _money(m.get("yes_ask_dollars"))
    last = _money(m.get("last_price_dollars"))

    # Mid of the book is the fairest read on the market's probability.
    if yes_bid is not None and yes_ask is not None and (yes_bid or yes_ask):
        prob = (yes_bid + yes_ask) / 2
    elif last:
        prob = last
    else:
        prob = None

    return {
        "ticker": m.get("ticker"),
        "event_ticker": m.get("event_ticker"),
        "title": m.get("yes_sub_title") or m.get("title") or m.get("subtitle"),
        "status": m.get("status"),
        "probability_pct": round(prob * 100, 1) if prob is not None else None,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "last": last,
        "volume": _money(m.get("volume_fp")),
        "volume_24h": _money(m.get("volume_24h_fp")),
        "open_interest": _money(m.get("open_interest_fp")),
        "liquidity": _money(m.get("liquidity_dollars")),
        "close_time": m.get("close_time"),
        "url": f"https://kalshi.com/markets/{m.get('event_ticker', '')}",
    }


def _tradeable(m: Dict[str, Any]) -> bool:
    """Filter out the enormous tail of empty auto-generated combo markets."""
    if m.get("probability_pct") is None:
        return False
    if (m.get("volume") or 0) <= 0 and (m.get("open_interest") or 0) <= 0:
        return False
    return True


async def series_markets(series_ticker: str, limit: int = 40) -> List[Dict[str, Any]]:
    data = await fetch_json(
        f"{API}/markets",
        {"series_ticker": series_ticker, "limit": limit, "status": "open"},
        ttl=900,
    )
    markets = [_clean_market(m) for m in (data.get("markets") or [])]
    return [m for m in markets if _tradeable(m)]


async def dashboard() -> Dict[str, Any]:
    """The curated macro board.

    Kalshi rate limits bursts, so the series are fetched with a short pause
    between them and one retry on a rate-limit response. The whole board is
    cached for 15 minutes afterwards, so this cost is paid rarely.
    """
    groups: List[Dict[str, Any]] = []
    errors: List[str] = []

    for index, (ticker, label, category) in enumerate(FINANCE_SERIES):
        if index:
            await asyncio.sleep(0.4)
        try:
            markets = await series_markets(ticker, limit=30)
        except ProviderError as exc:
            if "rate limited" in str(exc).lower():
                await asyncio.sleep(2.0)
                try:
                    markets = await series_markets(ticker, limit=30)
                except ProviderError as retry_exc:
                    errors.append(f"{label}: {retry_exc}")
                    continue
            else:
                errors.append(f"{label}: {exc}")
                continue
        if not markets:
            continue
        markets.sort(key=lambda m: -(m.get("volume") or 0))
        groups.append({
            "series": ticker,
            "label": label,
            "category": category,
            "markets": markets[:12],
        })

    if not groups:
        return unavailable(
            "Kalshi returned no open markets for the tracked economic series. "
            + (" ".join(errors) if errors else "")
        )

    return ok({
        "groups": groups,
        "errors": errors,
        "note": "A price of 63c means the market prices a 63% chance. These are "
                "real-money probabilities, and like option prices they include a "
                "risk premium and fee drag, so they are not pure forecasts.",
        "source": "Kalshi public trade API (no key required)",
    })


async def search(query: str, limit: int = 60) -> Dict[str, Any]:
    """Find markets whose title mentions a company or keyword."""
    query = (query or "").strip()
    if len(query) < 2:
        return unavailable("Enter at least two characters to search.")

    try:
        data = await fetch_json(
            f"{API}/events",
            {"limit": 200, "status": "open", "with_nested_markets": "true"},
            ttl=1800,
            cache_key="kalshi:events:open",
        )
    except ProviderError as exc:
        return unavailable(str(exc))

    needle = query.lower()
    hits: List[Dict[str, Any]] = []
    for event in data.get("events") or []:
        title = (event.get("title") or "") + " " + (event.get("sub_title") or "")
        if needle not in title.lower():
            continue
        markets = [_clean_market(m) for m in (event.get("markets") or [])]
        markets = [m for m in markets if _tradeable(m)]
        if not markets:
            continue
        hits.append({
            "event_ticker": event.get("event_ticker"),
            "title": event.get("title"),
            "sub_title": event.get("sub_title"),
            "category": event.get("category"),
            "markets": sorted(markets, key=lambda m: -(m.get("volume") or 0))[:10],
            "url": f"https://kalshi.com/markets/{event.get('event_ticker', '')}",
        })
        if len(hits) >= limit:
            break

    if not hits:
        return unavailable(f"No open Kalshi markets mention \"{query}\".")
    return ok({"query": query, "events": hits,
               "source": "Kalshi public trade API (no key required)"})


def compare_to_options(
    kalshi_markets: List[Dict[str, Any]], ladder: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Line Kalshi probabilities up against the option-implied ladder.

    Only meaningful when a Kalshi market is about the same underlying over a
    comparable horizon, so this is presented as a side-by-side rather than a
    hard signal.
    """
    if not kalshi_markets or not ladder:
        return unavailable("Need both a Kalshi market and an option chain.")

    rows = []
    for m in kalshi_markets[:12]:
        rows.append({
            "market": m.get("title"),
            "kalshi_pct": m.get("probability_pct"),
            "volume": m.get("volume"),
            "url": m.get("url"),
        })
    return ok({
        "rows": rows,
        "option_ladder": ladder,
        "caveat": "Kalshi contracts and option strikes rarely share the same "
                  "expiry or threshold. Treat differences as a prompt to look "
                  "closer, not as an arbitrage.",
    })
