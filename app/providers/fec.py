"""Campaign finance from the Federal Election Commission.

Corporations cannot give to candidates directly, but their employees and
their PAC can. This looks up committees whose name matches the company and
reports where that money went.

The FEC API needs a key. api.data.gov issues them free in about a minute at
https://api.open.fec.gov/developers/. Without one this degrades to a
"not configured" panel rather than failing.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any, Dict, List

from ..config import settings
from .base import ProviderError, fetch_json, ok, unavailable

API = "https://api.open.fec.gov/v1"


def _cycle() -> int:
    """FEC two-year cycles end on even years."""
    year = date.today().year
    return year if year % 2 == 0 else year + 1


async def committees(company: str) -> Dict[str, Any]:
    if not settings.fec_api_key:
        return unavailable(
            "FEC API key not configured. Add FEC_API_KEY to enable campaign "
            "finance. Keys are free at https://api.open.fec.gov/developers/.",
            needs_key=True,
            signup="https://api.open.fec.gov/developers/",
        )

    name = (company or "").strip()
    if not name:
        return unavailable("No company name to search.")

    try:
        data = await fetch_json(
            f"{API}/committees/",
            {"q": name, "api_key": settings.fec_api_key,
             "per_page": 20, "sort": "-last_file_date"},
            ttl=86400,
            cache_key=f"fec:committees:{name.lower()}",
        )
    except ProviderError as exc:
        return unavailable(str(exc))

    results = data.get("results") or []
    if not results:
        return unavailable(f"No FEC committees matched \"{name}\".")

    found = [{
        "committee_id": c.get("committee_id"),
        "name": c.get("name"),
        "designation": c.get("designation_full"),
        "type": c.get("committee_type_full"),
        "party": c.get("party_full"),
        "state": c.get("state"),
        "last_filed": c.get("last_file_date"),
        "url": f"https://www.fec.gov/data/committee/{c.get('committee_id')}/",
    } for c in results]

    return ok({"company": name, "committees": found,
               "source": "FEC OpenFEC API"})


async def disbursements(committee_id: str, cycle: int | None = None) -> Dict[str, Any]:
    """Where a committee's money went, aggregated by recipient."""
    if not settings.fec_api_key:
        return unavailable("FEC API key not configured.", needs_key=True)

    cycle = cycle or _cycle()
    try:
        data = await fetch_json(
            f"{API}/schedules/schedule_b/",
            {"committee_id": committee_id, "two_year_transaction_period": cycle,
             "api_key": settings.fec_api_key, "per_page": 100,
             "sort": "-disbursement_amount"},
            ttl=86400,
            cache_key=f"fec:disb:{committee_id}:{cycle}",
        )
    except ProviderError as exc:
        return unavailable(str(exc))

    rows = data.get("results") or []
    if not rows:
        return unavailable(f"No disbursements found for {committee_id} in {cycle}.")

    by_recipient: Dict[str, float] = defaultdict(float)
    by_party: Dict[str, float] = defaultdict(float)
    entries: List[Dict[str, Any]] = []

    for row in rows:
        amount = float(row.get("disbursement_amount") or 0)
        recipient = (row.get("recipient_name") or "Unknown").title()
        by_recipient[recipient] += amount

        party = ((row.get("recipient_committee") or {}) or {}).get("party_full")
        if party:
            by_party[party] += amount

        entries.append({
            "date": row.get("disbursement_date", "")[:10],
            "recipient": recipient,
            "amount": amount,
            "description": row.get("disbursement_description"),
            "state": row.get("recipient_state"),
        })

    return ok({
        "committee_id": committee_id,
        "cycle": cycle,
        "total": round(sum(by_recipient.values()), 2),
        "by_recipient": [{"recipient": k, "amount": round(v, 2)}
                         for k, v in sorted(by_recipient.items(),
                                            key=lambda kv: -kv[1])[:25]],
        "by_party": [{"party": k, "amount": round(v, 2)}
                     for k, v in sorted(by_party.items(), key=lambda kv: -kv[1])],
        "entries": entries[:60],
        "source": "FEC Schedule B disbursements",
        "caveat": "A corporate PAC is funded by employee contributions, not "
                  "company treasury money. Disbursements to a candidate committee "
                  "are support; disbursements to vendors are operating costs.",
    })
