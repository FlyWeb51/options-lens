"""Implied volatility structure, and how it compares to reality.

The headline number here is the variance risk premium: implied volatility
minus the volatility the stock has actually delivered. It is persistently
positive across equities, which is the quantitative statement of "options
are usually a bit expensive". When it goes negative, the market is charging
less for future movement than the stock has recently produced.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ..models import Chain
from .bs import year_fraction
from .pricing import atm_iv, otm_iv_curve


def realized_volatility(closes: List[float], window: int = 21) -> Optional[float]:
    """Annualised close-to-close volatility over the last `window` bars."""
    if len(closes) < window + 1:
        return None
    rets = []
    for i in range(len(closes) - window, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev and cur and prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def realized_series(closes: List[float]) -> Dict[str, Optional[float]]:
    return {
        "rv_10d": realized_volatility(closes, 10),
        "rv_21d": realized_volatility(closes, 21),
        "rv_63d": realized_volatility(closes, 63),
        "rv_126d": realized_volatility(closes, 126),
    }


def surface(chain: Chain) -> Dict[str, Any]:
    """Implied volatility by strike and expiry - the whole visible surface."""
    spot = chain.spot
    expiries = chain.expiries()
    grid: List[Dict[str, Any]] = []

    for expiry in expiries:
        contracts = chain.for_expiry(expiry)
        curve = otm_iv_curve(contracts, spot)
        if len(curve) < 3:
            continue
        dte = contracts[0].dte if contracts else 0
        grid.append({
            "expiry": expiry,
            "dte": round(dte, 0),
            "points": [
                {
                    "strike": k,
                    "moneyness_pct": round((k / spot - 1) * 100, 2),
                    "iv_pct": round(v * 100, 2),
                }
                for k, v in curve
            ],
            "atm_iv_pct": round((atm_iv(contracts, spot) or 0) * 100, 2) or None,
        })

    if not grid:
        return {"available": False, "reason": "not enough priced strikes to build a surface"}

    # Skew slope: how much IV changes per 1% move down in strike. A steeper
    # negative slope means downside protection is getting relatively pricier.
    for layer in grid:
        pts = layer["points"]
        downside = [p for p in pts if p["moneyness_pct"] < -2]
        upside = [p for p in pts if p["moneyness_pct"] > 2]
        if downside and upside:
            lo = sum(p["iv_pct"] for p in downside) / len(downside)
            hi = sum(p["iv_pct"] for p in upside) / len(upside)
            layer["skew_spread_pts"] = round(lo - hi, 2)
        else:
            layer["skew_spread_pts"] = None

    return {"available": True, "layers": grid, "spot": spot}


def term_and_premium(
    chain: Chain, closes: List[float]
) -> Dict[str, Any]:
    """Term structure of implied vol, plus the variance risk premium."""
    rv = realized_series(closes)
    rv21 = rv.get("rv_21d")

    terms: List[Dict[str, Any]] = []
    for expiry in chain.expiries():
        contracts = chain.for_expiry(expiry)
        iv = atm_iv(contracts, chain.spot)
        if not iv:
            continue
        dte = contracts[0].dte
        premium = None
        if rv21:
            premium = round((iv - rv21) * 100, 2)
        terms.append({
            "expiry": expiry,
            "dte": round(dte, 0),
            "iv_pct": round(iv * 100, 2),
            "premium_vs_rv_pts": premium,
            "daily_move_pct": round(iv / math.sqrt(252) * 100, 2),
            "expected_move_pct": round(iv * math.sqrt(year_fraction(dte)) * 100, 2),
        })

    shape = None
    if len(terms) >= 2:
        front, back = terms[0]["iv_pct"], terms[-1]["iv_pct"]
        if front > back * 1.05:
            shape = ("Inverted: near-dated volatility is bid above longer-dated. "
                     "Usually means a known event sits inside the front expiry, "
                     "or the market is stressed right now.")
        elif back > front * 1.05:
            shape = ("Upward sloping, the normal state. Longer-dated options carry "
                     "more volatility because more can happen over a longer window.")
        else:
            shape = "Flat term structure - no strong event concentration."

    verdict = None
    if rv21 and terms:
        front_iv = terms[0]["iv_pct"] / 100
        ratio = front_iv / rv21 if rv21 else None
        if ratio and ratio > 1.25:
            verdict = (f"Implied volatility ({front_iv * 100:.1f}%) is well above "
                       f"what the stock has actually done ({rv21 * 100:.1f}% over "
                       "21 days). Options look expensive relative to recent "
                       "reality - that favours selling premium, with the usual "
                       "caveat that the market may be pricing something real.")
        elif ratio and ratio < 0.9:
            verdict = (f"Implied volatility ({front_iv * 100:.1f}%) is below "
                       f"realised ({rv21 * 100:.1f}%). Options are cheap relative "
                       "to how much this stock has been moving.")
        else:
            verdict = (f"Implied ({front_iv * 100:.1f}%) and realised "
                       f"({rv21 * 100:.1f}%) volatility are broadly in line.")

    return {
        "available": bool(terms),
        "terms": terms,
        "realized": {k: (round(v * 100, 2) if v else None) for k, v in rv.items()},
        "shape": shape,
        "verdict": verdict,
        "explainer": (
            "Implied volatility is what the option market charges for future "
            "movement. Realised volatility is what the stock actually delivered. "
            "The gap between them is the variance risk premium, and it is "
            "positive most of the time because option sellers demand payment for "
            "carrying risk."
        ),
    }
