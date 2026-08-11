"""Squeeze detection: is this name set up to move violently on itself?

Two distinct mechanisms, deliberately scored separately before being
combined, because they have different causes and different timescales:

  Short squeeze  - a lot of shares sold short relative to how many change
                   hands daily. Covering demand is slow-burning fuel.

  Gamma squeeze  - option dealers who sold calls must buy stock as it
                   rises to stay hedged. When aggregate dealer gamma is
                   negative, hedging amplifies moves instead of damping
                   them. This is fast fuel.

Every component is reported with its own inputs so you can see exactly
why the number is what it is rather than trusting a black box.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ..analytics.bs import bs_greeks, year_fraction
from ..config import settings
from ..massive import MassiveError, client
from ..models import Chain, Contract

CONTRACT_MULTIPLIER = 100.0


# ---------------------------------------------------------------------------
# Short-side data
# ---------------------------------------------------------------------------

async def fetch_short_data(ticker: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "short_interest": None,
        "prior_short_interest": None,
        "settlement_date": None,
        "days_to_cover": None,
        "avg_daily_volume": None,
        "short_interest_change_pct": None,
        "short_volume_ratio": None,
        "short_volume_ratio_trend": None,
        "errors": [],
    }

    try:
        data = await client.get(
            "/stocks/v1/short-interest",
            {"ticker": ticker, "limit": 4, "sort": "settlement_date.desc"},
            ttl=settings.ttl_slow,
        )
        rows = data.get("results") or []
        if rows:
            latest = rows[0]
            out["short_interest"] = latest.get("short_interest")
            out["settlement_date"] = latest.get("settlement_date")
            out["days_to_cover"] = latest.get("days_to_cover")
            out["avg_daily_volume"] = latest.get("avg_daily_volume")
            if len(rows) > 1 and rows[1].get("short_interest"):
                prior = rows[1]["short_interest"]
                out["prior_short_interest"] = prior
                if prior:
                    out["short_interest_change_pct"] = round(
                        (latest["short_interest"] - prior) / prior * 100, 1
                    )
    except MassiveError as exc:
        out["errors"].append(f"short interest unavailable: {exc}")

    try:
        start = (date.today() - timedelta(days=30)).isoformat()
        data = await client.get(
            "/stocks/v1/short-volume",
            {
                "ticker": ticker,
                "date.gte": start,
                "limit": 20,
                "sort": "date.desc",
            },
            ttl=settings.ttl_slow,
        )
        rows = [r for r in (data.get("results") or []) if r.get("short_volume_ratio")]
        if rows:
            recent = [r["short_volume_ratio"] for r in rows[:5]]
            older = [r["short_volume_ratio"] for r in rows[5:20]]
            out["short_volume_ratio"] = round(sum(recent) / len(recent), 1)
            if older:
                base = sum(older) / len(older)
                out["short_volume_ratio_trend"] = round(
                    out["short_volume_ratio"] - base, 1
                )
    except MassiveError as exc:
        out["errors"].append(f"short volume unavailable: {exc}")

    return out


# ---------------------------------------------------------------------------
# Gamma exposure
# ---------------------------------------------------------------------------

def _exposure_weight(c: Contract) -> Optional[float]:
    """Open interest if we have it, otherwise traded volume as a proxy."""
    if c.open_interest:
        return float(c.open_interest)
    if c.volume:
        return float(c.volume)
    return None


def gamma_exposure(chain: Chain) -> Dict[str, Any]:
    """Net dealer gamma, the zero-gamma flip level, and the biggest walls.

    Convention: dealers are assumed short calls and long puts, which is the
    standard retail-flow assumption. Positive net gamma means dealer hedging
    dampens moves; negative means it amplifies them.
    """
    spot = chain.spot
    contributing = [c for c in chain.contracts if c.gamma and _exposure_weight(c)]
    if not contributing:
        return {
            "available": False,
            "reason": "no gamma or open interest data available on this plan",
        }

    using_volume_proxy = not chain.has_open_interest

    def net_gex_at(price: float) -> float:
        total = 0.0
        for c in contributing:
            weight = _exposure_weight(c)
            if not weight or not c.iv:
                continue
            T = year_fraction(c.dte)
            g = bs_greeks(price, c.strike, T, chain.risk_free, 0.0, c.iv, c.kind).gamma
            # Dollar gamma per 1% move in the underlying.
            dollar_gamma = g * weight * CONTRACT_MULTIPLIER * price * price * 0.01
            total += dollar_gamma if c.kind == "call" else -dollar_gamma
        return total

    net_now = net_gex_at(spot)

    # Scan for the zero-gamma flip level.
    flip = None
    lo, hi = spot * 0.7, spot * 1.3
    steps = 60
    prev_price = lo
    prev_value = net_gex_at(lo)
    for i in range(1, steps + 1):
        price = lo + (hi - lo) * i / steps
        value = net_gex_at(price)
        if prev_value == 0:
            flip = prev_price
            break
        if (prev_value < 0) != (value < 0):
            span = value - prev_value
            frac = -prev_value / span if abs(span) > 1e-9 else 0.5
            flip = prev_price + (price - prev_price) * frac
            break
        prev_price, prev_value = price, value

    # Per-strike gamma concentration
    by_strike: Dict[float, Dict[str, float]] = {}
    for c in contributing:
        weight = _exposure_weight(c) or 0.0
        dollar_gamma = (
            c.gamma * weight * CONTRACT_MULTIPLIER * spot * spot * 0.01
        )
        node = by_strike.setdefault(c.strike, {"call": 0.0, "put": 0.0, "net": 0.0})
        node[c.kind] += dollar_gamma
        node["net"] += dollar_gamma if c.kind == "call" else -dollar_gamma

    strikes_sorted = sorted(by_strike.items())
    call_wall = max(strikes_sorted, key=lambda kv: kv[1]["call"], default=(None, {}))
    put_wall = max(strikes_sorted, key=lambda kv: kv[1]["put"], default=(None, {}))

    total_abs = sum(abs(v["net"]) for _, v in strikes_sorted) or 1.0

    return {
        "available": True,
        "net_gex": round(net_now, 0),
        "net_gex_millions": round(net_now / 1e6, 2),
        "regime": "negative" if net_now < 0 else "positive",
        "flip_level": round(flip, 2) if flip else None,
        "distance_to_flip_pct": round((flip - spot) / spot * 100, 2) if flip else None,
        "call_wall": call_wall[0],
        "put_wall": put_wall[0],
        "using_volume_proxy": using_volume_proxy,
        "profile": [
            {
                "strike": k,
                "net": round(v["net"], 0),
                "share": round(abs(v["net"]) / total_abs, 4),
            }
            for k, v in strikes_sorted
        ],
        "interpretation": _gex_interpretation(net_now, flip, spot, using_volume_proxy),
    }


def _gex_interpretation(
    net: float, flip: Optional[float], spot: float, proxy: bool
) -> str:
    if net < 0:
        base = (
            "Dealers are net short gamma. Their hedging adds to whichever way the "
            "stock moves, so moves tend to extend rather than fade. This is the "
            "regime gamma squeezes happen in."
        )
    else:
        base = (
            "Dealers are net long gamma. Their hedging leans against the move, "
            "which usually pins price and suppresses realised volatility."
        )
    if flip:
        direction = "above" if flip > spot else "below"
        base += (
            f" The gamma flip sits at about {flip:.2f}, {abs(flip - spot) / spot * 100:.1f}% "
            f"{direction} spot; crossing it changes the regime."
        )
    if proxy:
        base += (
            " Note: computed from traded volume rather than open interest, which "
            "your data plan does not include. Treat the magnitude as indicative only."
        )
    return base


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------

def _band(value: Optional[float], stops: List[tuple], default: float = 0.0) -> float:
    if value is None:
        return default
    for threshold, points in stops:
        if value >= threshold:
            return points
    return 0.0


def squeeze_score(
    chain: Chain,
    short: Dict[str, Any],
    gex: Dict[str, Any],
    flow: Dict[str, Any],
    shares_outstanding: Optional[float],
) -> Dict[str, Any]:
    components: List[Dict[str, Any]] = []

    # --- 1. Short interest as a share of the company (max 28) -----------
    si = short.get("short_interest")
    short_pct = None
    if si and shares_outstanding:
        short_pct = si / shares_outstanding * 100.0
    pts = _band(
        short_pct,
        [(30, 28), (20, 23), (15, 18), (10, 13), (7, 8), (4, 4), (2, 2)],
    )
    components.append(
        {
            "name": "Short interest",
            "points": pts,
            "max": 28,
            "value": round(short_pct, 2) if short_pct is not None else None,
            "unit": "% of shares outstanding",
            "note": (
                "Not available - needs short interest and share count."
                if short_pct is None
                else "Above roughly 15% is genuinely crowded."
            ),
        }
    )

    # --- 2. Days to cover (max 20) --------------------------------------
    dtc = short.get("days_to_cover")
    pts = _band(dtc, [(10, 20), (7, 16), (5, 12), (3, 8), (2, 5), (1, 2)])
    components.append(
        {
            "name": "Days to cover",
            "points": pts,
            "max": 20,
            "value": dtc,
            "unit": "days of average volume",
            "note": "How long shorts would need to buy back their position.",
        }
    )

    # --- 3. Recent short selling pressure (max 12) ----------------------
    svr = short.get("short_volume_ratio")
    pts = _band(svr, [(60, 12), (55, 9), (50, 7), (45, 4), (40, 2)])
    components.append(
        {
            "name": "Daily short volume",
            "points": pts,
            "max": 12,
            "value": svr,
            "unit": "% of reported volume sold short",
            "note": "Elevated readings mean shorts are still pressing, not covering.",
        }
    )

    # --- 4. Dealer gamma regime (max 25) --------------------------------
    gamma_pts = 0.0
    gamma_value = None
    if gex.get("available"):
        gamma_value = gex.get("net_gex_millions")
        if gex.get("regime") == "negative":
            gamma_pts += 15
        dist = gex.get("distance_to_flip_pct")
        if dist is not None and abs(dist) <= 3:
            gamma_pts += 10
        elif dist is not None and abs(dist) <= 7:
            gamma_pts += 6
        if gex.get("using_volume_proxy"):
            gamma_pts *= 0.6  # discount for lower-quality input
    components.append(
        {
            "name": "Dealer gamma",
            "points": round(gamma_pts, 1),
            "max": 25,
            "value": gamma_value,
            "unit": "$m net gamma per 1% move",
            "note": gex.get("interpretation", "Not available."),
        }
    )

    # --- 5. Call-side option demand (max 15) ----------------------------
    ratio = flow.get("call_put_volume_ratio")
    otm_share = flow.get("otm_call_volume_share")
    call_pts = 0.0
    if ratio:
        call_pts += _band(ratio, [(3.0, 9), (2.0, 7), (1.5, 5), (1.2, 3), (1.0, 1)])
    if otm_share:
        call_pts += _band(otm_share, [(50, 6), (40, 4), (30, 2)])
    components.append(
        {
            "name": "Call demand",
            "points": round(min(call_pts, 15), 1),
            "max": 15,
            "value": round(ratio, 2) if ratio else None,
            "unit": "call/put volume ratio",
            "note": "Heavy short-dated out-of-the-money call buying is the usual "
            "trigger for a gamma squeeze.",
        }
    )

    total = sum(c["points"] for c in components)
    max_available = sum(
        c["max"] for c in components if c["value"] is not None or c["points"] > 0
    )
    confidence = round(max_available / 100.0, 2)

    if total >= 70:
        label, summary = "High", (
            "Multiple squeeze ingredients are present at once. Names in this state "
            "can move far more than the option market is pricing - in both "
            "directions. Position size matters more than direction here."
        )
    elif total >= 50:
        label, summary = "Elevated", (
            "Meaningful squeeze setup, but not all the pieces are in place. Worth "
            "watching for a catalyst."
        )
    elif total >= 30:
        label, summary = "Moderate", (
            "Some crowding or hedging pressure, nothing unusual by itself."
        )
    else:
        label, summary = "Low", (
            "No real squeeze setup in the data. Ordinary positioning."
        )

    return {
        "score": round(total, 1),
        "label": label,
        "summary": summary,
        "confidence": confidence,
        "components": components,
        "data_gaps": short.get("errors", []),
    }
