"""Corporate lobbying, from the Senate Lobbying Disclosure Act database.

lda.senate.gov publishes every filing required by the LDA: who was hired,
how much was paid, which issues were pushed, which lobbyists worked it, and
which chambers and agencies were contacted. It is free and needs no key.

This is disclosure data, not inference. Everything shown here was filed
under penalty of perjury by the registrant.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional

from .base import ProviderError, fetch_json, ok, unavailable

API = "https://lda.senate.gov/api/v1"

# LDA issue codes are terse; these are the ones that come up most.
ISSUE_LABELS = {
    "TAX": "Taxation", "CPT": "Copyright/Patent/Trademark", "TEC": "Telecom",
    "TRD": "Trade", "BUD": "Budget/Appropriations", "HCR": "Health",
    "ENV": "Environment", "ENG": "Energy", "FIN": "Financial institutions",
    "BAN": "Banking", "DEF": "Defense", "LBR": "Labor", "IMM": "Immigration",
    "TRA": "Transportation", "EDU": "Education", "SCI": "Science/Technology",
    "CSP": "Consumer protection", "TOR": "Torts", "GOV": "Government issues",
    "MMM": "Medicare/Medicaid", "AGR": "Agriculture", "AVI": "Aviation",
    "PHA": "Pharmacy", "RET": "Retirement", "HOM": "Homeland security",
    "INS": "Insurance", "RES": "Real estate", "UTI": "Utilities",
}


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def _filings(client_name: str, years: List[int], page_size: int = 25) -> List[Dict]:
    out: List[Dict[str, Any]] = []
    for year in years:
        try:
            data = await fetch_json(
                f"{API}/filings/",
                {"client_name": client_name, "filing_year": year,
                 "page_size": page_size},
                ttl=86400,
                cache_key=f"lda:{client_name.lower()}:{year}:{page_size}",
            )
        except ProviderError:
            continue
        out.extend(data.get("results") or [])
    return out


async def lobbying(company_name: str, ticker: str = "", years: int = 3) -> Dict[str, Any]:
    """Lobbying spend, issues, firms, lobbyists and targets for a company."""
    name = (company_name or "").strip()
    if not name:
        return unavailable("No company name available to search lobbying records.")

    # Trim common suffixes: the LDA client names rarely include them.
    for suffix in (" Inc.", " Inc", " Corporation", " Corp.", " Corp",
                   " Company", " Co.", " Holdings", " Group", " plc", " Ltd",
                   " Limited", ", Inc.", " Class A", " Common Stock"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip().rstrip(",")

    this_year = date.today().year
    year_list = [this_year - i for i in range(years)]

    try:
        rows = await _filings(name, year_list)
    except ProviderError as exc:
        return unavailable(str(exc))

    if not rows:
        return unavailable(
            f"No federal lobbying filings found for \"{name}\". Either the "
            "company does not lobby directly, files under a different legal "
            "name, or works only through trade associations."
        )

    total = 0.0
    by_year: Dict[int, float] = defaultdict(float)
    by_issue: Dict[str, float] = defaultdict(float)
    by_registrant: Dict[str, float] = defaultdict(float)
    entities: Dict[str, int] = defaultdict(int)
    lobbyists: Dict[str, Dict[str, Any]] = {}
    filings: List[Dict[str, Any]] = []
    clients: Dict[str, int] = defaultdict(int)

    for row in rows:
        amount = _num(row.get("income")) or _num(row.get("expenses"))
        year = row.get("filing_year") or 0
        registrant = (row.get("registrant") or {}).get("name", "Unknown")
        client = (row.get("client") or {}).get("name", "Unknown")
        clients[client] += 1

        total += amount
        by_year[year] += amount
        by_registrant[registrant] += amount

        issues: List[str] = []
        for activity in row.get("lobbying_activities") or []:
            code = activity.get("general_issue_code") or "?"
            label = ISSUE_LABELS.get(
                code, activity.get("general_issue_code_display") or code)
            issues.append(label)
            # Spend is reported per filing, not per issue, so split evenly
            # across the issues in that filing rather than double counting.
            n = max(len(row.get("lobbying_activities") or []), 1)
            by_issue[label] += amount / n

            for entity in activity.get("government_entities") or []:
                entities[entity.get("name", "Unknown").title()] += 1

            for entry in activity.get("lobbyists") or []:
                person = entry.get("lobbyist") or {}
                full = " ".join(filter(None, [
                    (person.get("first_name") or "").title(),
                    (person.get("last_name") or "").title(),
                ])).strip()
                if not full:
                    continue
                record = lobbyists.setdefault(full, {
                    "name": full, "filings": 0, "firms": set(),
                    "covered_position": None,
                })
                record["filings"] += 1
                record["firms"].add(registrant)
                if entry.get("covered_position"):
                    record["covered_position"] = entry["covered_position"]

        filings.append({
            "period": row.get("filing_type_display") or row.get("filing_type"),
            "year": year,
            "amount": amount,
            "registrant": registrant,
            "client": client,
            "posted": (row.get("dt_posted") or "")[:10],
            "issues": sorted(set(issues)),
            "document_url": row.get("filing_document_url"),
        })

    filings.sort(key=lambda f: (f["year"], f["posted"]), reverse=True)

    people = []
    for record in lobbyists.values():
        people.append({
            "name": record["name"],
            "filings": record["filings"],
            "firms": sorted(record["firms"]),
            "covered_position": record["covered_position"],
            "linkedin_search":
                "https://www.linkedin.com/search/results/people/?keywords="
                + record["name"].replace(" ", "%20"),
        })
    people.sort(key=lambda p: -p["filings"])

    revolving = [p for p in people if p["covered_position"]]

    return ok({
        "company": name,
        "ticker": ticker,
        "matched_clients": sorted(clients, key=lambda c: -clients[c]),
        "total_reported": round(total, 2),
        "filing_count": len(rows),
        "years_covered": sorted(by_year, reverse=True),
        "by_year": [{"year": y, "amount": round(v, 2)}
                    for y, v in sorted(by_year.items(), reverse=True)],
        "by_issue": [{"issue": k, "amount": round(v, 2)}
                     for k, v in sorted(by_issue.items(), key=lambda kv: -kv[1])[:15]],
        "by_registrant": [{"firm": k, "amount": round(v, 2)}
                          for k, v in sorted(by_registrant.items(),
                                             key=lambda kv: -kv[1])[:15]],
        "targets": [{"entity": k, "mentions": v}
                    for k, v in sorted(entities.items(), key=lambda kv: -kv[1])[:20]],
        "lobbyists": people[:40],
        "revolving_door": revolving[:20],
        "filings": filings[:40],
        "source": "US Senate Lobbying Disclosure Act database (lda.senate.gov), "
                  "free and public",
        "caveat": "Amounts are self-reported per filing and cover all activity by "
                  "that firm for that client in the period, not spend per issue. "
                  "Issue-level figures split a filing evenly across its issues, so "
                  "treat them as indicative. Trade-association spending on a "
                  "company's behalf is not captured here.",
    })
