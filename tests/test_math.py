"""Verification of the pricing and probability maths.

Run with:  python -m tests.test_math
No test framework needed.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics.bs import bs_greeks, bs_price, implied_vol, norm_cdf
from app.analytics.pricing import (
    CubicSpline,
    expected_move,
    risk_neutral_distribution,
)
from app.models import Contract

FAILURES = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def close(a: float, b: float, tol: float = 1e-3) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------

def test_black_scholes():
    print("\nBlack-Scholes against published reference values")
    # Hull, Options Futures and Other Derivatives: S=100 K=100 T=1 r=5% vol=20%
    call = bs_price(100, 100, 1.0, 0.05, 0.0, 0.20, "call")
    put = bs_price(100, 100, 1.0, 0.05, 0.0, 0.20, "put")
    check("call = 10.4506", close(call, 10.450584, 1e-4), f"got {call:.6f}")
    check("put = 5.5735", close(put, 5.573526, 1e-4), f"got {put:.6f}")

    # Put-call parity: C - P = S*e^-qT - K*e^-rT
    parity = call - put
    expected = 100 - 100 * math.exp(-0.05)
    check("put-call parity holds", close(parity, expected, 1e-8),
          f"{parity:.8f} vs {expected:.8f}")

    # Deep in the money call approaches its discounted intrinsic value
    deep = bs_price(200, 50, 1.0, 0.05, 0.0, 0.20, "call")
    intrinsic = 200 - 50 * math.exp(-0.05)
    check("deep ITM call = discounted intrinsic", close(deep, intrinsic, 0.01),
          f"{deep:.4f} vs {intrinsic:.4f}")

    # Zero volatility collapses to intrinsic
    zero = bs_price(110, 100, 0.5, 0.0, 0.0, 0.0, "call")
    check("zero vol call = intrinsic", close(zero, 10.0), f"got {zero}")


def test_implied_vol():
    print("\nImplied volatility solver round-trips")
    cases = [
        (100, 100, 1.0, 0.05, 0.20, "call"),
        (100, 120, 0.25, 0.04, 0.55, "call"),
        (100, 80, 0.08, 0.04, 0.90, "put"),
        (57.3, 60.0, 0.14, 0.042, 0.35, "call"),
        (100, 100, 2.0, 0.03, 0.12, "put"),
        (250, 180, 0.5, 0.05, 0.42, "put"),
    ]
    worst = 0.0
    for S, K, T, r, vol, kind in cases:
        price = bs_price(S, K, T, r, 0.0, vol, kind)
        recovered = implied_vol(price, S, K, T, r, 0.0, kind)
        if recovered is None:
            check(f"recovered vol for K={K} {kind}", False, "returned None")
            continue
        err = abs(recovered - vol)
        worst = max(worst, err)
        check(f"K={K} {kind}: {vol:.2f} -> {recovered:.4f}", err < 1e-4,
              f"error {err:.2e}")
    print(f"  worst absolute volatility error: {worst:.2e}")

    # Unsolvable inputs must return None rather than a wrong number
    check("price below intrinsic returns None",
          implied_vol(0.001, 100, 50, 1.0, 0.05, 0.0, "call") is None)
    check("zero price returns None",
          implied_vol(0.0, 100, 100, 1.0, 0.05, 0.0, "call") is None)


def test_greeks():
    print("\nGreeks")
    g = bs_greeks(100, 100, 1.0, 0.05, 0.0, 0.20, "call")
    # d1 = (ln(1) + (0.05 + 0.02)) / 0.2 = 0.35 -> N(d1) = 0.63683
    check("ATM call delta = N(d1) = 0.6368", close(g.delta, 0.636831, 1e-5),
          f"got {g.delta:.6f}")
    check("gamma positive", g.gamma > 0)
    check("theta negative for long option", g.theta < 0)

    gp = bs_greeks(100, 100, 1.0, 0.05, 0.0, 0.20, "put")
    check("call delta - put delta = 1", close(g.delta - gp.delta, 1.0, 1e-9))
    check("call and put gamma identical", close(g.gamma, gp.gamma, 1e-12))

    # Gamma via finite difference of delta
    h = 0.01
    d_up = bs_greeks(100 + h, 100, 1.0, 0.05, 0.0, 0.20, "call").delta
    d_dn = bs_greeks(100 - h, 100, 1.0, 0.05, 0.0, 0.20, "call").delta
    numeric_gamma = (d_up - d_dn) / (2 * h)
    check("gamma matches finite difference of delta",
          close(g.gamma, numeric_gamma, 1e-6),
          f"{g.gamma:.8f} vs {numeric_gamma:.8f}")


def test_spline():
    print("\nCubic spline")
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [x**2 for x in xs]
    s = CubicSpline(xs, ys)
    check("passes through knots", all(close(s(x), y, 1e-9) for x, y in zip(xs, ys)))
    check("interpolates smoothly (2.5 -> ~6.25)", abs(s(2.5) - 6.25) < 0.2,
          f"got {s(2.5):.4f}")
    check("flat extrapolation below range", close(s(0.0), 1.0))
    check("flat extrapolation above range", close(s(9.0), 25.0))


def _synthetic_chain(spot=100.0, vol=0.30, T_days=30.0, r=0.04):
    """A chain priced from a single flat volatility.

    The recovered distribution should therefore be lognormal, which gives
    us closed-form answers to check against.
    """
    T = T_days / 365.0
    contracts = []
    for i in range(-20, 21):
        strike = round(spot * (1 + i * 0.02), 2)
        if strike <= 0:
            continue
        for kind in ("call", "put"):
            price = bs_price(spot, strike, T, r, 0.0, vol, kind)
            greeks = bs_greeks(spot, strike, T, r, 0.0, vol, kind)
            contracts.append(
                Contract(
                    ticker=f"O:TEST{kind[0].upper()}{strike}",
                    underlying="TEST",
                    kind=kind,
                    strike=strike,
                    expiry="2026-09-18",
                    dte=T_days,
                    mid=price,
                    volume=100,
                    open_interest=500,
                    iv=vol,
                    delta=greeks.delta,
                    gamma=greeks.gamma,
                    vega=greeks.vega,
                    theta=greeks.theta,
                    iv_source="api",
                    price_source="quote",
                )
            )
    return contracts


def test_distribution():
    print("\nRisk-neutral distribution (Breeden-Litzenberger)")
    spot, vol, T_days, r = 100.0, 0.30, 30.0, 0.04
    T = T_days / 365.0
    contracts = _synthetic_chain(spot, vol, T_days, r)

    d = risk_neutral_distribution(contracts, spot, r)
    check("distribution available", d.get("available") is True, str(d.get("reason")))

    # Density must integrate to 1 - reconstruct from the downsampled curve
    curve = d["curve"]
    step = curve[1]["price"] - curve[0]["price"]
    area = sum(p["density"] for p in curve) * step
    check("density integrates to 1", close(area, 1.0, 0.02), f"got {area:.4f}")
    check("density never negative", all(p["density"] >= 0 for p in curve))

    # Lognormal closed form: median = S*exp((r - vol^2/2)*T)
    theoretical_median = spot * math.exp((r - 0.5 * vol * vol) * T)
    check("median matches lognormal closed form",
          close(d["median"], theoretical_median, 0.6),
          f"{d['median']:.3f} vs {theoretical_median:.3f}")

    # Mean of the risk-neutral density is the forward price
    forward = spot * math.exp(r * T)
    check("mean equals the forward price", close(d["mean"], forward, 0.6),
          f"{d['mean']:.3f} vs {forward:.3f}")

    # P(S_T > S_0) closed form = N(d2) with K = S_0
    d2 = (math.log(spot / spot) + (r - 0.5 * vol**2) * T) / (vol * math.sqrt(T))
    theoretical_above = norm_cdf(d2) * 100
    check("P(above spot) matches closed form",
          close(d["prob_above_spot"], theoretical_above, 1.5),
          f"{d['prob_above_spot']:.2f}% vs {theoretical_above:.2f}%")

    # Ladder must be monotonic: higher price -> lower chance of exceeding it
    ladder = d["strike_ladder"]
    probs = [row["prob_above"] for row in ladder]
    check("ladder probabilities decrease as price rises",
          all(probs[i] >= probs[i + 1] - 1e-9 for i in range(len(probs) - 1)))
    check("ladder probabilities all within 0-100",
          all(0 <= p <= 100 for p in probs))

    # Percentiles must be ordered
    p = d["percentiles"]
    ordered = [p["p5"], p["p10"], p["p25"], p["p50"], p["p75"], p["p90"], p["p95"]]
    check("percentiles are ordered",
          all(ordered[i] <= ordered[i + 1] for i in range(len(ordered) - 1)))

    # Flat volatility means near-zero skew
    check("flat vol surface gives near-symmetric skew", abs(d["skew"]) < 0.6,
          f"skew {d['skew']}")


def test_skewed_surface():
    print("\nDistribution responds correctly to a volatility smirk")
    spot, T_days, r = 100.0, 45.0, 0.04
    T = T_days / 365.0
    contracts = []
    for i in range(-20, 21):
        strike = round(spot * (1 + i * 0.02), 2)
        # Downside puts priced richer - the standard equity smirk
        vol = 0.30 + max(0.0, (spot - strike) / spot) * 0.7
        for kind in ("call", "put"):
            price = bs_price(spot, strike, T, r, 0.0, vol, kind)
            g = bs_greeks(spot, strike, T, r, 0.0, vol, kind)
            contracts.append(
                Contract(
                    ticker="O:SKEW", underlying="SKEW", kind=kind, strike=strike,
                    expiry="2026-09-25", dte=T_days, mid=price, volume=50,
                    open_interest=200, iv=vol, delta=g.delta, gamma=g.gamma,
                    iv_source="api", price_source="quote",
                )
            )
    d = risk_neutral_distribution(contracts, spot, r)
    check("distribution available", d.get("available") is True)
    check("downside smirk produces negative skew", d["skew"] < 0,
          f"skew {d['skew']}")
    check("left tail is fatter than right",
          (d["percentiles"]["p50"] - d["percentiles"]["p5"])
          > (d["percentiles"]["p95"] - d["percentiles"]["p50"]))


def test_expected_move():
    print("\nExpected move")
    spot, vol, T_days, r = 100.0, 0.30, 30.0, 0.04
    T = T_days / 365.0
    contracts = _synthetic_chain(spot, vol, T_days, r)
    m = expected_move(contracts, spot, r)
    check("expected move available", m["available"] is True)

    theoretical = vol * math.sqrt(T) * 100  # one sigma in percent
    check("straddle-implied move within 15% of theoretical sigma",
          abs(m["move_pct"] - theoretical) / theoretical < 0.15,
          f"{m['move_pct']:.2f}% vs {theoretical:.2f}%")
    check("range brackets spot", m["low"] < spot < m["high"])
    check("two sigma range is wider than one sigma",
          m["two_sigma_high"] > m["high"] and m["two_sigma_low"] < m["low"])
    check("recovered ATM IV matches input",
          close(m["atm_iv"], vol, 0.02), f"got {m['atm_iv']}")


def test_iv_recovery_from_prices_only():
    print("\nEnd-to-end: recover volatility from prices alone (free-tier path)")
    spot, vol, T_days, r = 87.5, 0.44, 21.0, 0.042
    T = T_days / 365.0
    errors = []
    for i in range(-10, 11):
        strike = round(spot * (1 + i * 0.03), 2)
        for kind in ("call", "put"):
            price = bs_price(spot, strike, T, r, 0.0, vol, kind)
            if price < 0.01:
                continue
            recovered = implied_vol(price, spot, strike, T, r, 0.0, kind)
            if recovered is not None:
                errors.append(abs(recovered - vol))
    check("recovered volatility on most strikes", len(errors) > 25,
          f"only {len(errors)} strikes solved")
    check("max recovery error under 0.1 vol points", max(errors) < 0.001,
          f"max {max(errors):.2e}")


if __name__ == "__main__":
    test_black_scholes()
    test_implied_vol()
    test_greeks()
    test_spline()
    test_distribution()
    test_skewed_surface()
    test_expected_move()
    test_iv_recovery_from_prices_only()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All checks passed.")
