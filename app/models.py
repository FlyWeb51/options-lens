"""Normalised data structures shared across the app.

Both the snapshot path (paid plans) and the end-of-day path (free plan)
produce these, so every analytics module downstream sees one shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional


@dataclass
class Contract:
    ticker: str
    underlying: str
    kind: str            # "call" | "put"
    strike: float
    expiry: str          # YYYY-MM-DD
    dte: float           # calendar days to expiry

    # Market data (any of these may be missing depending on plan)
    bid: Optional[float] = None
    ask: Optional[float] = None
    mid: Optional[float] = None
    last: Optional[float] = None
    close: Optional[float] = None
    volume: Optional[float] = None
    open_interest: Optional[float] = None

    # Derived / supplied
    iv: Optional[float] = None
    delta: Optional[float] = None
    gamma: Optional[float] = None
    vega: Optional[float] = None
    theta: Optional[float] = None
    iv_source: str = "none"      # "api" | "computed" | "none"
    price_source: str = "none"   # "quote" | "trade" | "eod_close"

    @property
    def price(self) -> Optional[float]:
        """Best available fair price for this contract."""
        for candidate in (self.mid, self.last, self.close):
            if candidate is not None and candidate > 0:
                return candidate
        return None

    @property
    def notional(self) -> Optional[float]:
        p = self.price
        if p is None or self.volume is None:
            return None
        return p * self.volume * 100.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "kind": self.kind,
            "strike": self.strike,
            "expiry": self.expiry,
            "dte": round(self.dte, 1),
            "bid": self.bid,
            "ask": self.ask,
            "price": _r(self.price, 4),
            "volume": self.volume,
            "open_interest": self.open_interest,
            "iv": _r(self.iv, 4),
            "delta": _r(self.delta, 4),
            "gamma": _r(self.gamma, 6),
            "vega": _r(self.vega, 4),
            "theta": _r(self.theta, 4),
            "iv_source": self.iv_source,
            "price_source": self.price_source,
        }


@dataclass
class Chain:
    underlying: str
    spot: float
    as_of: str
    mode: str                       # "snapshot" | "endofday"
    contracts: List[Contract] = field(default_factory=list)
    risk_free: float = 0.042
    warnings: List[str] = field(default_factory=list)
    spot_source: str = "unknown"
    speed: str = "standard"

    def expiries(self) -> List[str]:
        return sorted({c.expiry for c in self.contracts})

    def for_expiry(self, expiry: str) -> List[Contract]:
        return [c for c in self.contracts if c.expiry == expiry]

    @property
    def has_open_interest(self) -> bool:
        return any(c.open_interest for c in self.contracts)

    @property
    def has_volume(self) -> bool:
        return any(c.volume for c in self.contracts)


def _r(value: Optional[float], places: int) -> Optional[float]:
    return None if value is None else round(value, places)


def days_between(start: date, end: date) -> float:
    return float((end - start).days)
