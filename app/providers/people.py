"""Who runs the company, and what they are doing with their own shares.

Officer names and titles come from SEC Form 4 filings via Massive, which is
available on every Massive plan including the free one. Form 4 is filed
within two business days of any insider transaction, so it is both a
leadership roster and a live record of insider buying and selling.

LinkedIn deliberately provides search links rather than scraped profiles:
scraping LinkedIn violates their terms, and a search link gets you to the
right person in one click anyway.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ..config import settings
from ..massive import MassiveError, client
from .base import ok, unavailable

# Form 4 transaction codes worth distinguishing. P and S are the ones that
# carry signal: those are open-market decisions with the insider's own money.
CODE_MEANING = {
    "P": ("Open-market purchase", "buy"),
    "S": ("Open-market sale", "sell"),
    "A": ("Grant or award", "grant"),
    "M": ("Option exercise", "exercise"),
    "F": ("Shares withheld for tax", "tax"),
    "G": ("Gift", "gift"),
    "D": ("Disposition to issuer", "other"),
    "C": ("Conversion", "other"),
    "X": ("Option exercise (in the money)", "exercise"),
}


def _linkedin(name: str, company: str = "") -> str:
    query = name if not company else f"{name} {company}"
    return ("https://www.linkedin.com/search/results/people/?keywords="
            + query.replace(" ", "%20").replace("&", "%26"))


def _pretty(name: str) -> str:
    """Form 4 names arrive as 'Cook Timothy D' - flip to natural order."""
    name = (name or "").strip()
    if not name:
        return name
    if "," in name:
        last, _, first = name.partition(",")
        return f"{first.strip().title()} {last.strip().title()}".strip()
    parts = name.split()
    if len(parts) >= 2 and parts[0].isupper() and len(parts[0]) > 1:
        return " ".join(p.title() for p in parts[1:] + [parts[0]])
    return " ".join(p.title() for p in parts)


async def insiders(ticker: str, company_name: str = "", days: int = 365) -> Dict[str, Any]:
    since = (date.today() - timedelta(days=days)).isoformat()

    try:
        data = await client.get(
            "/stocks/filings/vX/form-4",
            {"tickers": ticker, "filing_date.gte": since,
             "limit": 300, "sort": "filing_date.desc"},
            ttl=settings.ttl_slow,
        )
    except MassiveError as exc:
        return unavailable(f"Insider filings unavailable: {exc}")

    rows = data.get("results") or []
    if not rows:
        return unavailable(
            f"No Form 4 insider filings for {ticker} in the last {days} days."
        )

    transactions: List[Dict[str, Any]] = []
    roster: Dict[str, Dict[str, Any]] = {}
    net_buy = 0.0
    net_sell = 0.0
    buy_count = 0
    sell_count = 0

    for row in rows:
        raw_name = row.get("owner_name") or ""
        name = _pretty(raw_name)
        if not name:
            continue

        code = row.get("transaction_code") or ""
        label, bucket = CODE_MEANING.get(code, (f"Code {code}", "other"))
        shares = row.get("transaction_shares") or 0
        price = row.get("transaction_price_per_share")
        value = row.get("transaction_value")
        if value is None and price and shares:
            value = price * shares
        value = value or 0
        acquired = row.get("transaction_acquired_disposed") == "A"

        person = roster.setdefault(name, {
            "name": name,
            "title": None,
            "is_officer": False,
            "is_director": False,
            "is_ten_percent": False,
            "transactions": 0,
            "bought_value": 0.0,
            "sold_value": 0.0,
            "shares_owned": None,
            "last_activity": None,
            "cik": row.get("owner_cik"),
        })
        person["transactions"] += 1
        person["is_officer"] = person["is_officer"] or bool(row.get("is_officer"))
        person["is_director"] = person["is_director"] or bool(row.get("is_director"))
        person["is_ten_percent"] = (person["is_ten_percent"]
                                    or bool(row.get("is_ten_percent_owner")))
        if row.get("officer_title") and not person["title"]:
            person["title"] = row["officer_title"]
        owned = row.get("shares_owned_following_transaction")
        if owned is not None and person["shares_owned"] is None:
            person["shares_owned"] = owned
        filed = row.get("filing_date")
        if filed and (person["last_activity"] is None or filed > person["last_activity"]):
            person["last_activity"] = filed

        if bucket == "buy":
            person["bought_value"] += value
            net_buy += value
            buy_count += 1
        elif bucket == "sell":
            person["sold_value"] += value
            net_sell += value
            sell_count += 1

        if bucket in ("buy", "sell") or value >= 250_000:
            transactions.append({
                "date": row.get("transaction_date") or filed,
                "filed": filed,
                "name": name,
                "title": row.get("officer_title"),
                "code": code,
                "action": label,
                "bucket": bucket,
                "acquired": acquired,
                "shares": shares,
                "price": price,
                "value": round(value, 2),
                "security": row.get("security_title"),
                "planned_10b5_1": bool(row.get("aff_10b5_one")),
                "url": row.get("filing_url"),
            })

    transactions.sort(key=lambda t: (t.get("date") or ""), reverse=True)

    people = []
    for person in roster.values():
        role_bits = []
        if person["title"]:
            role_bits.append(person["title"])
        elif person["is_officer"]:
            role_bits.append("Officer")
        if person["is_director"]:
            role_bits.append("Director")
        if person["is_ten_percent"]:
            role_bits.append("10%+ holder")
        person["role"] = " · ".join(role_bits) or "Insider"
        person["bought_value"] = round(person["bought_value"], 2)
        person["sold_value"] = round(person["sold_value"], 2)
        person["net_value"] = round(person["bought_value"] - person["sold_value"], 2)
        person["linkedin_search"] = _linkedin(person["name"], company_name)
        person["sec_url"] = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={person['cik']}&type=4&dateb=&owner=include&count=40"
            if person.get("cik") else None
        )
        people.append(person)

    # Officers and directors first, then by how active they've been.
    people.sort(key=lambda p: (not p["is_officer"], not p["is_director"],
                               -p["transactions"]))

    net = net_buy - net_sell
    if buy_count == 0 and sell_count > 0:
        verdict = ("Only selling in this window. Common and often scheduled "
                   "rather than a view on the stock.")
    elif net > 0 and buy_count >= 2:
        verdict = ("Net insider buying. Open-market purchases by insiders are "
                   "the one insider signal with any documented predictive value.")
    elif net < 0:
        verdict = "Net insider selling by value."
    else:
        verdict = "Roughly balanced insider activity."

    planned = sum(1 for t in transactions if t.get("planned_10b5_1"))

    return ok({
        "ticker": ticker,
        "window_days": days,
        "people": people[:60],
        "transactions": transactions[:80],
        "summary": {
            "bought_value": round(net_buy, 2),
            "sold_value": round(net_sell, 2),
            "net_value": round(net, 2),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "insiders_tracked": len(people),
            "planned_10b5_1": planned,
            "verdict": verdict,
        },
        "source": "SEC Form 4 filings via Massive (available on all plans)",
        "caveat": "Grants, option exercises and tax withholding are excluded from "
                  "the buy/sell totals because they are compensation mechanics, not "
                  "decisions to take a position. Sales flagged 10b5-1 were scheduled "
                  "in advance and carry little signal.",
    })


async def leadership(ticker: str, overview: Optional[Dict[str, Any]],
                     insider_data: Dict[str, Any]) -> Dict[str, Any]:
    """A best-effort leadership roster with links out."""
    company = ""
    homepage = None
    if overview:
        company = overview.get("name") or ""
        homepage = overview.get("homepage_url")

    officers = []
    if insider_data.get("available"):
        officers = [p for p in insider_data.get("people", [])
                    if p.get("is_officer") or p.get("is_director")]

    if not officers:
        return unavailable(
            "No officers or directors identified. Leadership here is derived from "
            "SEC Form 4 filers, so a company with no recent insider filings will "
            "show nobody.",
            company=company,
            linkedin_company=("https://www.linkedin.com/search/results/companies/"
                              "?keywords=" + company.replace(" ", "%20"))
            if company else None,
        )

    return ok({
        "company": company,
        "homepage": homepage,
        "linkedin_company": ("https://www.linkedin.com/search/results/companies/"
                             "?keywords=" + company.replace(" ", "%20"))
        if company else None,
        "officers": officers,
        "source": "Derived from SEC Form 4 filer roles",
        "caveat": "This lists insiders who have filed a Form 4, which covers "
                  "Section 16 officers and directors. It is not a full org chart, "
                  "and someone who has not transacted recently will be missing.",
    })
