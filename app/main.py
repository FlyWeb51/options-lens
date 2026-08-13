"""FastAPI application.

Analysis runs as a background job because on the free data tier a single
ticker can take several minutes (5 API calls per minute). The browser polls
for progress, and finished results are cached so the second person to look
at a ticker gets it instantly.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from fastapi import Cookie, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .analytics import strategy as strategy_mod
from .analyze import analyse_ticker, get_overview
from .config import SPEED_MODES, BASE_DIR, settings
from .massive import MassiveError, client
from .providers import base as provider_base
from .providers import fec, kalshi, lobbying, people, sec
from .sources import registry

app = FastAPI(title="Options Lens", version="2.0.0")

STATIC_DIR = BASE_DIR / "static"
CACHE_PREFIX = "analysis:"


# ---------------------------------------------------------------------------
# Optional shared-password gate
# ---------------------------------------------------------------------------

def _token() -> str:
    return hashlib.sha256(
        f"options-lens::{settings.access_password}".encode()
    ).hexdigest()


def _authorised(cookie: Optional[str]) -> bool:
    if not settings.access_password:
        return True
    return bool(cookie) and hmac.compare_digest(cookie, _token())


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    open_paths = ("/login", "/api/login", "/static", "/favicon.ico", "/api/health")
    if not settings.access_password or request.url.path.startswith(open_paths):
        return await call_next(request)
    if _authorised(request.cookies.get("ol_auth")):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Password required."}, status_code=401)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/login")
async def login(payload: Dict[str, str], response: Response):
    supplied = (payload or {}).get("password", "")
    if not settings.access_password or hmac.compare_digest(
        supplied, settings.access_password
    ):
        response.set_cookie(
            "ol_auth", _token(), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax"
        )
        return {"ok": True}
    raise HTTPException(status_code=401, detail="Wrong password.")


@app.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    ticker: str
    speed: str = "quick"
    status: str = "running"       # running | done | error
    progress: float = 0.0
    message: str = "Starting"
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    cache_key: str = ""

    def public(self) -> Dict[str, Any]:
        return {
            "job_id": self.id,
            "ticker": self.ticker,
            "status": self.status,
            "progress": round(self.progress, 1),
            "message": self.message,
            "elapsed": round(time.time() - self.started_at, 1),
            "result": self.result,
            "error": self.error,
        }


JOBS: Dict[str, Job] = {}
ACTIVE_BY_TICKER: Dict[str, str] = {}
_job_lock = asyncio.Lock()


async def _run_job(job: Job) -> None:
    def progress(message: str, pct: float) -> None:
        job.message = message
        job.progress = pct

    try:
        result = await analyse_ticker(job.ticker, progress, job.speed)
        job.result = result
        job.status = "done"
        job.progress = 100.0
        job.message = "Complete"
        ttl = settings.ttl_slow if result.get("mode") == "endofday" else settings.ttl_chain
        await client.cache.set(job.cache_key or (CACHE_PREFIX + job.ticker), result, ttl)
    except MassiveError as exc:
        job.status = "error"
        job.error = str(exc)
    except ValueError as exc:
        job.status = "error"
        job.error = str(exc)
    except Exception as exc:  # pragma: no cover
        job.status = "error"
        job.error = f"Unexpected error: {exc}"
    finally:
        ACTIVE_BY_TICKER.pop(job.cache_key or job.ticker, None)


@app.post("/api/analyze")
async def start_analysis(payload: Dict[str, Any]):
    ticker = str((payload or {}).get("ticker", "")).strip().upper()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Enter a valid ticker symbol.")
    if not settings.has_key:
        raise HTTPException(
            status_code=500,
            detail="No API key configured on the server. Set MASSIVE_API_KEY.",
        )

    force = bool((payload or {}).get("refresh"))
    speed = str((payload or {}).get("speed") or settings.default_speed).lower()
    if speed not in SPEED_MODES:
        speed = settings.default_speed

    cache_key = f"{CACHE_PREFIX}{ticker}:{speed}"

    if not force:
        cached = await client.cache.get(cache_key)
        if cached:
            age = await client.cache.age(cache_key)
            return {
                "status": "done",
                "cached": True,
                "cache_age_seconds": round(age or 0),
                "result": cached,
            }

    async with _job_lock:
        existing = ACTIVE_BY_TICKER.get(cache_key)
        if existing and existing in JOBS:
            return {"status": "running", "job_id": existing, "joined_existing": True}

        job = Job(id=uuid.uuid4().hex[:12], ticker=ticker, speed=speed)
        JOBS[job.id] = job
        ACTIVE_BY_TICKER[cache_key] = job.id
        job.cache_key = cache_key
        asyncio.create_task(_run_job(job))

    _prune_jobs()
    return {"status": "running", "job_id": job.id, "speed": speed}


@app.get("/api/job/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown job.")
    return job.public()


def _prune_jobs(max_age: float = 3600.0) -> None:
    cutoff = time.time() - max_age
    for jid in [j for j, job in JOBS.items() if job.started_at < cutoff]:
        JOBS.pop(jid, None)


# ---------------------------------------------------------------------------
# Info endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"ok": True, "version": app.version, "key_configured": settings.has_key}


@app.get("/api/capabilities")
async def capabilities():
    if not settings.has_key:
        return {"error": "No API key configured."}
    caps = await client.detect_capabilities()
    return {"capabilities": caps.as_dict(), "usage": client.stats(),
            "speed_modes": SPEED_MODES, "default_speed": settings.default_speed}


@app.get("/api/sources")
async def api_sources():
    """API Stock - the data source bookmark."""
    return registry()


# ---------------------------------------------------------------------------
# Company pages. Each is independent so a failure in one never blanks another.
# ---------------------------------------------------------------------------

def _clean_ticker(ticker: str) -> str:
    ticker = (ticker or "").strip().upper()
    if not ticker or not ticker.replace(".", "").replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid ticker symbol.")
    return ticker


@app.get("/api/fundamentals/{ticker}")
async def fundamentals(ticker: str, timeframe: str = "quarterly"):
    ticker = _clean_ticker(ticker)
    overview = await get_overview(ticker)
    data = await sec.fundamentals(
        ticker,
        cik_hint=(overview or {}).get("cik"),
        quarterly=(timeframe != "annual"),
    )
    if data.get("available"):
        data["ratios"] = sec.derive_ratios(data["metrics"])
        data["company"] = (overview or {}).get("name") or data.get("entity")
    return data


@app.get("/api/people/{ticker}")
async def people_page(ticker: str, days: int = 365):
    ticker = _clean_ticker(ticker)
    overview = await get_overview(ticker)
    company = (overview or {}).get("name", "")
    insider_data = await people.insiders(ticker, company, days)
    leadership = await people.leadership(ticker, overview, insider_data)
    return {
        "ticker": ticker,
        "company": company,
        "insiders": insider_data,
        "leadership": leadership,
        "overview": {
            "name": company,
            "description": (overview or {}).get("description"),
            "homepage": (overview or {}).get("homepage_url"),
            "employees": (overview or {}).get("total_employees"),
            "sector": (overview or {}).get("sic_description"),
            "address": (overview or {}).get("address"),
        } if overview else None,
    }


@app.get("/api/politics/{ticker}")
async def politics(ticker: str):
    ticker = _clean_ticker(ticker)
    overview = await get_overview(ticker)
    company = (overview or {}).get("name", ticker)

    lobby = await lobbying.lobbying(company, ticker)
    committees = await fec.committees(company)
    return {"ticker": ticker, "company": company,
            "lobbying": lobby, "committees": committees}


@app.get("/api/politics/{ticker}/committee/{committee_id}")
async def committee_detail(ticker: str, committee_id: str):
    return await fec.disbursements(committee_id)


@app.get("/api/kalshi")
async def kalshi_board():
    return await kalshi.dashboard()


@app.get("/api/kalshi/search")
async def kalshi_search(q: str):
    return await kalshi.search(q)


@app.post("/api/strategy")
async def strategy(payload: Dict[str, Any]):
    """Payoff and market-implied probability of profit for a structure."""
    spot = float(payload.get("spot") or 0)
    if spot <= 0:
        raise HTTPException(status_code=400, detail="A spot price is required.")

    legs = payload.get("legs")
    preset = payload.get("preset")
    density = payload.get("density") or None

    if preset:
        built = strategy_mod.build_from_chain(
            preset, payload.get("chain") or [], spot, payload.get("expiry") or ""
        )
        if not built.get("available"):
            return built
        legs = built["legs"]
        label = built["label"]
    else:
        label = payload.get("label", "Custom structure")

    result = strategy_mod.evaluate(legs or [], spot, density)
    result["label"] = label
    return result


@app.get("/api/strategy/presets")
async def strategy_presets():
    return {"presets": [{"key": k, "label": v["label"]}
                        for k, v in strategy_mod.PRESETS.items()]}


@app.on_event("startup")
async def startup() -> None:
    await client.cache.purge_expired()


@app.on_event("shutdown")
async def shutdown() -> None:
    await client.close()
    await provider_base.close()


# ---------------------------------------------------------------------------
# Static site (mounted last so API routes win)
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
