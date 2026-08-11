"""Configuration, loaded from environment variables.

Nothing secret is ever hard-coded here. Put your key in a .env file
(see .env.example) or set it in your host's environment panel.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader so there is no hard dependency on python-dotenv."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # --- credentials -------------------------------------------------
    api_key: str = os.environ.get("MASSIVE_API_KEY", "")
    base_url: str = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com")

    # Optional shared password so you can hand the URL to friends
    # without it being wide open. Leave blank to disable the gate.
    access_password: str = os.environ.get("ACCESS_PASSWORD", "")

    # --- rate limiting ------------------------------------------------
    # Massive's free tier is 5 requests/minute. Paid tiers are unlimited;
    # set MASSIVE_RATE_LIMIT=0 to disable throttling entirely.
    rate_limit_per_min: int = _int("MASSIVE_RATE_LIMIT", 5)
    max_concurrency: int = _int("MASSIVE_MAX_CONCURRENCY", 8)
    request_timeout: float = _float("MASSIVE_TIMEOUT", 25.0)

    # --- caching ------------------------------------------------------
    cache_path: str = os.environ.get("CACHE_PATH", str(BASE_DIR / "cache.sqlite"))
    ttl_chain: int = _int("TTL_CHAIN", 600)          # 10 min
    ttl_quote: int = _int("TTL_QUOTE", 300)          # 5 min
    ttl_reference: int = _int("TTL_REFERENCE", 86400)  # 1 day
    ttl_slow: int = _int("TTL_SLOW", 21600)          # 6 h (short interest, float)

    # --- analysis knobs -----------------------------------------------
    # How far either side of spot to pull strikes, as a fraction of spot.
    strike_window: float = _float("STRIKE_WINDOW", 0.35)
    # How many expirations forward to analyse.
    max_expirations: int = _int("MAX_EXPIRATIONS", 6)
    # Hard ceiling on contracts fetched in the slow end-of-day fallback mode.
    max_contracts_eod: int = _int("MAX_CONTRACTS_EOD", 90)
    # Fallback risk-free rate if the treasury endpoint is unavailable.
    default_risk_free: float = _float("RISK_FREE_RATE", 0.042)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)


settings = Settings()
