"""Time decay: what holding an option costs you per day, and when it hurts.

Theta is not linear. An at-the-money option loses value roughly with the
square root of time remaining, which means decay accelerates sharply in the
last few weeks. Out-of-the-money options decay more evenly but end at zero.
Showing the actual curve makes that concrete in a way a single theta number
never does.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from ..models import Chain, Contract
from .bs import bs_greeks, bs_price, year_fraction


def decay_curve(
    contract: Contract, spot: float, risk_free: float, steps: int = 40
) -> List[Dict[str, Any]]:
    """Value of this contract from today to expiry, holding spot and IV fixed."""
    if not contract.iv or contract.dte <= 0:
        return []

    out = []
    for i in range(steps + 1):
        days_left = contract.dte * (1 - i / steps)
        T = year_fraction(days_left)
        value = bs_price(spot, contract.strike, T, risk_free, 0.0,
                         contract.iv, contract.kind)
        greeks = bs_greeks(spot, contract.strike, T, risk_free, 0.0,
                           contract.iv, contract.kind)
        out.append({
            "days_left": round(days_left, 1),
            "days_held": round(contract.dte - days_left, 1),
            "value": round(value, 4),
            "theta_per_day": round(greeks.theta, 4),
        })
    return out


def analyse(chain: Chain, max_contracts: int = 6) -> Dict[str, Any]:
    """Decay profiles for the most relevant contracts, plus a plain summary."""
    spot = chain.spot
    priced = [c for c in chain.contracts if c.iv and c.price and c.dte > 0]
    if not priced:
        return {"available": False, "reason": "no priced contracts with volatility"}

    # Pick a spread: nearest at-the-money call and put across the front
    # expiries, since that's what people actually consider buying.
    picks: List[Contract] = []
    for expiry in chain.expiries()[:3]:
        for kind in ("call", "put"):
            candidates = [c for c in priced if c.expiry == expiry and c.kind == kind]
            if candidates:
                picks.append(min(candidates, key=lambda c: abs(c.strike - spot)))
    picks = picks[:max_contracts]

    profiles = []
    for c in picks:
        curve = decay_curve(c, spot, chain.risk_free)
        if not curve:
            continue
        price = c.price or 0
        theta = c.theta or 0
        # How much of today's premium evaporates per day, as a percentage.
        burn_pct = (abs(theta) / price * 100) if price else None
        # Break-even move needed just to offset one day of decay.
        delta = abs(c.delta or 0)
        breakeven_move = (abs(theta) / delta) if delta > 0.01 else None

        half_life = None
        for point in curve:
            if price and point["value"] <= price / 2:
                half_life = point["days_held"]
                break

        profiles.append({
            "ticker": c.ticker,
            "kind": c.kind,
            "strike": c.strike,
            "expiry": c.expiry,
            "dte": round(c.dte, 0),
            "price": round(price, 3),
            "iv_pct": round(c.iv * 100, 1),
            "delta": round(c.delta, 3) if c.delta is not None else None,
            "theta_per_day": round(theta, 4),
            "theta_pct_of_premium": round(burn_pct, 2) if burn_pct else None,
            "breakeven_daily_move": round(breakeven_move, 3) if breakeven_move else None,
            "breakeven_daily_move_pct": round(breakeven_move / spot * 100, 3)
            if breakeven_move else None,
            "half_life_days": half_life,
            "total_premium_at_risk": round(price * 100, 2),
            "curve": curve,
        })

    if not profiles:
        return {"available": False, "reason": "could not build decay curves"}

    front = profiles[0]
    summary = (
        f"The nearest at-the-money {front['kind']} costs "
        f"${front['total_premium_at_risk']:.0f} per contract and bleeds "
        f"${abs(front['theta_per_day']) * 100:.2f} a day at the current price"
    )
    if front.get("theta_pct_of_premium"):
        summary += f", which is {front['theta_pct_of_premium']:.1f}% of its premium daily"
    if front.get("breakeven_daily_move_pct"):
        summary += (f". The stock needs to move about "
                    f"{front['breakeven_daily_move_pct']:.2f}% in your favour each "
                    "day just to break even against decay")
    summary += "."

    return {
        "available": True,
        "profiles": profiles,
        "summary": summary,
        "explainer": (
            "Theta is the value an option loses per calendar day if nothing else "
            "changes. It accelerates as expiry approaches for at-the-money "
            "options - the curve steepens in the final weeks. Half-life is how "
            "many days until the option is worth half what it costs now, "
            "assuming the stock does not move."
        ),
    }
