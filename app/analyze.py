"""Orchestrator: chain in, complete analysis out."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .analytics import flow as flow_mod
from .analytics import pricing, squeeze
from .chain import build_chain, get_avg_daily_volume, get_shares_outstanding
from .config import settings
from .models import Chain


async def analyse_ticker(ticker: str, progress=lambda m, p: None) -> Dict[str, Any]:
    ticker = ticker.strip().upper()

    chain: Chain = await build_chain(ticker, progress)
    if not chain.contracts:
        raise ValueError(
            f"No usable option contracts were returned for {ticker}. "
            "It may not have listed options."
        )

    progress("Computing expected move and probabilities", 76)
    expiries = chain.expiries()
    primary_expiry = _pick_primary_expiry(chain)
    primary = chain.for_expiry(primary_expiry)

    move = pricing.expected_move(primary, chain.spot, chain.risk_free)
    dist = pricing.risk_neutral_distribution(primary, chain.spot, chain.risk_free)
    terms = pricing.term_structure(chain)

    progress("Scanning option flow", 84)
    flow = flow_mod.analyse_flow(chain)
    skew = flow_mod.skew_summary(chain)

    progress("Checking short interest and dealer gamma", 90)
    short = await squeeze.fetch_short_data(ticker)
    shares = await get_shares_outstanding(ticker)
    if not short.get("avg_daily_volume"):
        short["avg_daily_volume"] = await get_avg_daily_volume(ticker)
    gex = squeeze.gamma_exposure(chain)
    score = squeeze.squeeze_score(chain, short, gex, flow, shares)

    progress("Done", 100)

    return {
        "ticker": ticker,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "spot": round(chain.spot, 2),
        "spot_source": chain.spot_source,
        "mode": chain.mode,
        "risk_free_rate": round(chain.risk_free * 100, 3),
        "shares_outstanding": shares,
        "warnings": chain.warnings,
        "expiries": expiries,
        "primary_expiry": primary_expiry,
        "expected_move": move,
        "distribution": dist,
        "term_structure": terms,
        "flow": flow,
        "skew": skew,
        "short": short,
        "gamma": gex,
        "squeeze": score,
        "verdict": _verdict(chain, move, dist, flow, score, gex),
        "chain": [c.as_dict() for c in sorted(
            chain.contracts, key=lambda c: (c.expiry, c.strike, c.kind)
        )],
        "contract_count": len(chain.contracts),
    }


def _pick_primary_expiry(chain: Chain) -> str:
    """The most useful expiry to headline: nearest one with real depth."""
    best = None
    best_score = -1.0
    for expiry in chain.expiries():
        contracts = chain.for_expiry(expiry)
        priced = [c for c in contracts if c.iv and c.price]
        if len(priced) < 4:
            continue
        dte = contracts[0].dte
        # Prefer something a week or more out - zero-day chains are noisy.
        proximity = 1.0 / (1.0 + abs(dte - 30) / 30.0)
        score = len(priced) * 0.3 + proximity * 40
        if score > best_score:
            best_score, best = score, expiry
    return best or chain.expiries()[0]


def _verdict(chain, move, dist, flow, score, gex) -> Dict[str, Any]:
    """A short plain-English read of what the numbers are saying together."""
    lines: List[str] = []

    if move.get("available"):
        lines.append(
            f"Options are pricing a one standard deviation move of "
            f"{move['move_pct']}% by {move['expiry']}, which puts roughly a "
            f"two-in-three chance on {chain.underlying} finishing between "
            f"${move['low']} and ${move['high']}."
        )

    if dist.get("available"):
        skew_val = dist.get("skew", 0)
        median = dist.get("median")
        if median:
            direction = "above" if median > chain.spot else "below"
            drift = abs(median / chain.spot - 1) * 100
            lines.append(
                f"The implied median lands at ${median}, {drift:.1f}% {direction} "
                f"the current ${chain.spot:.2f}, with a "
                f"{dist['prob_above_spot']}% chance of finishing above today's price."
            )
        if skew_val < -0.4:
            lines.append(
                "The distribution is left-skewed: the market is paying up for "
                "downside protection and prices a fatter tail to the downside."
            )
        elif skew_val > 0.4:
            lines.append(
                "The distribution is right-skewed, which is unusual for equities "
                "and means upside tail outcomes are being bid."
            )

    if flow.get("available") and flow.get("tilt_label"):
        lines.append(flow["tilt_label"])

    lines.append(f"Squeeze read: {score['label']} ({score['score']}/100). {score['summary']}")

    if gex.get("available"):
        lines.append(gex["interpretation"])

    return {
        "headline": f"{chain.underlying} at ${chain.spot:.2f}",
        "lines": lines,
        "caveat": (
            "These are risk-neutral probabilities derived from option prices. They "
            "describe what the market charges for each outcome, not a forecast. "
            "They systematically overstate tail risk because they include a risk "
            "premium. Nothing here is a recommendation to trade."
        ),
    }
