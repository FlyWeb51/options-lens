"""Unusual option activity and directional positioning.

Nothing here identifies who traded or whether they bought or sold - that
information is not in the data. What it does identify is where activity is
abnormal relative to what is already open, which is the honest version of
what "unusual options activity" screens claim to do.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import Chain, Contract


def _is_otm(c: Contract, spot: float) -> bool:
    return (c.kind == "call" and c.strike > spot) or (
        c.kind == "put" and c.strike < spot
    )


def analyse_flow(chain: Chain, top_n: int = 20) -> Dict[str, Any]:
    spot = chain.spot
    contracts = [c for c in chain.contracts if c.volume]

    if not contracts:
        return {
            "available": False,
            "reason": "no traded volume in the fetched contracts",
        }

    call_vol = sum(c.volume for c in contracts if c.kind == "call")
    put_vol = sum(c.volume for c in contracts if c.kind == "put")
    call_oi = sum(c.open_interest or 0 for c in chain.contracts if c.kind == "call")
    put_oi = sum(c.open_interest or 0 for c in chain.contracts if c.kind == "put")

    call_premium = sum(c.notional or 0 for c in contracts if c.kind == "call")
    put_premium = sum(c.notional or 0 for c in contracts if c.kind == "put")

    otm_call_vol = sum(
        c.volume for c in contracts if c.kind == "call" and _is_otm(c, spot)
    )

    ranked = _rank_unusual(contracts, spot)

    total_premium = call_premium + put_premium
    tilt_pct = (
        round((call_premium - put_premium) / total_premium * 100, 1)
        if total_premium > 0
        else None
    )

    return {
        "available": True,
        "call_volume": int(call_vol),
        "put_volume": int(put_vol),
        "call_put_volume_ratio": round(call_vol / put_vol, 2) if put_vol else None,
        "call_open_interest": int(call_oi) or None,
        "put_open_interest": int(put_oi) or None,
        "call_put_oi_ratio": round(call_oi / put_oi, 2) if put_oi else None,
        "call_premium": round(call_premium, 0),
        "put_premium": round(put_premium, 0),
        "premium_tilt_pct": tilt_pct,
        "otm_call_volume_share": round(otm_call_vol / call_vol * 100, 1)
        if call_vol
        else None,
        "tilt_label": _tilt_label(tilt_pct),
        "unusual": ranked[:top_n],
        "has_open_interest": chain.has_open_interest,
    }


def _tilt_label(tilt: Optional[float]) -> str:
    if tilt is None:
        return "No premium data."
    if tilt > 50:
        return "Strongly call-weighted premium."
    if tilt > 15:
        return "Call-weighted premium."
    if tilt < -50:
        return "Strongly put-weighted premium."
    if tilt < -15:
        return "Put-weighted premium."
    return "Roughly balanced between calls and puts."


def _rank_unusual(contracts: List[Contract], spot: float) -> List[Dict[str, Any]]:
    """Score each contract on how out of the ordinary its activity is."""
    scored: List[Dict[str, Any]] = []

    volumes = sorted((c.volume for c in contracts if c.volume), reverse=True)
    if not volumes:
        return []
    vol_p90 = volumes[max(int(len(volumes) * 0.1) - 1, 0)]

    for c in contracts:
        if not c.volume or c.volume < 10:
            continue

        score = 0.0
        reasons: List[str] = []

        # Volume relative to what is already open is the cleanest signal
        # that today's activity is new positioning rather than churn.
        vol_oi = None
        if c.open_interest and c.open_interest > 0:
            vol_oi = c.volume / c.open_interest
            if vol_oi >= 5:
                score += 45
                reasons.append(f"volume is {vol_oi:.1f}x open interest")
            elif vol_oi >= 2:
                score += 30
                reasons.append(f"volume is {vol_oi:.1f}x open interest")
            elif vol_oi >= 1:
                score += 15
                reasons.append("volume exceeds open interest")

        # Absolute size
        if vol_p90 and c.volume >= vol_p90:
            score += 15
            reasons.append("among the highest volume strikes in the chain")

        # Premium committed
        notional = c.notional or 0
        if notional >= 5_000_000:
            score += 25
            reasons.append(f"${notional / 1e6:.1f}m of premium traded")
        elif notional >= 1_000_000:
            score += 15
            reasons.append(f"${notional / 1e6:.2f}m of premium traded")
        elif notional >= 250_000:
            score += 7

        # Short-dated and far out of the money is the lottery-ticket profile
        moneyness = abs(c.strike - spot) / spot
        if _is_otm(c, spot) and moneyness > 0.10 and c.dte <= 21:
            score += 18
            reasons.append(
                f"short-dated and {moneyness * 100:.0f}% out of the money"
            )
        elif _is_otm(c, spot) and moneyness > 0.20:
            score += 8
            reasons.append(f"{moneyness * 100:.0f}% out of the money")

        if score < 20:
            continue

        scored.append(
            {
                "ticker": c.ticker,
                "kind": c.kind,
                "strike": c.strike,
                "expiry": c.expiry,
                "dte": round(c.dte, 0),
                "volume": int(c.volume),
                "open_interest": int(c.open_interest) if c.open_interest else None,
                "vol_oi_ratio": round(vol_oi, 2) if vol_oi else None,
                "price": round(c.price, 3) if c.price else None,
                "notional": round(notional, 0),
                "iv": round(c.iv * 100, 1) if c.iv else None,
                "delta": round(c.delta, 3) if c.delta is not None else None,
                "moneyness_pct": round((c.strike / spot - 1) * 100, 1),
                "score": round(min(score, 100), 1),
                "reasons": reasons,
            }
        )

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored


def skew_summary(chain: Chain) -> Dict[str, Any]:
    """25-delta risk reversal: what calls cost relative to equivalent puts."""
    out: List[Dict[str, Any]] = []
    for expiry in chain.expiries():
        contracts = [c for c in chain.for_expiry(expiry) if c.iv and c.delta]
        calls = [c for c in contracts if c.kind == "call"]
        puts = [c for c in contracts if c.kind == "put"]
        if not calls or not puts:
            continue

        call_25 = min(calls, key=lambda c: abs(abs(c.delta) - 0.25))
        put_25 = min(puts, key=lambda c: abs(abs(c.delta) - 0.25))
        if abs(abs(call_25.delta) - 0.25) > 0.15 or abs(abs(put_25.delta) - 0.25) > 0.15:
            continue

        rr = (call_25.iv - put_25.iv) * 100
        out.append(
            {
                "expiry": expiry,
                "dte": round(contracts[0].dte, 0),
                "call_25d_iv": round(call_25.iv * 100, 2),
                "put_25d_iv": round(put_25.iv * 100, 2),
                "risk_reversal": round(rr, 2),
                "reading": (
                    "Calls bid over puts - unusual, and a hallmark of squeeze "
                    "or momentum positioning."
                    if rr > 0
                    else "Puts bid over calls - the normal state for equities."
                ),
            }
        )
    return {"available": bool(out), "by_expiry": out}
