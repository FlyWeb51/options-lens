"""Where is the market pricing this stock to go?

Two answers, from most to least assumption-heavy:

1. Expected move  - the straddle-implied one standard deviation range.
2. Risk-neutral distribution - the full probability distribution implied by
   the option prices, via Breeden-Litzenberger. The second derivative of
   call price with respect to strike is the discounted probability density
   of the underlying at expiration.

Important framing: this is the *risk-neutral* distribution. It is what the
market charges for each outcome, not a forecast of what will happen. It is
skewed by hedging demand and embeds a risk premium, so tail probabilities
are typically overstated relative to real-world frequencies.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from ..models import Chain, Contract
from .bs import bs_price, norm_cdf, year_fraction


# ---------------------------------------------------------------------------
# Natural cubic spline (no numpy/scipy dependency)
# ---------------------------------------------------------------------------

class CubicSpline:
    """Natural cubic spline with flat extrapolation beyond the knots."""

    def __init__(self, xs: Sequence[float], ys: Sequence[float]):
        pairs = sorted(zip(xs, ys))
        self.x = [p[0] for p in pairs]
        self.y = [p[1] for p in pairs]
        n = len(self.x)
        if n < 3:
            self.c = [0.0] * max(n, 1)
            return

        h = [self.x[i + 1] - self.x[i] for i in range(n - 1)]
        alpha = [0.0] * n
        for i in range(1, n - 1):
            alpha[i] = (
                3.0 * (self.y[i + 1] - self.y[i]) / h[i]
                - 3.0 * (self.y[i] - self.y[i - 1]) / h[i - 1]
            )

        l = [1.0] + [0.0] * (n - 1)
        mu = [0.0] * n
        z = [0.0] * n
        for i in range(1, n - 1):
            l[i] = 2.0 * (self.x[i + 1] - self.x[i - 1]) - h[i - 1] * mu[i - 1]
            if abs(l[i]) < 1e-12:
                l[i] = 1e-12
            mu[i] = h[i] / l[i]
            z[i] = (alpha[i] - h[i - 1] * z[i - 1]) / l[i]

        self.c = [0.0] * n
        self.b = [0.0] * n
        self.d = [0.0] * n
        for j in range(n - 2, -1, -1):
            self.c[j] = z[j] - mu[j] * self.c[j + 1]
            self.b[j] = (self.y[j + 1] - self.y[j]) / h[j] - h[j] * (
                self.c[j + 1] + 2.0 * self.c[j]
            ) / 3.0
            self.d[j] = (self.c[j + 1] - self.c[j]) / (3.0 * h[j])

    def __call__(self, xq: float) -> float:
        n = len(self.x)
        if n == 0:
            return 0.0
        if n < 3:
            return self.y[0]
        if xq <= self.x[0]:
            return self.y[0]
        if xq >= self.x[-1]:
            return self.y[-1]

        lo, hi = 0, n - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.x[mid] <= xq:
                lo = mid
            else:
                hi = mid
        dx = xq - self.x[lo]
        return self.y[lo] + self.b[lo] * dx + self.c[lo] * dx**2 + self.d[lo] * dx**3


# ---------------------------------------------------------------------------
# Volatility surface helpers
# ---------------------------------------------------------------------------

def _usable(contracts: List[Contract]) -> List[Contract]:
    return [c for c in contracts if c.iv and 0.01 < c.iv < 5.0 and c.price]


def atm_iv(contracts: List[Contract], spot: float) -> Optional[float]:
    """Interpolate implied volatility at the money."""
    usable = _usable(contracts)
    if not usable:
        return None
    usable.sort(key=lambda c: abs(c.strike - spot))
    near = usable[:6]
    weights = [1.0 / (abs(c.strike - spot) + 0.01) for c in near]
    total = sum(weights)
    return sum(c.iv * w for c, w in zip(near, weights)) / total if total else None


def otm_iv_curve(
    contracts: List[Contract], spot: float
) -> List[Tuple[float, float]]:
    """Out-of-the-money implied volatility by strike.

    OTM options carry all the information and none of the intrinsic value,
    so they invert far more cleanly than in-the-money contracts.
    """
    best: Dict[float, Tuple[float, float]] = {}
    for c in _usable(contracts):
        is_otm = (c.kind == "call" and c.strike >= spot) or (
            c.kind == "put" and c.strike <= spot
        )
        if not is_otm:
            continue
        # Prefer the contract with tighter pricing when both types exist.
        score = abs(c.strike - spot)
        prior = best.get(c.strike)
        if prior is None or score < prior[1]:
            best[c.strike] = (c.iv, score)
    return sorted((k, v[0]) for k, v in best.items())


def _smooth(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Light 3-point moving average to stop noisy quotes wrecking the density."""
    if len(points) < 5:
        return points
    out = [points[0]]
    for i in range(1, len(points) - 1):
        k = points[i][0]
        v = (points[i - 1][1] + points[i][1] + points[i + 1][1]) / 3.0
        out.append((k, v))
    out.append(points[-1])
    return out


# ---------------------------------------------------------------------------
# Expected move
# ---------------------------------------------------------------------------

def expected_move(contracts: List[Contract], spot: float, risk_free: float) -> Dict:
    """Straddle-implied and volatility-implied one standard deviation move."""
    if not contracts:
        return {"available": False, "reason": "no contracts"}

    dte = contracts[0].dte
    T = year_fraction(dte)

    calls = {c.strike: c for c in contracts if c.kind == "call" and c.price}
    puts = {c.strike: c for c in contracts if c.kind == "put" and c.price}
    paired = sorted(set(calls) & set(puts), key=lambda k: abs(k - spot))

    straddle_move = None
    straddle_strike = None
    if paired:
        k = paired[0]
        straddle = calls[k].price + puts[k].price
        straddle_strike = k
        # The classic approximation: an at-the-money straddle costs roughly
        # 0.8 standard deviations, so scale it up to a true 1 sigma.
        straddle_move = (straddle / spot) * 1.25

    iv = atm_iv(contracts, spot)
    iv_move = iv * math.sqrt(T) if iv else None

    move = straddle_move if straddle_move is not None else iv_move
    if move is None:
        return {"available": False, "reason": "no priceable at-the-money contracts"}

    return {
        "available": True,
        "expiry": contracts[0].expiry,
        "dte": round(dte, 1),
        "atm_iv": round(iv, 4) if iv else None,
        "straddle_strike": straddle_strike,
        "move_pct": round(move * 100, 2),
        "move_dollars": round(move * spot, 2),
        "low": round(spot * (1 - move), 2),
        "high": round(spot * (1 + move), 2),
        "two_sigma_low": round(spot * (1 - 2 * move), 2),
        "two_sigma_high": round(spot * (1 + 2 * move), 2),
        "source": "straddle" if straddle_move is not None else "atm_iv",
    }


# ---------------------------------------------------------------------------
# Risk-neutral distribution (Breeden-Litzenberger)
# ---------------------------------------------------------------------------

def risk_neutral_distribution(
    contracts: List[Contract],
    spot: float,
    risk_free: float,
    grid_points: int = 401,
) -> Dict:
    """Recover the market-implied probability density of price at expiry."""
    dte = contracts[0].dte if contracts else 30.0
    T = year_fraction(dte)
    curve = _smooth(otm_iv_curve(contracts, spot))

    fallback_iv = atm_iv(contracts, spot)
    method = "breeden-litzenberger"

    if len(curve) < 5:
        if not fallback_iv:
            return {"available": False, "reason": "not enough priced strikes"}
        method = "lognormal (single volatility - too few strikes for a smile)"
        spline = None
    else:
        spline = CubicSpline([k for k, _ in curve], [v for _, v in curve])

    def iv_at(k: float) -> float:
        if spline is None:
            return fallback_iv or 0.3
        return max(spline(k), 0.01)

    # Grid width scales with volatility so high-vol names get full tails.
    base_iv = fallback_iv or 0.4
    width = max(4.0 * base_iv * math.sqrt(T), 0.25)
    k_lo = max(spot * (1 - width), 0.01)
    k_hi = spot * (1 + width)
    step = (k_hi - k_lo) / (grid_points - 1)

    strikes = [k_lo + i * step for i in range(grid_points)]
    call_prices = [
        bs_price(spot, k, T, risk_free, 0.0, iv_at(k), "call") for k in strikes
    ]

    # density = e^{rT} * second derivative of call price with respect to strike
    disc = math.exp(risk_free * T)
    density = [0.0] * grid_points
    for i in range(1, grid_points - 1):
        second = (call_prices[i + 1] - 2 * call_prices[i] + call_prices[i - 1]) / (
            step * step
        )
        density[i] = max(second * disc, 0.0)
    density[0] = density[1]
    density[-1] = density[-2]

    area = sum(density) * step
    if area <= 0:
        return {"available": False, "reason": "density collapsed"}
    density = [d / area for d in density]

    # Cumulative distribution
    cdf: List[float] = []
    running = 0.0
    for i, d in enumerate(density):
        running += d * step
        cdf.append(min(running, 1.0))

    def prob_below(level: float) -> float:
        if level <= k_lo:
            return 0.0
        if level >= k_hi:
            return 1.0
        idx = int((level - k_lo) / step)
        idx = max(0, min(idx, grid_points - 2))
        frac = (level - strikes[idx]) / step
        return cdf[idx] + frac * (cdf[idx + 1] - cdf[idx])

    def quantile(p: float) -> float:
        for i, value in enumerate(cdf):
            if value >= p:
                if i == 0:
                    return strikes[0]
                prev = cdf[i - 1]
                span = value - prev
                frac = (p - prev) / span if span > 1e-12 else 0.0
                return strikes[i - 1] + frac * step
        return strikes[-1]

    mean = sum(s * d for s, d in zip(strikes, density)) * step
    var = sum(((s - mean) ** 2) * d for s, d in zip(strikes, density)) * step
    third = sum(((s - mean) ** 3) * d for s, d in zip(strikes, density)) * step
    sd = math.sqrt(max(var, 1e-12))
    skew = third / (sd**3) if sd > 0 else 0.0

    # Downsample the curve for the browser.
    stride = max(grid_points // 160, 1)
    curve_out = [
        {"price": round(strikes[i], 2), "density": density[i]}
        for i in range(0, grid_points, stride)
    ]

    return {
        "available": True,
        "method": method,
        "expiry": contracts[0].expiry if contracts else None,
        "dte": round(dte, 1),
        "curve": curve_out,
        "mean": round(mean, 2),
        "median": round(quantile(0.5), 2),
        "mode": round(strikes[density.index(max(density))], 2),
        "std_dev": round(sd, 2),
        "skew": round(skew, 3),
        "percentiles": {
            "p5": round(quantile(0.05), 2),
            "p10": round(quantile(0.10), 2),
            "p25": round(quantile(0.25), 2),
            "p50": round(quantile(0.50), 2),
            "p75": round(quantile(0.75), 2),
            "p90": round(quantile(0.90), 2),
            "p95": round(quantile(0.95), 2),
        },
        "prob_above_spot": round((1.0 - prob_below(spot)) * 100, 1),
        "strike_ladder": _strike_ladder(spot, prob_below),
        "iv_smile": [{"strike": k, "iv": round(v, 4)} for k, v in curve],
    }


def _strike_ladder(spot: float, prob_below) -> List[Dict]:
    """Probability of finishing above each of a set of round price levels."""
    ladder = []
    for pct in (-30, -25, -20, -15, -10, -5, 0, 5, 10, 15, 20, 25, 30):
        level = spot * (1 + pct / 100.0)
        p_above = (1.0 - prob_below(level)) * 100.0
        ladder.append(
            {
                "move_pct": pct,
                "price": round(level, 2),
                "prob_above": round(p_above, 1),
                "prob_below": round(100.0 - p_above, 1),
            }
        )
    return ladder


# ---------------------------------------------------------------------------
# Term structure
# ---------------------------------------------------------------------------

def term_structure(chain: Chain) -> List[Dict]:
    """At-the-money implied volatility and expected move by expiration."""
    out = []
    for expiry in chain.expiries():
        contracts = chain.for_expiry(expiry)
        iv = atm_iv(contracts, chain.spot)
        move = expected_move(contracts, chain.spot, chain.risk_free)
        out.append(
            {
                "expiry": expiry,
                "dte": round(contracts[0].dte, 1) if contracts else None,
                "atm_iv": round(iv * 100, 2) if iv else None,
                "expected_move_pct": move.get("move_pct"),
                "contracts": len(contracts),
            }
        )
    return out
