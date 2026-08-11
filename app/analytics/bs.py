"""Black-Scholes pricing, implied volatility and greeks.

Pure standard library. Used both to fill in values the free API tier does
not provide, and to reprice a fitted volatility smile onto a dense strike
grid when building the probability distribution.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

SQRT_2PI = math.sqrt(2.0 * math.pi)


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1_d2(S: float, K: float, T: float, r: float, q: float, sigma: float):
    vol_sqrt_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def bs_price(
    S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str
) -> float:
    """European option price. `kind` is "call" or "put"."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic = (S - K) if kind == "call" else (K - S)
        return max(intrinsic, 0.0)
    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if kind == "call":
        return S * disc_q * norm_cdf(d1) - K * disc_r * norm_cdf(d2)
    return K * disc_r * norm_cdf(-d2) - S * disc_q * norm_cdf(-d1)


def bs_vega(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, r, q, sigma)
    return S * math.exp(-q * T) * norm_pdf(d1) * math.sqrt(T)


@dataclass
class Greeks:
    delta: float
    gamma: float
    vega: float
    theta: float

    def as_dict(self):
        return {
            "delta": round(self.delta, 5),
            "gamma": round(self.gamma, 6),
            "vega": round(self.vega, 5),
            "theta": round(self.theta, 5),
        }


def bs_greeks(
    S: float, K: float, T: float, r: float, q: float, sigma: float, kind: str
) -> Greeks:
    """Delta, gamma, vega (per 1 vol point), theta (per calendar day)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        if kind == "call":
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return Greeks(delta, 0.0, 0.0, 0.0)

    d1, d2 = _d1_d2(S, K, T, r, q, sigma)
    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)
    pdf_d1 = norm_pdf(d1)

    gamma = disc_q * pdf_d1 / (S * sigma * math.sqrt(T))
    vega = S * disc_q * pdf_d1 * math.sqrt(T) / 100.0  # per 1% vol move

    if kind == "call":
        delta = disc_q * norm_cdf(d1)
        theta = (
            -S * disc_q * pdf_d1 * sigma / (2.0 * math.sqrt(T))
            - r * K * disc_r * norm_cdf(d2)
            + q * S * disc_q * norm_cdf(d1)
        )
    else:
        delta = -disc_q * norm_cdf(-d1)
        theta = (
            -S * disc_q * pdf_d1 * sigma / (2.0 * math.sqrt(T))
            + r * K * disc_r * norm_cdf(-d2)
            - q * S * disc_q * norm_cdf(-d1)
        )

    return Greeks(delta, gamma, vega, theta / 365.0)


def implied_vol(
    price: float,
    S: float,
    K: float,
    T: float,
    r: float = 0.04,
    q: float = 0.0,
    kind: str = "call",
    lo: float = 1e-4,
    hi: float = 6.0,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> Optional[float]:
    """Invert Black-Scholes for volatility. Returns None if not solvable.

    Uses Brent's method on a bracketed root, which is far more robust than
    Newton-Raphson near the wings where vega collapses.
    """
    if price is None or price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None

    # No-arbitrage bounds. A price outside these cannot be inverted.
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if kind == "call":
        intrinsic = max(S * disc_q - K * disc_r, 0.0)
        upper = S * disc_q
    else:
        intrinsic = max(K * disc_r - S * disc_q, 0.0)
        upper = K * disc_r
    if price <= intrinsic + 1e-10 or price >= upper - 1e-10:
        return None

    def f(sigma: float) -> float:
        return bs_price(S, K, T, r, q, sigma, kind) - price

    f_lo, f_hi = f(lo), f(hi)
    if f_lo * f_hi > 0:
        return None

    # Brent's method
    a, b = lo, hi
    fa, fb = f_lo, f_hi
    if abs(fa) < abs(fb):
        a, b, fa, fb = b, a, fb, fa
    c, fc = a, fa
    mflag = True
    d = 0.0

    for _ in range(max_iter):
        if abs(fb) < tol or abs(b - a) < tol:
            return b
        if fa != fc and fb != fc:
            s = (
                a * fb * fc / ((fa - fb) * (fa - fc))
                + b * fa * fc / ((fb - fa) * (fb - fc))
                + c * fa * fb / ((fc - fa) * (fc - fb))
            )
        else:
            s = b - fb * (b - a) / (fb - fa) if fb != fa else (a + b) / 2.0

        cond1 = not ((3 * a + b) / 4.0 < s < b or b < s < (3 * a + b) / 4.0)
        cond2 = mflag and abs(s - b) >= abs(b - c) / 2.0
        cond3 = (not mflag) and abs(s - b) >= abs(c - d) / 2.0
        cond4 = mflag and abs(b - c) < tol
        cond5 = (not mflag) and abs(c - d) < tol
        if cond1 or cond2 or cond3 or cond4 or cond5:
            s = (a + b) / 2.0
            mflag = True
        else:
            mflag = False

        fs = f(s)
        d, c, fc = c, b, fb
        if fa * fs < 0:
            b, fb = s, fs
        else:
            a, fa = s, fs
        if abs(fa) < abs(fb):
            a, b, fa, fb = b, a, fb, fa

    return b if abs(fb) < 1e-3 else None


def year_fraction(days: float) -> float:
    """Calendar days to years, floored so same-day expiries stay finite."""
    return max(days, 0.5) / 365.0
