"""End-to-end pipeline test with a stubbed HTTP layer.

Exercises both data paths (live snapshot and free-tier end-of-day) plus the
web routes, without touching the network or needing a real API key.

Run with:  python -m tests.test_pipeline
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("MASSIVE_API_KEY", "test-key-not-real")
os.environ["CACHE_PATH"] = "/tmp/options-lens-test-cache.sqlite"

from app.analytics.bs import bs_greeks, bs_price  # noqa: E402
from app.analyze import analyse_ticker  # noqa: E402
from app.massive import Capabilities, client  # noqa: E402

FAILURES = []
SPOT = 42.5
VOL = 0.62
RATE = 0.043


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# Synthetic market
# ---------------------------------------------------------------------------

def _expiries():
    today = date.today()
    return [(today + timedelta(days=d)).isoformat() for d in (9, 23, 44, 72)]


def _strikes():
    return [round(SPOT * (1 + i * 0.04), 1) for i in range(-8, 9)]


def _contract_ticker(expiry: str, kind: str, strike: float) -> str:
    ymd = expiry.replace("-", "")[2:]
    return f"O:TEST{ymd}{kind[0].upper()}{int(strike * 1000):08d}"


def _vol_for(strike: float) -> float:
    """A realistic downside smirk."""
    return VOL + max(0.0, (SPOT - strike) / SPOT) * 0.55


def _snapshot_rows():
    rows = []
    today = date.today()
    for expiry in _expiries():
        dte = (date.fromisoformat(expiry) - today).days
        T = max(dte, 0.5) / 365.0
        for strike in _strikes():
            for kind in ("call", "put"):
                vol = _vol_for(strike)
                price = bs_price(SPOT, strike, T, RATE, 0.0, vol, kind)
                g = bs_greeks(SPOT, strike, T, RATE, 0.0, vol, kind)
                distance = abs(strike - SPOT) / SPOT
                oi = int(4000 * math.exp(-8 * distance)) + 25
                volume = int(oi * (0.4 + distance * 3.0)) + 15
                rows.append(
                    {
                        "details": {
                            "contract_type": kind,
                            "expiration_date": expiry,
                            "strike_price": strike,
                            "ticker": _contract_ticker(expiry, kind, strike),
                            "shares_per_contract": 100,
                        },
                        "last_quote": {
                            "bid": round(max(price - 0.03, 0.01), 2),
                            "ask": round(price + 0.03, 2),
                            "midpoint": round(price, 4),
                        },
                        "last_trade": {"price": round(price, 4)},
                        "day": {"close": round(price, 4), "volume": volume},
                        "greeks": {
                            "delta": g.delta,
                            "gamma": g.gamma,
                            "vega": g.vega,
                            "theta": g.theta,
                        },
                        "implied_volatility": vol,
                        "open_interest": oi,
                    }
                )
    return rows


SNAPSHOT_ROWS = _snapshot_rows()


def _reference_rows():
    return [
        {
            "ticker": r["details"]["ticker"],
            "contract_type": r["details"]["contract_type"],
            "expiration_date": r["details"]["expiration_date"],
            "strike_price": r["details"]["strike_price"],
            "underlying_ticker": "TEST",
            "shares_per_contract": 100,
        }
        for r in SNAPSHOT_ROWS
    ]


BY_TICKER = {r["details"]["ticker"]: r for r in SNAPSHOT_ROWS}


async def fake_get(path, params=None, ttl=600, allow_cache=True):
    params = params or {}

    if path.startswith("/v3/snapshot/options/"):
        return {"status": "OK", "results": SNAPSHOT_ROWS}

    if path == "/v3/reference/options/contracts":
        return {"status": "OK", "results": _reference_rows()}

    if path.startswith("/v2/snapshot/locale/us/markets/stocks/tickers/"):
        return {"ticker": {"lastTrade": {"p": SPOT}, "prevDay": {"c": SPOT, "v": 9_400_000}}}

    if path.startswith("/v2/aggs/ticker/O:"):
        opt = path.split("/v2/aggs/ticker/")[1].split("/prev")[0]
        row = BY_TICKER.get(opt)
        if not row:
            return {"status": "OK", "results": []}
        return {
            "status": "OK",
            "results": [
                {
                    "c": row["day"]["close"],
                    "o": row["day"]["close"],
                    "h": row["day"]["close"],
                    "l": row["day"]["close"],
                    "v": row["day"]["volume"],
                }
            ],
        }

    if "/range/" in path:
        return {"status": "OK", "results": [{"v": 9_400_000} for _ in range(30)]}

    if path.endswith("/prev"):
        return {"status": "OK", "results": [{"c": SPOT, "v": 9_400_000}]}

    if path == "/fed/v1/treasury-yields":
        return {"status": "OK", "results": [{"yield_3_month": RATE * 100}]}

    if path == "/stocks/v1/short-interest":
        return {
            "status": "OK",
            "results": [
                {
                    "ticker": "TEST",
                    "short_interest": 47_500_000,
                    "avg_daily_volume": 9_400_000,
                    "days_to_cover": 5.05,
                    "settlement_date": "2026-07-31",
                },
                {"ticker": "TEST", "short_interest": 41_000_000},
            ],
        }

    if path == "/stocks/v1/short-volume":
        return {
            "status": "OK",
            "results": [{"short_volume_ratio": 58.0 - i * 0.4} for i in range(20)],
        }

    if path.startswith("/v3/reference/tickers/"):
        return {
            "status": "OK",
            "results": {"weighted_shares_outstanding": 240_000_000},
        }

    return {"status": "OK", "results": []}


async def fake_paginate(path, params=None, ttl=600, max_pages=6):
    data = await fake_get(path, params, ttl)
    return data.get("results") or []


# ---------------------------------------------------------------------------

def install_stubs(chain_snapshot: bool):
    client.get = fake_get                     # type: ignore[assignment]
    client.paginate = fake_paginate           # type: ignore[assignment]
    caps = Capabilities(
        checked=True,
        chain_snapshot=chain_snapshot,
        short_interest=True,
        ticker_overview=True,
    )
    if not chain_snapshot:
        caps.notes.append("Test note: running without the chain snapshot endpoint.")
    client.caps = caps

    async def _detect(probe_ticker="AAPL"):
        return caps

    client.detect_capabilities = _detect       # type: ignore[assignment]


def run_mode(label: str, chain_snapshot: bool):
    print(f"\n{label}")
    install_stubs(chain_snapshot)
    result = asyncio.run(analyse_ticker("TEST"))

    check("result produced", isinstance(result, dict))
    check("mode is correct",
          result["mode"] == ("snapshot" if chain_snapshot else "endofday"),
          result["mode"])
    check("spot recovered", abs(result["spot"] - SPOT) < 0.01, str(result["spot"]))
    check("contracts present", result["contract_count"] > 20,
          str(result["contract_count"]))

    move = result["expected_move"]
    check("expected move available", move.get("available") is True,
          str(move.get("reason")))
    if move.get("available"):
        check("expected move is a sane percentage", 1 < move["move_pct"] < 60,
              str(move["move_pct"]))
        check("move range brackets spot", move["low"] < SPOT < move["high"])

    dist = result["distribution"]
    check("distribution available", dist.get("available") is True,
          str(dist.get("reason")))
    if dist.get("available"):
        check("probability above spot in range",
              0 < dist["prob_above_spot"] < 100, str(dist["prob_above_spot"]))
        check("ladder has 13 rows", len(dist["strike_ladder"]) == 13)
        check("ladder monotonic", all(
            dist["strike_ladder"][i]["prob_above"]
            >= dist["strike_ladder"][i + 1]["prob_above"] - 1e-9
            for i in range(len(dist["strike_ladder"]) - 1)
        ))
        # A lognormal is right-skewed in price space by construction, so the
        # meaningful test is that the downside smirk pulls skew well below
        # the flat-volatility baseline for the same at-the-money vol.
        atm = result["expected_move"]["atm_iv"]
        T = dist["dte"] / 365.0
        v = math.exp(atm * atm * T) - 1.0
        baseline_skew = (v + 3.0) * math.sqrt(v)
        check("downside smirk pulls skew below the flat-vol baseline",
              dist["skew"] < baseline_skew - 0.15,
              f"skew {dist['skew']} vs flat baseline {baseline_skew:.3f}")
        smile = dist["iv_smile"]
        check("smirk preserved: low strikes carry higher volatility",
              smile[0]["iv"] > smile[-1]["iv"],
              f"{smile[0]['iv']} vs {smile[-1]['iv']}")

    sq = result["squeeze"]
    check("squeeze score in range", 0 <= sq["score"] <= 100, str(sq["score"]))
    check("squeeze has components", len(sq["components"]) == 5)
    check("component points never exceed their maximum",
          all(c["points"] <= c["max"] + 1e-9 for c in sq["components"]))
    # 47.5m short of 240m shares = 19.8%, days to cover 5.05
    si_comp = next(c for c in sq["components"] if c["name"] == "Short interest")
    check("short interest percentage computed",
          si_comp["value"] is not None and abs(si_comp["value"] - 19.79) < 0.1,
          str(si_comp["value"]))
    dtc = next(c for c in sq["components"] if c["name"] == "Days to cover")
    check("days to cover scored", dtc["points"] == 12, str(dtc["points"]))

    gex = result["gamma"]
    check("gamma exposure computed", gex.get("available") is True,
          str(gex.get("reason")))
    if gex.get("available"):
        check("gamma profile has strikes", len(gex["profile"]) > 5)
        check("volume proxy flag matches mode",
              gex["using_volume_proxy"] == (not chain_snapshot))

    flow = result["flow"]
    check("flow available", flow.get("available") is True, str(flow.get("reason")))
    if flow.get("available"):
        check("call/put ratio computed", flow["call_put_volume_ratio"] is not None)

    check("term structure populated", len(result["term_structure"]) >= 2)
    check("verdict lines produced", len(result["verdict"]["lines"]) >= 3)
    check("chain serialised", len(result["chain"]) == result["contract_count"])

    # Everything must be JSON serialisable for the API response
    import json
    try:
        json.dumps(result)
        check("result is JSON serialisable", True)
    except (TypeError, ValueError) as exc:
        check("result is JSON serialisable", False, str(exc))

    return result


def test_routes():
    print("\nWeb routes")
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        r = c.get("/api/health")
        check("health returns 200", r.status_code == 200, str(r.status_code))
        check("health reports key configured", r.json()["key_configured"] is True)

        r = c.get("/")
        check("index page served", r.status_code == 200 and b"Options Lens" in r.content)

        r = c.post("/api/analyze", json={"ticker": "!!bad!!"})
        check("bad ticker rejected", r.status_code == 400, str(r.status_code))

        r = c.get("/static/app.js")
        check("static assets served", r.status_code == 200)


if __name__ == "__main__":
    snap = run_mode("Live snapshot mode (paid options plan)", True)
    eod = run_mode("End-of-day mode (free options plan)", False)

    print("\nCross-mode consistency")
    check("both modes agree on spot", snap["spot"] == eod["spot"])
    if snap["expected_move"]["available"] and eod["expected_move"]["available"]:
        diff = abs(snap["expected_move"]["move_pct"] - eod["expected_move"]["move_pct"])
        check("expected move agrees within 2 points across modes", diff < 2.0,
              f"difference {diff:.2f}")
    check("end-of-day mode reports no open interest",
          all(c["open_interest"] is None for c in eod["chain"]))
    check("end-of-day mode computes its own volatility",
          all(c["iv_source"] in ("computed", "none") for c in eod["chain"]))

    test_routes()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} CHECK(S) FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("All checks passed.")
