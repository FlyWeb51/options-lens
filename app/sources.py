"""API Stock - the bookmark of data sources.

A live registry of every API this app can use: what it costs, what it
unlocks, whether it is currently wired up, and the env var to set to turn
it on. Anything marked "planned" has no code behind it yet and is here so
you can decide what to add next.

Status is computed from the actual environment at request time, so this
page always tells the truth about what is switched on.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

from .config import settings

SOURCES: List[Dict[str, Any]] = [
    # ---------------- wired up ----------------
    {
        "name": "Massive (options + equities)",
        "category": "Market data",
        "cost": "Free tier; Options Starter ~$29/mo",
        "cost_tier": "freemium",
        "env": "MASSIVE_API_KEY",
        "status": "wired",
        "unlocks": "Option chains, contract reference, previous-day bars, short "
                   "interest, short volume, Form 4 insider filings, treasury yields.",
        "upgrade_note": "The paid Options Starter plan unlocks the chain snapshot: "
                        "live greeks, implied volatility and open interest in one "
                        "call. That single upgrade takes a ticker from ~10 minutes "
                        "to a few seconds and makes gamma exposure trustworthy.",
        "url": "https://massive.com/pricing",
        "docs": "https://massive.com/docs",
    },
    {
        "name": "SEC EDGAR XBRL",
        "category": "Fundamentals",
        "cost": "Free, no key",
        "cost_tier": "free",
        "env": "SEC_USER_AGENT",
        "status": "wired",
        "unlocks": "Revenue, margins, EPS, cash flow, balance sheet, buybacks - "
                   "with year-over-year growth. Powers the Fundamentals tab.",
        "upgrade_note": "Only requires a descriptive User-Agent with your contact "
                        "details, which SEC asks for. No key, no rate tier.",
        "url": "https://www.sec.gov/edgar/sec-api-documentation",
        "docs": "https://data.sec.gov/",
    },
    {
        "name": "Senate Lobbying Disclosure (LDA)",
        "category": "Politics",
        "cost": "Free, no key",
        "cost_tier": "free",
        "env": None,
        "status": "wired",
        "unlocks": "Lobbying spend by quarter, issues pushed, firms hired, named "
                   "lobbyists, revolving-door hires, and which chambers and "
                   "agencies were contacted. Powers the Politics tab.",
        "url": "https://lda.senate.gov/api/",
        "docs": "https://lda.senate.gov/api/redoc/v1/",
    },
    {
        "name": "Kalshi",
        "category": "Prediction markets",
        "cost": "Free read access, no key",
        "cost_tier": "free",
        "env": None,
        "status": "wired",
        "unlocks": "Real-money probabilities on Fed decisions, CPI, jobs, GDP, "
                   "recession, index levels and crypto. Powers the Kalshi tab.",
        "url": "https://kalshi.com",
        "docs": "https://trading-api.readme.io/reference/getting-started",
    },
    # ---------------- optional, needs a key ----------------
    {
        "name": "OpenFEC",
        "category": "Politics",
        "cost": "Free with key",
        "cost_tier": "free",
        "env": "FEC_API_KEY",
        "status": "optional",
        "unlocks": "Corporate PAC committees and where their money went, by "
                   "recipient and party. The Politics tab shows a prompt until "
                   "this key is set.",
        "upgrade_note": "Keys are issued instantly at api.data.gov.",
        "url": "https://api.open.fec.gov/developers/",
        "docs": "https://api.open.fec.gov/developers/",
    },
    {
        "name": "Finnhub",
        "category": "Estimates",
        "cost": "Free tier; paid from $49/mo",
        "cost_tier": "freemium",
        "env": "FINNHUB_KEY",
        "status": "planned",
        "unlocks": "Analyst consensus EPS and revenue estimates - the missing "
                   "piece for real beat/miss versus consensus. The free tier "
                   "covers estimates and earnings surprises for US names.",
        "url": "https://finnhub.io/pricing",
        "docs": "https://finnhub.io/docs/api",
    },
    {
        "name": "Financial Modeling Prep",
        "category": "Estimates",
        "cost": "Free tier; paid from $22/mo",
        "cost_tier": "freemium",
        "env": "FMP_KEY",
        "status": "planned",
        "unlocks": "Historical earnings surprises and the analyst estimate "
                   "calendar. An alternative to Finnhub for beat/miss history.",
        "url": "https://site.financialmodelingprep.com/developer/docs/pricing",
        "docs": "https://site.financialmodelingprep.com/developer/docs",
    },
    # ---------------- worth knowing about ----------------
    {
        "name": "Benzinga (via Massive)",
        "category": "Estimates & news",
        "cost": "$99/mo per dataset",
        "cost_tier": "paid",
        "env": None,
        "status": "planned",
        "unlocks": "Analyst ratings, price targets, consensus estimates and "
                   "corporate guidance, delivered through the Massive API you "
                   "already use, so no second integration.",
        "url": "https://massive.com/partners/benzinga",
    },
    {
        "name": "FRED (St. Louis Fed)",
        "category": "Macro",
        "cost": "Free with key",
        "cost_tier": "free",
        "env": "FRED_API_KEY",
        "status": "planned",
        "unlocks": "Every US macro series: yield curve, credit spreads, money "
                   "supply, housing. Good backdrop for the Kalshi macro board.",
        "url": "https://fred.stlouisfed.org/docs/api/api_key.html",
    },
    {
        "name": "Polymarket",
        "category": "Prediction markets",
        "cost": "Free read access",
        "cost_tier": "free",
        "env": None,
        "status": "planned",
        "unlocks": "A second prediction market to cross-check Kalshi. Where the "
                   "two disagree on the same event, one of them is mispriced.",
        "url": "https://docs.polymarket.com/",
    },
    {
        "name": "QuiverQuant",
        "category": "Politics",
        "cost": "Paid, from ~$75/mo",
        "cost_tier": "paid",
        "env": "QUIVER_KEY",
        "status": "planned",
        "unlocks": "Congressional stock trading disclosures, government contract "
                   "awards, and lobbying joined to tickers. The obvious companion "
                   "to the Politics tab.",
        "url": "https://www.quiverquant.com/pricing/",
    },
    {
        "name": "SEC EDGAR full-text search",
        "category": "Filings",
        "cost": "Free, no key",
        "cost_tier": "free",
        "env": None,
        "status": "planned",
        "unlocks": "Search any phrase across all filings. Useful for spotting "
                   "risk-factor language changes between 10-Ks.",
        "url": "https://efts.sec.gov/LATEST/search-index?q=test",
    },
    {
        "name": "Alpha Vantage",
        "category": "Market data",
        "cost": "Free tier (25 req/day); paid from $50/mo",
        "cost_tier": "freemium",
        "env": "ALPHAVANTAGE_KEY",
        "status": "planned",
        "unlocks": "Backup price history and FX. The free tier is too thin to "
                   "carry this app but fine as a fallback.",
        "url": "https://www.alphavantage.co/premium/",
    },
    {
        "name": "Tradier",
        "category": "Market data",
        "cost": "Free sandbox; brokerage account for live",
        "cost_tier": "freemium",
        "env": "TRADIER_TOKEN",
        "status": "planned",
        "unlocks": "A genuinely free live option chain with greeks if you open a "
                   "brokerage account. The cheapest realistic route out of "
                   "end-of-day mode.",
        "url": "https://documentation.tradier.com/brokerage-api",
    },
]


def registry() -> Dict[str, Any]:
    """Current status of every source, computed from the live environment."""
    out = []
    for source in SOURCES:
        env = source.get("env")
        configured = bool(os.environ.get(env)) if env else True

        status = source["status"]
        if status == "optional" and not configured:
            state, state_label = "needs_key", "Needs a key"
        elif status == "wired" and env and not configured:
            state, state_label = "needs_key", "Needs a key"
        elif status == "wired":
            state, state_label = "active", "Active"
        elif status == "planned":
            state, state_label = "planned", "Not wired up yet"
        else:
            state, state_label = "active", "Active"

        out.append({**source, "state": state, "state_label": state_label,
                    "configured": configured})

    counts: Dict[str, int] = {}
    for row in out:
        counts[row["state"]] = counts.get(row["state"], 0) + 1

    return {
        "sources": out,
        "counts": counts,
        "categories": sorted({s["category"] for s in out}),
        "how_to_add": (
            "Set the environment variable on Render (Dashboard > your service > "
            "Environment), then Save. The service restarts in about 30 seconds "
            "and this page will show the source as Active."
        ),
        "massive_mode_note": (
            "The single highest-impact upgrade is Massive Options Starter. "
            "Everything else on this list adds new panels; that one makes the "
            "whole app fast and makes open interest real."
        ),
    }
