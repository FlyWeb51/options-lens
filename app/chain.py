"""Fetch and normalise an options chain.

Two paths, chosen automatically by capability detection:

  snapshot mode  (paid options plan)
      One paginated call to /v3/snapshot/options/{ticker} gives prices,
      greeks, implied volatility and open interest.

  end-of-day mode (free options plan)
      The snapshot endpoint is not available, so we list contracts from the
      reference endpoint and pull previous-day bars one contract at a time.
      Implied volatility and greeks are computed locally from the closing
      price. Open interest is not published on this tier.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .analytics.bs import bs_greeks, implied_vol, year_fraction
from .config import settings
from .massive import MassiveError, PlanRestricted, client
from .models import Chain, Contract

Progress = Callable[[str, float], None]


def _noop(message: str, pct: float) -> None:  # pragma: no cover
    return None


# ---------------------------------------------------------------------------
# Supporting market data
# ---------------------------------------------------------------------------

async def get_risk_free_rate() -> float:
    """Use the 3-month treasury yield as the short-rate proxy."""
    try:
        data = await client.get(
            "/fed/v1/treasury-yields",
            {"limit": 1, "sort": "date.desc"},
            ttl=settings.ttl_slow,
        )
        rows = data.get("results") or []
        if rows:
            for key in ("yield_3_month", "yield_6_month", "yield_1_year"):
                value = rows[0].get(key)
                if value:
                    return float(value) / 100.0
    except MassiveError:
        pass
    return settings.default_risk_free


async def get_spot(ticker: str) -> Tuple[float, str, Optional[float]]:
    """Return (price, source, previous_day_volume)."""
    # Try the live-ish snapshot first; falls back to the previous daily bar,
    # which every plan including the free tier can read.
    try:
        data = await client.get(
            f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
            {},
            ttl=settings.ttl_quote,
        )
        node = data.get("ticker") or {}
        last = (node.get("lastTrade") or {}).get("p")
        day = node.get("day") or {}
        prev = node.get("prevDay") or {}
        price = last or day.get("c") or prev.get("c")
        if price:
            return float(price), "snapshot", prev.get("v")
    except MassiveError:
        pass

    data = await client.get(
        f"/v2/aggs/ticker/{ticker}/prev", {"adjusted": "true"}, ttl=settings.ttl_quote
    )
    rows = data.get("results") or []
    if not rows:
        raise MassiveError(f"No price data found for {ticker}. Check the symbol.")
    bar = rows[0]
    return float(bar["c"]), "previous_close", bar.get("v")


async def get_avg_daily_volume(ticker: str, lookback_days: int = 40) -> Optional[float]:
    end = date.today()
    start = end - timedelta(days=lookback_days * 2)
    try:
        data = await client.get(
            f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
            {"adjusted": "true", "sort": "desc", "limit": lookback_days},
            ttl=settings.ttl_slow,
        )
    except MassiveError:
        return None
    rows = data.get("results") or []
    vols = [r.get("v") for r in rows if r.get("v")]
    return sum(vols) / len(vols) if vols else None


async def get_shares_outstanding(ticker: str) -> Optional[float]:
    try:
        data = await client.get(
            f"/v3/reference/tickers/{ticker}", {}, ttl=settings.ttl_slow
        )
    except MassiveError:
        return None
    res = data.get("results") or {}
    for key in ("weighted_shares_outstanding", "share_class_shares_outstanding"):
        if res.get(key):
            return float(res[key])
    return None


# ---------------------------------------------------------------------------
# Chain building
# ---------------------------------------------------------------------------

async def build_chain(ticker: str, progress: Progress = _noop) -> Chain:
    ticker = ticker.strip().upper()
    caps = await client.detect_capabilities()

    progress("Fetching underlying price", 5)
    spot, spot_source, _ = await get_spot(ticker)
    risk_free = await get_risk_free_rate()

    if caps.chain_snapshot:
        chain = await _build_from_snapshot(ticker, spot, risk_free, progress)
    else:
        chain = await _build_from_eod(ticker, spot, risk_free, progress)

    chain.spot_source = spot_source
    chain.warnings.extend(caps.notes)
    return chain


def _expiry_bounds() -> Tuple[str, str]:
    today = date.today()
    # Look far enough out to catch monthlies, then trim by count later.
    return today.isoformat(), (today + timedelta(days=200)).isoformat()


async def _build_from_snapshot(
    ticker: str, spot: float, risk_free: float, progress: Progress
) -> Chain:
    lo_exp, hi_exp = _expiry_bounds()
    lo_strike = spot * (1 - settings.strike_window)
    hi_strike = spot * (1 + settings.strike_window)

    progress("Loading options chain snapshot", 25)
    rows = await client.paginate(
        f"/v3/snapshot/options/{ticker}",
        {
            "limit": 250,
            "expiration_date.gte": lo_exp,
            "expiration_date.lte": hi_exp,
            "strike_price.gte": round(lo_strike, 2),
            "strike_price.lte": round(hi_strike, 2),
            "sort": "expiration_date",
            "order": "asc",
        },
        ttl=settings.ttl_chain,
        max_pages=8,
    )

    today = date.today()
    contracts: List[Contract] = []
    for row in rows:
        details = row.get("details") or {}
        expiry = details.get("expiration_date")
        strike = details.get("strike_price")
        kind = details.get("contract_type")
        if not expiry or strike is None or kind not in ("call", "put"):
            continue

        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days
        if dte < 0:
            continue

        quote = row.get("last_quote") or {}
        trade = row.get("last_trade") or {}
        day = row.get("day") or {}
        greeks = row.get("greeks") or {}

        bid, ask = quote.get("bid"), quote.get("ask")
        mid = quote.get("midpoint")
        if mid is None and bid is not None and ask is not None:
            mid = (bid + ask) / 2.0

        c = Contract(
            ticker=details.get("ticker", ""),
            underlying=ticker,
            kind=kind,
            strike=float(strike),
            expiry=expiry,
            dte=float(dte),
            bid=bid,
            ask=ask,
            mid=mid,
            last=trade.get("price"),
            close=day.get("close"),
            volume=day.get("volume"),
            open_interest=row.get("open_interest"),
            iv=row.get("implied_volatility"),
            delta=greeks.get("delta"),
            gamma=greeks.get("gamma"),
            vega=greeks.get("vega"),
            theta=greeks.get("theta"),
            iv_source="api" if row.get("implied_volatility") else "none",
            price_source="quote" if mid else ("trade" if trade.get("price") else "eod_close"),
        )
        _fill_missing_analytics(c, spot, risk_free)
        contracts.append(c)

    progress("Chain loaded", 70)
    chain = Chain(
        underlying=ticker,
        spot=spot,
        as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        mode="snapshot",
        contracts=contracts,
        risk_free=risk_free,
    )
    _trim_expiries(chain)
    return chain


async def _build_from_eod(
    ticker: str, spot: float, risk_free: float, progress: Progress
) -> Chain:
    lo_exp, hi_exp = _expiry_bounds()
    lo_strike = spot * (1 - settings.strike_window)
    hi_strike = spot * (1 + settings.strike_window)

    progress("Listing contracts", 10)
    rows = await client.paginate(
        "/v3/reference/options/contracts",
        {
            "underlying_ticker": ticker,
            "expiration_date.gte": lo_exp,
            "expiration_date.lte": hi_exp,
            "strike_price.gte": round(lo_strike, 2),
            "strike_price.lte": round(hi_strike, 2),
            "limit": 1000,
            "sort": "expiration_date",
            "order": "asc",
            "expired": "false",
        },
        ttl=settings.ttl_reference,
        max_pages=2,
    )
    if not rows:
        raise MassiveError(f"No listed option contracts found for {ticker}.")

    today = date.today()
    selected = _select_eod_contracts(rows, spot, today)
    total = len(selected)

    contracts: List[Contract] = []
    for index, meta in enumerate(selected):
        pct = 15 + 60 * (index / max(total, 1))
        progress(
            f"Pricing contract {index + 1} of {total} "
            f"({meta['contract_type']} {meta['strike_price']:g} exp {meta['expiration_date']})",
            pct,
        )
        try:
            bar_data = await client.get(
                f"/v2/aggs/ticker/{meta['ticker']}/prev",
                {"adjusted": "true"},
                ttl=settings.ttl_slow,
            )
        except MassiveError:
            continue

        bars = bar_data.get("results") or []
        if not bars:
            continue
        bar = bars[0]
        expiry = meta["expiration_date"]
        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days

        c = Contract(
            ticker=meta["ticker"],
            underlying=ticker,
            kind=meta["contract_type"],
            strike=float(meta["strike_price"]),
            expiry=expiry,
            dte=float(dte),
            close=bar.get("c"),
            volume=bar.get("v"),
            open_interest=None,
            price_source="eod_close",
        )
        _fill_missing_analytics(c, spot, risk_free)
        contracts.append(c)

    chain = Chain(
        underlying=ticker,
        spot=spot,
        as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        mode="endofday",
        contracts=contracts,
        risk_free=risk_free,
        warnings=[
            "End-of-day mode: option prices are previous-session closes, not live "
            "quotes. Implied volatility and greeks are computed locally from those "
            "closes. Open interest is not available, so gamma exposure is estimated "
            "from traded volume instead and is less reliable.",
        ],
    )
    _trim_expiries(chain)
    return chain


def _select_eod_contracts(
    rows: List[Dict[str, Any]], spot: float, today: date
) -> List[Dict[str, Any]]:
    """Pick the most informative contracts within the request budget.

    Priority: near-the-money strikes on the nearest expirations, keeping
    calls and puts paired so the probability model stays well conditioned.
    """
    by_expiry: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        expiry = row.get("expiration_date")
        if not expiry or row.get("contract_type") not in ("call", "put"):
            continue
        if (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days < 0:
            continue
        by_expiry.setdefault(expiry, []).append(row)

    expiries = sorted(by_expiry)[: settings.max_expirations]
    if not expiries:
        return []

    budget = settings.max_contracts_eod
    # Weight the front expirations more heavily - that is where the
    # tradeable signal lives.
    weights = [1.0 / (i + 1) ** 0.6 for i in range(len(expiries))]
    weight_sum = sum(weights)

    picked: List[Dict[str, Any]] = []
    for expiry, weight in zip(expiries, weights):
        quota = max(int(budget * weight / weight_sum), 6)
        strikes = sorted({r["strike_price"] for r in by_expiry[expiry]})
        strikes.sort(key=lambda k: abs(k - spot))
        keep = set(strikes[: max(quota // 2, 3)])
        for row in by_expiry[expiry]:
            if row["strike_price"] in keep:
                picked.append(row)

    picked.sort(key=lambda r: (r["expiration_date"], abs(r["strike_price"] - spot)))
    return picked[:budget]


def _fill_missing_analytics(c: Contract, spot: float, risk_free: float) -> None:
    """Compute implied volatility and greeks wherever the API did not supply them."""
    T = year_fraction(c.dte)
    price = c.price

    if c.iv is None and price:
        iv = implied_vol(price, spot, c.strike, T, risk_free, 0.0, c.kind)
        if iv is not None:
            c.iv = iv
            c.iv_source = "computed"

    if c.iv and (c.delta is None or c.gamma is None):
        g = bs_greeks(spot, c.strike, T, risk_free, 0.0, c.iv, c.kind)
        c.delta = c.delta if c.delta is not None else g.delta
        c.gamma = c.gamma if c.gamma is not None else g.gamma
        c.vega = c.vega if c.vega is not None else g.vega
        c.theta = c.theta if c.theta is not None else g.theta


def _trim_expiries(chain: Chain) -> None:
    keep = set(chain.expiries()[: settings.max_expirations])
    chain.contracts = [c for c in chain.contracts if c.expiry in keep]
