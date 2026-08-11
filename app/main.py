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

from .analyze import analyse_ticker
from .config import BASE_DIR, settings
from .massive import MassiveError, client

app = FastAPI(title="Options Lens", version="1.0.0")

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
    status: str = "running"       # running | done | error
    progress: float = 0.0
    message: str = "Starting"
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)

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
        result = await analyse_ticker(job.ticker, progress)
        job.result = result
        job.status = "done"
        job.progress = 100.0
        job.message = "Complete"
        ttl = settings.ttl_slow if result.get("mode") == "endofday" else settings.ttl_chain
        await client.cache.set(CACHE_PREFIX + job.ticker, result, ttl)
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
        ACTIVE_BY_TICKER.pop(job.ticker, None)


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

    if not force:
        cached = await client.cache.get(CACHE_PREFIX + ticker)
        if cached:
            age = await client.cache.age(CACHE_PREFIX + ticker)
            return {
                "status": "done",
                "cached": True,
                "cache_age_seconds": round(age or 0),
                "result": cached,
            }

    async with _job_lock:
        existing = ACTIVE_BY_TICKER.get(ticker)
        if existing and existing in JOBS:
            return {"status": "running", "job_id": existing, "joined_existing": True}

        job = Job(id=uuid.uuid4().hex[:12], ticker=ticker)
        JOBS[job.id] = job
        ACTIVE_BY_TICKER[ticker] = job.id
        asyncio.create_task(_run_job(job))

    _prune_jobs()
    return {"status": "running", "job_id": job.id}


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
    return {"capabilities": caps.as_dict(), "usage": client.stats()}


@app.on_event("startup")
async def startup() -> None:
    await client.cache.purge_expired()


@app.on_event("shutdown")
async def shutdown() -> None:
    await client.close()


# ---------------------------------------------------------------------------
# Static site (mounted last so API routes win)
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
