"""Configuration, loaded from environment variables.

Nothing secret is hard-coded. Put keys in a .env file (see .env.example)
or set them in your host's environment panel.
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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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


# How many contracts each speed mode is allowed to fetch in end-of-day mode.
# Every contract costs one API call, so this is the single biggest lever on
# how long a cold ticker takes.
SPEED_MODES = {
    "quick":    {"contracts": 26, "expirations": 2, "window": 0.22},
    "standard": {"contracts": 54, "expirations": 4, "window": 0.30},
    "deep":     {"contracts": 96, "expirations": 6, "window": 0.38},
}


@dataclass(frozen=True)
class Settings:
    # --- Massive (options + equities) --------------------------------
    api_key: str = os.environ.get("MASSIVE_API_KEY", "")
    base_url: str = os.environ.get("MASSIVE_BASE_URL", "https://api.massive.com")

    access_password: str = os.environ.get("ACCESS_PASSWORD", "")

    rate_limit_per_min: int = _int("MASSIVE_RATE_LIMIT", 5)
    max_concurrency: int = _int("MASSIVE_MAX_CONCURRENCY", 8)
    request_timeout: float = _float("MASSIVE_TIMEOUT", 25.0)

    # --- Optional extra providers -------------------------------------
    # All of these degrade gracefully when absent.
    fec_api_key: str = os.environ.get("FEC_API_KEY", "")        # api.open.fec.gov
    finnhub_key: str = os.environ.get("FINNHUB_KEY", "")        # analyst estimates
    fmp_key: str = os.environ.get("FMP_KEY", "")                # earnings surprises

    # SEC requires a descriptive User-Agent with contact details.
    sec_user_agent: str = os.environ.get(
        "SEC_USER_AGENT", "options-lens/2.0 (contact: set SEC_USER_AGENT)"
    )

    # --- caching ------------------------------------------------------
    cache_path: str = os.environ.get("CACHE_PATH", str(BASE_DIR / "cache.sqlite"))
    ttl_chain: int = _int("TTL_CHAIN", 600)
    ttl_quote: int = _int("TTL_QUOTE", 300)
    ttl_reference: int = _int("TTL_REFERENCE", 86400)
    ttl_slow: int = _int("TTL_SLOW", 21600)
    ttl_external: int = _int("TTL_EXTERNAL", 3600)

    # --- analysis knobs -----------------------------------------------
    default_speed: str = os.environ.get("DEFAULT_SPEED", "quick")
    strike_window: float = _float("STRIKE_WINDOW", 0.30)
    max_expirations: int = _int("MAX_EXPIRATIONS", 4)
    max_contracts_eod: int = _int("MAX_CONTRACTS_EOD", 54)
    default_risk_free: float = _float("RISK_FREE_RATE", 0.042)

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def speed(self, name: str | None) -> dict:
        """Resolve a speed mode, falling back to the configured default."""
        mode = (name or self.default_speed or "standard").lower()
        return SPEED_MODES.get(mode, SPEED_MODES["standard"])


settings = Settings()
