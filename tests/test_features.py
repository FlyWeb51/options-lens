"""Tests for the v2 feature set: volatility, decay, strategy, providers.

Run with:  python -m tests.test_features
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MASSIVE_API_KEY", "test-key-not-real")
os.environ["CACHE_PATH"] = "/tmp/options-lens-features-cache.sqlite"

from app.analytics import decay as decay_mod  # noqa: E402
from app.analytics import strategy as strategy_mod  # noqa: E402
from app.analytics import vol as vol_mod  # noqa: E402
from app.analytics.bs import bs_greeks, bs_price  # noqa: E402
from app.analytics.pricing import risk_neutral_distribution  # noqa: E402
from app.config import SPEED_MODES, settings  # noqa: E402
from app.models import Chain, Contract  # noqa: E402
from app.providers import sec  # noqa: E402
from app.sources import registry  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def close(a, b, tol=1e-3):
    return abs(a - b) <= tol


def synthetic_chain(spot=100.0, vol=0.35, rate=0.04):
    contracts = []
    for dte in (14.0, 35.0, 70.0):
        T = dte / 365.0
        for i in range(-8, 9):
            strike = round(spot * (1 + i * 0.03), 2)
            v = vol + max(0.0, (spot - strike) / spot) * 0.5
            for kind in ("call", "put"):
                price = bs_price(spot, strike, T, rate, 0.0, v, kind)
                g = bs_greeks(spot, strike, T, rate, 0.0, v, kind)
                contracts.append(Contract(
                    ticker=f"O:T{int(dte)}{kind[0].upper()}{int(strike*1000):08d}",
                    underlying="TEST", kind=kind, strike=strike,
                    expiry=f"2026-{9 + int(dte // 30)}-18", dte=dte,
                    mid=price, volume=200, open_interest=900, iv=v,
                    delta=g.delta, gamma=g.gamma, vega=g.vega, theta=g.theta,
                    iv_source="api", price_source="quote"))
    return Chain(underlying="TEST", spot=spot, as_of="now", mode="snapshot",
                 contracts=contracts, risk_free=rate)


def geometric_closes(n=300, start=100.0, daily_vol=0.02):
    """Deterministic price path with a known volatility."""
    closes = [start]
    for i in range(1, n):
        step = daily_vol * (1 if i % 2 else -1) * (1 + 0.1 * math.sin(i / 7))
        closes.append(closes[-1] * math.exp(step))
    return closes


def test_realized_vol():
    print("\nRealised volatility")
    closes = [100.0 * math.exp(0.01 * i) for i in range(100)]
    rv = vol_mod.realized_volatility(closes, 21)
    check("constant drift gives near-zero volatility", rv is not None and rv < 0.02,
          f"got {rv}")

    closes = geometric_closes(200, daily_vol=0.02)
    rv = vol_mod.realized_volatility(closes, 63)
    expected = 0.02 * math.sqrt(252)
    check("alternating 2% moves annualise near 32%",
          rv is not None and abs(rv - expected) < 0.10,
          f"got {rv:.4f} vs {expected:.4f}")

    check("too little data returns None",
          vol_mod.realized_volatility([100.0, 101.0], 21) is None)


def test_surface_and_terms():
    print("\nVolatility surface and term structure")
    chain = synthetic_chain()
    surf = vol_mod.surface(chain)
    check("surface available", surf.get("available") is True, str(surf.get("reason")))
    check("one layer per expiry", len(surf["layers"]) == 3, str(len(surf["layers"])))
    check("downside smirk gives positive skew spread",
          all(l["skew_spread_pts"] > 0 for l in surf["layers"]),
          str([l["skew_spread_pts"] for l in surf["layers"]]))

    closes = geometric_closes(200, daily_vol=0.005)
    vt = vol_mod.term_and_premium(chain, closes)
    check("term structure available", vt["available"] is True)
    check("terms sorted by expiry", len(vt["terms"]) == 3)
    check("realised vol computed", vt["realized"]["rv_21d"] is not None)
    check("premium computed vs realised",
          vt["terms"][0]["premium_vs_rv_pts"] is not None)
    check("high implied vs low realised reads as expensive",
          "expensive" in (vt["verdict"] or ""), vt["verdict"])


def test_decay():
    print("\nTime decay")
    chain = synthetic_chain()
    d = decay_mod.analyse(chain)
    check("decay available", d.get("available") is True, str(d.get("reason")))
    p = d["profiles"][0]
    check("curve runs to expiry", p["curve"][-1]["days_left"] == 0.0)
    check("value decays monotonically for ATM option",
          all(p["curve"][i]["value"] >= p["curve"][i + 1]["value"] - 1e-6
              for i in range(len(p["curve"]) - 1)))
    check("theta is negative", p["theta_per_day"] < 0)
    check("half life within contract life",
          p["half_life_days"] is None or 0 < p["half_life_days"] <= p["dte"],
          str(p["half_life_days"]))
    check("summary mentions a dollar cost", "$" in d["summary"])


def test_strategy():
    print("\nStrategy payoffs")
    spot = 100.0

    # Long call: max loss is the premium, upside unbounded.
    legs = [{"kind": "call", "qty": 1, "strike": 100.0, "premium": 5.0}]
    s = strategy_mod.evaluate(legs, spot)
    check("long call available", s["available"] is True)
    check("max loss equals premium paid", close(s["max_loss"], -500.0, 1e-6),
          str(s["max_loss"]))
    check("break-even at strike plus premium",
          s["breakevens"] and abs(s["breakevens"][0] - 105.0) < 0.5,
          str(s["breakevens"]))
    check("net cost is a debit", s["direction"] == "debit")

    # Short put: credit received, loss grows as price falls.
    legs = [{"kind": "put", "qty": -1, "strike": 95.0, "premium": 3.0}]
    s = strategy_mod.evaluate(legs, spot)
    check("short put is a credit", s["direction"] == "credit")
    check("short put max profit is the premium", close(s["max_profit"], 300.0, 1e-6),
          str(s["max_profit"]))

    # Vertical spread: both max profit and max loss are bounded.
    legs = [
        {"kind": "call", "qty": 1, "strike": 100.0, "premium": 5.0},
        {"kind": "call", "qty": -1, "strike": 110.0, "premium": 1.5},
    ]
    s = strategy_mod.evaluate(legs, spot)
    width = (110.0 - 100.0) * 100
    net = (5.0 - 1.5) * 100
    check("spread max profit is width minus debit",
          close(s["max_profit"], width - net, 1.0),
          f"{s['max_profit']} vs {width - net}")
    check("spread max loss is the debit", close(s["max_loss"], -net, 1.0),
          str(s["max_loss"]))

    # Straddle has two break-evens.
    legs = [
        {"kind": "call", "qty": 1, "strike": 100.0, "premium": 6.0},
        {"kind": "put", "qty": 1, "strike": 100.0, "premium": 5.0},
    ]
    s = strategy_mod.evaluate(legs, spot)
    check("straddle has two break-evens", len(s["breakevens"]) == 2,
          str(s["breakevens"]))


def test_strategy_probability():
    print("\nProbability of profit from the implied distribution")
    chain = synthetic_chain(vol=0.35)
    contracts = chain.for_expiry(chain.expiries()[1])
    dist = risk_neutral_distribution(contracts, chain.spot, chain.risk_free)
    check("distribution available", dist.get("available") is True)

    # A far out-of-the-money long call should have a low chance of profit.
    legs = [{"kind": "call", "qty": 1, "strike": 130.0, "premium": 0.5}]
    s = strategy_mod.evaluate(legs, chain.spot, dist["curve"])
    check("OTM call probability of profit is low",
          0 < s["probability_of_profit_pct"] < 25,
          str(s["probability_of_profit_pct"]))

    # A deep in-the-money call should have a high chance of profit.
    legs = [{"kind": "call", "qty": 1, "strike": 70.0, "premium": 30.5}]
    s2 = strategy_mod.evaluate(legs, chain.spot, dist["curve"])
    check("ITM call probability of profit is higher",
          s2["probability_of_profit_pct"] > s["probability_of_profit_pct"],
          f"{s2['probability_of_profit_pct']} vs {s['probability_of_profit_pct']}")
    check("probabilities stay within 0-100",
          0 <= s2["probability_of_profit_pct"] <= 100)

    # A fairly priced option should have expected value near zero.
    T = contracts[0].dte / 365.0
    fair = bs_price(chain.spot, 100.0, T, chain.risk_free, 0.0, 0.35, "call")
    legs = [{"kind": "call", "qty": 1, "strike": 100.0, "premium": fair}]
    s3 = strategy_mod.evaluate(legs, chain.spot, dist["curve"])
    check("fairly priced option has expected value near zero",
          abs(s3["expected_value"]) < 60,
          f"EV {s3['expected_value']} on a ${fair * 100:.0f} option")


def test_presets():
    print("\nStrategy presets build from a chain")
    chain = synthetic_chain()
    expiry = chain.expiries()[0]
    rows = [c.as_dict() for c in chain.contracts]
    for name in strategy_mod.PRESETS:
        built = strategy_mod.build_from_chain(name, rows, chain.spot, expiry)
        check(f"preset {name} builds", built.get("available") is True,
              str(built.get("reason")))
        if built.get("available"):
            s = strategy_mod.evaluate(built["legs"], chain.spot)
            check(f"preset {name} evaluates", s.get("available") is True)


def test_sec_helpers():
    print("\nSEC fundamentals helpers")
    fact = {"units": {"USD": [
        {"end": "2024-03-31", "start": "2024-01-01", "val": 100, "form": "10-Q",
         "fy": 2024, "fp": "Q1", "filed": "2024-04-15"},
        {"end": "2024-06-30", "start": "2024-04-01", "val": 110, "form": "10-Q",
         "fy": 2024, "fp": "Q2", "filed": "2024-07-15"},
        {"end": "2024-09-30", "start": "2024-07-01", "val": 120, "form": "10-Q",
         "fy": 2024, "fp": "Q3", "filed": "2024-10-15"},
        {"end": "2024-12-31", "start": "2024-10-01", "val": 130, "form": "10-K",
         "fy": 2024, "fp": "FY", "filed": "2025-02-15"},
        {"end": "2025-03-31", "start": "2025-01-01", "val": 150, "form": "10-Q",
         "fy": 2025, "fp": "Q1", "filed": "2025-04-15"},
        # A restatement of Q1 2024 filed later - the newer one must win.
        {"end": "2024-03-31", "start": "2024-01-01", "val": 105, "form": "10-Q",
         "fy": 2024, "fp": "Q1", "filed": "2025-04-16"},
    ]}}
    series = sec._series_from_fact(fact, want_quarterly=True)
    check("quarterly series extracted", len(series) == 5, str(len(series)))
    check("restatement supersedes original",
          series[0]["value"] == 105, str(series[0]["value"]))
    check("series sorted by period",
          [r["period_end"] for r in series] == sorted(r["period_end"] for r in series))

    grown = sec._add_growth(series, quarterly=True)
    # Q1 2025 (150) vs Q1 2024 (105) = +42.86%
    check("year-over-year growth computed correctly",
          close(grown[-1]["yoy_pct"], 42.86, 0.05), str(grown[-1]["yoy_pct"]))
    # 150 vs 130 sequential = +15.38%
    check("sequential growth computed correctly",
          close(grown[-1]["qoq_pct"], 15.38, 0.05), str(grown[-1]["qoq_pct"]))
    check("earliest points have no year-over-year figure",
          grown[0]["yoy_pct"] is None)

    annual = sec._series_from_fact({"units": {"USD": [
        {"end": "2023-12-31", "start": "2023-01-01", "val": 400, "form": "10-K",
         "fy": 2023, "fp": "FY", "filed": "2024-02-01"},
        {"end": "2024-12-31", "start": "2024-01-01", "val": 500, "form": "10-K",
         "fy": 2024, "fp": "FY", "filed": "2025-02-01"},
    ]}}, want_quarterly=False)
    check("annual series separates from quarterly", len(annual) == 2, str(len(annual)))
    ann = sec._add_growth(annual, quarterly=False)
    check("annual growth uses a one-period lag",
          close(ann[-1]["yoy_pct"], 25.0, 0.01), str(ann[-1]["yoy_pct"]))


def test_ratios():
    print("\nDerived ratios")
    metrics = [
        {"key": "Revenues", "label": "Revenue", "unit": "USD", "series": [
            {"period_end": "2024-03-31", "value": 1000},
            {"period_end": "2024-06-30", "value": 1200},
            {"period_end": "2025-03-31", "value": 1500},
        ]},
        {"key": "NetIncomeLoss", "label": "Net income", "unit": "USD", "series": [
            {"period_end": "2024-03-31", "value": 100},
            {"period_end": "2024-06-30", "value": 180},
            {"period_end": "2025-03-31", "value": 300},
        ]},
    ]
    ratios = sec.derive_ratios(metrics)
    net_margin = next((r for r in ratios if r["label"] == "Net margin"), None)
    check("net margin derived", net_margin is not None)
    if net_margin:
        check("net margin is 20% in the latest period",
              close(net_margin["latest"], 20.0, 0.01), str(net_margin["latest"]))
    check("ratios skip pairs with no overlap",
          all(len(r["series"]) >= 2 for r in ratios))


def test_speed_modes():
    print("\nSpeed modes")
    check("three modes defined", len(SPEED_MODES) == 3)
    check("quick is cheapest",
          SPEED_MODES["quick"]["contracts"] < SPEED_MODES["standard"]["contracts"]
          < SPEED_MODES["deep"]["contracts"])
    check("quick fits inside 6 minutes at 5 calls/min",
          SPEED_MODES["quick"]["contracts"] / 5 <= 6,
          f"{SPEED_MODES['quick']['contracts'] / 5:.1f} min")
    check("unknown mode falls back to a valid mode",
          settings.speed("nonsense") in SPEED_MODES.values())
    check("None falls back to the default",
          settings.speed(None) == SPEED_MODES[settings.default_speed])


def test_sources_registry():
    print("\nAPI Stock registry")
    reg = registry()
    check("registry returns sources", len(reg["sources"]) >= 12,
          str(len(reg["sources"])))
    check("every source has a state",
          all(s.get("state") in ("active", "needs_key", "planned")
              for s in reg["sources"]))
    check("every source has a cost tier",
          all(s.get("cost_tier") in ("free", "freemium", "paid")
              for s in reg["sources"]))
    check("every source has a link",
          all(s.get("url", "").startswith("http") for s in reg["sources"]))
    names = [s["name"] for s in reg["sources"]]
    check("no duplicate sources", len(names) == len(set(names)))

    massive = next(s for s in reg["sources"] if s["name"].startswith("Massive"))
    check("Massive is active when a key is set", massive["state"] == "active",
          massive["state"])
    fec_row = next(s for s in reg["sources"] if s["name"] == "OpenFEC")
    check("OpenFEC shows as needing a key when unset",
          fec_row["state"] == "needs_key", fec_row["state"])
    check("counts add up",
          sum(reg["counts"].values()) == len(reg["sources"]))


def test_kalshi_parsing():
    print("\nKalshi parsing")
    from app.providers import kalshi
    raw = {
        "ticker": "KXTEST-1", "event_ticker": "KXTEST",
        "yes_sub_title": "Above 5000", "status": "active",
        "yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6400",
        "last_price_dollars": "0.6200", "volume_fp": "1500.00",
        "open_interest_fp": "800.00", "close_time": "2026-12-31T00:00:00Z",
    }
    m = kalshi._clean_market(raw)
    check("probability is the mid of the book", close(m["probability_pct"], 62.0, 0.01),
          str(m["probability_pct"]))
    check("tradeable market passes the filter", kalshi._tradeable(m) is True)

    empty = kalshi._clean_market({"ticker": "X", "volume_fp": "0.00",
                                  "open_interest_fp": "0.00"})
    check("empty market filtered out", kalshi._tradeable(empty) is False)

    no_book = kalshi._clean_market({"ticker": "Y", "last_price_dollars": "0.4500",
                                    "volume_fp": "10.00"})
    check("falls back to last price when book is empty",
          close(no_book["probability_pct"], 45.0, 0.01), str(no_book["probability_pct"]))


def test_lobbying_parsing():
    print("\nLobbying aggregation")
    from app.providers import lobbying as lob
    check("issue codes are labelled", lob.ISSUE_LABELS["TAX"] == "Taxation")
    check("numeric coercion handles nulls", lob._num(None) == 0.0)
    check("numeric coercion parses strings", lob._num("40000.00") == 40000.0)


def test_routes():
    print("\nRoutes")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/health")
        check("health ok", r.status_code == 200)

        r = c.get("/api/sources")
        check("API Stock endpoint returns sources",
              r.status_code == 200 and len(r.json()["sources"]) > 10)

        r = c.get("/api/strategy/presets")
        check("presets endpoint works",
              r.status_code == 200 and len(r.json()["presets"]) == len(strategy_mod.PRESETS))

        chain = synthetic_chain()
        r = c.post("/api/strategy", json={
            "preset": "call_spread", "expiry": chain.expiries()[0],
            "spot": chain.spot,
            "chain": [x.as_dict() for x in chain.contracts],
        })
        check("strategy endpoint builds a spread",
              r.status_code == 200 and r.json().get("available") is True,
              r.text[:160])

        r = c.post("/api/strategy", json={"spot": 0})
        check("strategy rejects a missing spot price", r.status_code == 400)

        r = c.get("/api/fundamentals/!!!")
        check("bad ticker rejected on fundamentals", r.status_code == 400)

        r = c.get("/")
        check("index serves the tabbed UI",
              r.status_code == 200 and b'data-tab="kalshi"' in r.content)

        r = c.get("/static/app.js")
        check("app.js served", r.status_code == 200)


if __name__ == "__main__":
    test_realized_vol()
    test_surface_and_terms()
    test_decay()
    test_strategy()
    test_strategy_probability()
    test_presets()
    test_sec_helpers()
    test_ratios()
    test_speed_modes()
    test_sources_registry()
    test_kalshi_parsing()
    test_lobbying_parsing()
    test_routes()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All checks passed.")
