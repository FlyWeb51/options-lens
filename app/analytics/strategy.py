"""Strategy payoff builder with market-implied probability of profit.

Most payoff calculators show you the shape of a trade. This one also runs
the payoff through the risk-neutral density recovered from the chain, so
you get the market's own odds on each outcome rather than a guess.

That is the honest version of "probability of profit": it is the
probability the option market is charging for, which already includes a
risk premium.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

PRESETS = {
    "long_call": {"label": "Long call", "legs": [{"kind": "call", "qty": 1}]},
    "long_put": {"label": "Long put", "legs": [{"kind": "put", "qty": 1}]},
    "covered_call": {"label": "Covered call",
                     "legs": [{"kind": "call", "qty": -1}, {"kind": "stock", "qty": 1}]},
    "cash_secured_put": {"label": "Cash secured put",
                         "legs": [{"kind": "put", "qty": -1}]},
    "call_spread": {"label": "Bull call spread",
                    "legs": [{"kind": "call", "qty": 1, "strike_offset": 0.0},
                             {"kind": "call", "qty": -1, "strike_offset": 0.08}]},
    "put_spread": {"label": "Bear put spread",
                   "legs": [{"kind": "put", "qty": 1, "strike_offset": 0.0},
                            {"kind": "put", "qty": -1, "strike_offset": -0.08}]},
    "straddle": {"label": "Long straddle",
                 "legs": [{"kind": "call", "qty": 1}, {"kind": "put", "qty": 1}]},
    "iron_condor": {"label": "Iron condor",
                    "legs": [{"kind": "put", "qty": -1, "strike_offset": -0.05},
                             {"kind": "put", "qty": 1, "strike_offset": -0.12},
                             {"kind": "call", "qty": -1, "strike_offset": 0.05},
                             {"kind": "call", "qty": 1, "strike_offset": 0.12}]},
}

MULTIPLIER = 100.0


def _leg_payoff(leg: Dict[str, Any], price_at_expiry: float) -> float:
    kind = leg["kind"]
    qty = leg["qty"]
    if kind == "stock":
        return qty * (price_at_expiry - leg["entry"]) * MULTIPLIER
    strike = leg["strike"]
    premium = leg["premium"]
    if kind == "call":
        intrinsic = max(price_at_expiry - strike, 0.0)
    else:
        intrinsic = max(strike - price_at_expiry, 0.0)
    # Long pays premium up front and receives intrinsic; short is the mirror.
    return qty * (intrinsic - premium) * MULTIPLIER


def evaluate(
    legs: List[Dict[str, Any]],
    spot: float,
    density_curve: Optional[List[Dict[str, float]]] = None,
) -> Dict[str, Any]:
    """Payoff across price, plus probability-weighted outcomes."""
    if not legs:
        return {"available": False, "reason": "no legs supplied"}

    for leg in legs:
        if leg.get("kind") == "stock":
            leg.setdefault("entry", spot)
        else:
            if leg.get("strike") is None or leg.get("premium") is None:
                return {"available": False,
                        "reason": "each option leg needs a strike and a premium"}

    lo = max(spot * 0.4, 0.01)
    hi = spot * 1.8
    steps = 240
    step = (hi - lo) / steps

    points = []
    for i in range(steps + 1):
        price = lo + i * step
        pnl = sum(_leg_payoff(leg, price) for leg in legs)
        points.append({"price": round(price, 2), "pnl": round(pnl, 2)})

    pnls = [p["pnl"] for p in points]
    max_profit = max(pnls)
    max_loss = min(pnls)

    # Break-evens: where the payoff crosses zero.
    breakevens = []
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        if (a["pnl"] < 0) != (b["pnl"] < 0):
            span = b["pnl"] - a["pnl"]
            frac = -a["pnl"] / span if abs(span) > 1e-9 else 0.5
            breakevens.append(round(a["price"] + frac * (b["price"] - a["price"]), 2))

    net_debit = sum(
        leg["qty"] * leg.get("premium", 0) * MULTIPLIER
        for leg in legs if leg.get("kind") != "stock"
    )

    result: Dict[str, Any] = {
        "available": True,
        "points": points,
        "max_profit": None if max_profit >= 1e8 else round(max_profit, 2),
        "max_loss": round(max_loss, 2),
        "breakevens": breakevens,
        "net_cost": round(net_debit, 2),
        "direction": "debit" if net_debit > 0 else "credit",
        "legs": legs,
    }

    if density_curve and len(density_curve) > 3:
        prob = _probability_weighted(points, density_curve)
        result.update(prob)

    return result


def _probability_weighted(
    points: List[Dict[str, float]], density_curve: List[Dict[str, float]]
) -> Dict[str, Any]:
    """Integrate the payoff against the market-implied density."""
    prices = [d["price"] for d in density_curve]
    dens = [d["density"] for d in density_curve]
    if len(prices) < 2:
        return {}
    step = prices[1] - prices[0]

    total = sum(dens) * step
    if total <= 0:
        return {}

    def payoff_at(price: float) -> float:
        if price <= points[0]["price"]:
            return points[0]["pnl"]
        if price >= points[-1]["price"]:
            return points[-1]["pnl"]
        span = points[1]["price"] - points[0]["price"]
        idx = int((price - points[0]["price"]) / span)
        idx = max(0, min(idx, len(points) - 2))
        a, b = points[idx], points[idx + 1]
        frac = (price - a["price"]) / span if span else 0
        return a["pnl"] + frac * (b["pnl"] - a["pnl"])

    p_profit = 0.0
    expected = 0.0
    downside = 0.0
    for price, d in zip(prices, dens):
        weight = d * step / total
        pnl = payoff_at(price)
        expected += pnl * weight
        if pnl > 0:
            p_profit += weight
        else:
            downside += pnl * weight

    return {
        "probability_of_profit_pct": round(p_profit * 100, 1),
        "expected_value": round(expected, 2),
        "expected_downside": round(downside, 2),
        "probability_note": (
            "These odds come from the option chain itself, so they are "
            "risk-neutral. Expected value near zero is the correct result for a "
            "fairly priced trade - the market is not handing out free money. "
            "Use this to compare structures, not to find edge."
        ),
    }


def build_from_chain(
    preset: str, chain_contracts: List[Dict[str, Any]], spot: float, expiry: str
) -> Dict[str, Any]:
    """Turn a preset name into concrete legs using real chain prices."""
    spec = PRESETS.get(preset)
    if not spec:
        return {"available": False, "reason": f"unknown preset {preset}"}

    pool = [c for c in chain_contracts
            if c.get("expiry") == expiry and c.get("price")]
    if not pool:
        return {"available": False, "reason": f"no priced contracts for {expiry}"}

    legs: List[Dict[str, Any]] = []
    for leg in spec["legs"]:
        if leg["kind"] == "stock":
            legs.append({"kind": "stock", "qty": leg["qty"], "entry": spot})
            continue
        target = spot * (1 + leg.get("strike_offset", 0.0))
        candidates = [c for c in pool if c["kind"] == leg["kind"]]
        if not candidates:
            return {"available": False,
                    "reason": f"no {leg['kind']} contracts priced for {expiry}"}
        pick = min(candidates, key=lambda c: abs(c["strike"] - target))
        legs.append({
            "kind": leg["kind"],
            "qty": leg["qty"],
            "strike": pick["strike"],
            "premium": pick["price"],
            "ticker": pick.get("ticker"),
        })

    return {"available": True, "label": spec["label"], "legs": legs}
