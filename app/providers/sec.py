"""Company fundamentals from SEC EDGAR.

Massive's financials endpoints need a paid expansion, but the SEC publishes
the same underlying XBRL data for free at data.sec.gov with no API key.
That is what this module uses, so the fundamentals explorer works on the
free tier.

The one requirement SEC imposes is a descriptive User-Agent with contact
details. Set SEC_USER_AGENT in your environment.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import settings
from .base import ProviderError, fetch_json, ok, unavailable

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# The XBRL tags worth charting, in the order people usually want them.
# Several concepts have more than one valid tag depending on how the company
# files, so each metric lists its candidates in preference order.
METRICS: List[Dict[str, Any]] = [
    {"key": "Revenues", "label": "Revenue", "unit": "USD", "tags": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues", "SalesRevenueNet"]},
    {"key": "GrossProfit", "label": "Gross profit", "unit": "USD",
     "tags": ["GrossProfit"]},
    {"key": "OperatingIncomeLoss", "label": "Operating income", "unit": "USD",
     "tags": ["OperatingIncomeLoss"]},
    {"key": "NetIncomeLoss", "label": "Net income", "unit": "USD",
     "tags": ["NetIncomeLoss", "ProfitLoss"]},
    {"key": "EPS", "label": "Diluted EPS", "unit": "USD/share",
     "tags": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]},
    {"key": "ResearchAndDevelopmentExpense", "label": "R&D expense", "unit": "USD",
     "tags": ["ResearchAndDevelopmentExpense"]},
    {"key": "OperatingExpenses", "label": "Operating expenses", "unit": "USD",
     "tags": ["OperatingExpenses", "CostsAndExpenses"]},
    {"key": "Assets", "label": "Total assets", "unit": "USD", "tags": ["Assets"]},
    {"key": "Liabilities", "label": "Total liabilities", "unit": "USD",
     "tags": ["Liabilities"]},
    {"key": "StockholdersEquity", "label": "Shareholder equity", "unit": "USD",
     "tags": ["StockholdersEquity",
              "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]},
    {"key": "CashAndCashEquivalentsAtCarryingValue", "label": "Cash & equivalents",
     "unit": "USD", "tags": ["CashAndCashEquivalentsAtCarryingValue"]},
    {"key": "NetCashProvidedByUsedInOperatingActivities",
     "label": "Operating cash flow", "unit": "USD",
     "tags": ["NetCashProvidedByUsedInOperatingActivities"]},
    {"key": "PaymentsToAcquirePropertyPlantAndEquipment", "label": "CapEx",
     "unit": "USD", "tags": ["PaymentsToAcquirePropertyPlantAndEquipment"]},
    {"key": "PaymentsForRepurchaseOfCommonStock", "label": "Buybacks",
     "unit": "USD", "tags": ["PaymentsForRepurchaseOfCommonStock"]},
    {"key": "LongTermDebtNoncurrent", "label": "Long-term debt", "unit": "USD",
     "tags": ["LongTermDebtNoncurrent", "LongTermDebt"]},
]

_HEADERS = None


def _headers() -> Dict[str, str]:
    global _HEADERS
    if _HEADERS is None:
        _HEADERS = {
            "User-Agent": settings.sec_user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Host": "data.sec.gov",
        }
    return dict(_HEADERS)


async def resolve_cik(ticker: str, hint: Optional[str] = None) -> Optional[str]:
    """Ticker to zero-padded 10-digit CIK."""
    if hint:
        digits = "".join(ch for ch in str(hint) if ch.isdigit())
        if digits:
            return digits.zfill(10)

    data = await fetch_json(
        TICKER_MAP_URL,
        headers={"User-Agent": settings.sec_user_agent, "Host": "www.sec.gov"},
        ttl=7 * 86400,
        cache_key="sec:tickermap",
    )
    target = ticker.strip().upper()
    rows = data.values() if isinstance(data, dict) else data
    for row in rows:
        if str(row.get("ticker", "")).upper() == target:
            return str(row.get("cik_str", "")).zfill(10)
    return None


def _series_from_fact(fact: Dict[str, Any], want_quarterly: bool) -> List[Dict[str, Any]]:
    """Flatten one XBRL fact into a clean, deduplicated time series."""
    out: Dict[str, Dict[str, Any]] = {}
    for unit_rows in (fact.get("units") or {}).values():
        for row in unit_rows:
            end = row.get("end")
            val = row.get("val")
            form = row.get("form", "")
            fp = row.get("fp", "")
            fy = row.get("fy")
            if end is None or val is None:
                continue
            if not form.startswith("10-"):
                continue

            start = row.get("start")
            is_quarterly = False
            if start:
                # Roughly 90 days means a quarter; roughly 365 means a year.
                try:
                    from datetime import date
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                    is_quarterly = days <= 120
                except ValueError:
                    is_quarterly = fp not in ("FY",)
            else:
                # Instantaneous (balance sheet) values have no start date.
                is_quarterly = True

            if want_quarterly and start and not is_quarterly:
                continue
            if not want_quarterly and start and is_quarterly:
                continue

            # Later filings restate earlier periods; keep the newest.
            prior = out.get(end)
            if prior is None or str(row.get("filed", "")) >= prior["_filed"]:
                out[end] = {
                    "period_end": end,
                    "value": val,
                    "fiscal_year": fy,
                    "fiscal_period": fp,
                    "form": form,
                    "_filed": str(row.get("filed", "")),
                }

    series = sorted(out.values(), key=lambda r: r["period_end"])
    for row in series:
        row.pop("_filed", None)
    return series


def _add_growth(series: List[Dict[str, Any]], quarterly: bool) -> List[Dict[str, Any]]:
    """Attach year-over-year and sequential growth to each point."""
    lag = 4 if quarterly else 1
    for i, row in enumerate(series):
        row["yoy_pct"] = None
        row["qoq_pct"] = None
        if i >= lag:
            prior = series[i - lag]["value"]
            if prior not in (None, 0):
                row["yoy_pct"] = round((row["value"] - prior) / abs(prior) * 100, 2)
        if quarterly and i >= 1:
            prior = series[i - 1]["value"]
            if prior not in (None, 0):
                row["qoq_pct"] = round((row["value"] - prior) / abs(prior) * 100, 2)
    return series


async def fundamentals(
    ticker: str, cik_hint: Optional[str] = None, quarterly: bool = True
) -> Dict[str, Any]:
    """Every supported metric as a time series with growth rates attached."""
    try:
        cik = await resolve_cik(ticker, cik_hint)
    except ProviderError as exc:
        return unavailable(str(exc))

    if not cik:
        return unavailable(f"No SEC CIK found for {ticker}. Foreign issuers and "
                           "funds often do not file XBRL with the SEC.")

    try:
        facts = await fetch_json(
            FACTS_URL.format(cik=cik),
            headers=_headers(),
            ttl=86400,
            cache_key=f"sec:facts:{cik}",
        )
    except ProviderError as exc:
        return unavailable(str(exc))

    us_gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    dei = (facts.get("facts") or {}).get("dei") or {}

    metrics: List[Dict[str, Any]] = []
    for spec in METRICS:
        fact = None
        used_tag = None
        for tag in spec["tags"]:
            if tag in us_gaap:
                fact = us_gaap[tag]
                used_tag = tag
                break
        if fact is None:
            continue

        series = _series_from_fact(fact, want_quarterly=quarterly)
        if len(series) < 2:
            continue
        series = _add_growth(series[-24:], quarterly)

        latest = series[-1]
        metrics.append({
            "key": spec["key"],
            "label": spec["label"],
            "unit": spec["unit"],
            "tag": used_tag,
            "series": series,
            "latest": latest["value"],
            "latest_period": latest["period_end"],
            "yoy_pct": latest.get("yoy_pct"),
        })

    if not metrics:
        return unavailable(
            f"SEC has a CIK for {ticker} but no usable XBRL financial facts."
        )

    shares = None
    if "EntityCommonStockSharesOutstanding" in dei:
        rows = _series_from_fact(dei["EntityCommonStockSharesOutstanding"], True)
        if rows:
            shares = rows[-1]["value"]

    return ok({
        "ticker": ticker,
        "cik": cik,
        "entity": facts.get("entityName"),
        "timeframe": "quarterly" if quarterly else "annual",
        "shares_outstanding": shares,
        "metrics": metrics,
        "source": "SEC EDGAR XBRL company facts (free, no key required)",
        "filings_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-&dateb=&owner=include&count=40",
    })


def derive_ratios(metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Margins and returns computed from the raw metrics we already have."""
    by_key = {m["key"]: {r["period_end"]: r["value"] for r in m["series"]}
              for m in metrics}

    def ratio(num_key: str, den_key: str, label: str, pct: bool = True):
        num, den = by_key.get(num_key), by_key.get(den_key)
        if not num or not den:
            return None
        periods = sorted(set(num) & set(den))
        if len(periods) < 2:
            return None
        series = []
        for p in periods:
            d = den[p]
            if not d:
                continue
            value = num[p] / d * (100 if pct else 1)
            series.append({"period_end": p, "value": round(value, 3)})
        if len(series) < 2:
            return None
        series = _add_growth(series, quarterly=True)
        return {
            "key": f"{num_key}/{den_key}",
            "label": label,
            "unit": "%" if pct else "x",
            "series": series,
            "latest": series[-1]["value"],
            "latest_period": series[-1]["period_end"],
            "yoy_pct": series[-1].get("yoy_pct"),
            "derived": True,
        }

    candidates = [
        ratio("GrossProfit", "Revenues", "Gross margin"),
        ratio("OperatingIncomeLoss", "Revenues", "Operating margin"),
        ratio("NetIncomeLoss", "Revenues", "Net margin"),
        ratio("ResearchAndDevelopmentExpense", "Revenues", "R&D as % of revenue"),
        ratio("NetIncomeLoss", "StockholdersEquity", "Return on equity"),
        ratio("NetIncomeLoss", "Assets", "Return on assets"),
        ratio("Liabilities", "StockholdersEquity", "Debt to equity", pct=False),
    ]
    return [c for c in candidates if c]
