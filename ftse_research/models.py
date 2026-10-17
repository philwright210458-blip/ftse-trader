"""Explicit time, price and virtual-risk contracts for research records."""
from dataclasses import dataclass, asdict, fields
from datetime import datetime
from hashlib import sha256
import json
import math

import pandas as pd


def stamp(value=None):
    value = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    if value.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return value.tz_convert("UTC")


def iso(value):
    return stamp(value).isoformat()


def key(*parts):
    return sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def serialise(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class ResearchConfig:
    """Illustrative virtual defaults, explicitly unapproved for real money."""
    capital: float = 1000.0
    position_size: float = 100.0
    max_positions: int = 3
    daily_loss_limit_pct: float = 2.0
    mover_threshold_pct: float = 5.0
    interval_minutes: int = 15
    history_days: int = 60
    max_data_lag_minutes: float = 20.0
    signal_expiry_minutes: float = 45.0
    spread_bps: float = 10.0
    slippage_bps: float = 5.0
    commission_per_side: float = 0.0
    stamp_duty_pct: float = 0.5
    allow_synthetic_shorts: bool = False
    allow_same_session_reentry: bool = False
    minimum_pilot_sessions: int = 60
    minimum_pilot_closed_trades: int = 100
    maximum_pilot_drawdown_pct: float = 10.0
    mode: str = "paper"

    def __post_init__(self):
        positive = ("capital", "position_size", "max_positions", "interval_minutes", "history_days",
                    "max_data_lag_minutes", "signal_expiry_minutes", "minimum_pilot_sessions",
                    "minimum_pilot_closed_trades")
        for field in positive:
            if not math.isfinite(float(getattr(self, field))) or getattr(self, field) <= 0:
                raise ValueError(f"{field} must be positive")
        for field in ("daily_loss_limit_pct", "mover_threshold_pct", "spread_bps",
                      "slippage_bps", "commission_per_side", "stamp_duty_pct", "maximum_pilot_drawdown_pct"):
            if not math.isfinite(float(getattr(self, field))) or getattr(self, field) < 0:
                raise ValueError(f"{field} must be non-negative")
        if self.position_size > self.capital:
            raise ValueError("Virtual position size cannot exceed initial capital")
        for field in ("max_positions", "interval_minutes", "history_days", "minimum_pilot_sessions", "minimum_pilot_closed_trades"):
            if not isinstance(getattr(self, field), int) or isinstance(getattr(self, field), bool):
                raise ValueError(f"{field} must be a whole number")
        for field in ("allow_synthetic_shorts", "allow_same_session_reentry"):
            if type(getattr(self, field)) is not bool:
                raise ValueError(f"{field} must be an explicit boolean")
        if self.daily_loss_limit_pct > 100:
            raise ValueError("Daily loss limit cannot exceed 100%")
        if self.mode not in {"paper", "synthetic-demo"}:
            raise ValueError("Only paper and synthetic-demo modes are supported")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        unknown = set(data) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown research configuration fields: {sorted(unknown)}")
        return cls(**data)


@dataclass(frozen=True)
class Candle:
    ticker: str
    start: str
    end: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    fetched_at: str
    interval: str = "15m"
    quote_unit: str = "GBp"
    adjusted_close: float | None = None
    source: str = "yahoo"

    def to_dict(self):
        data = asdict(self)
        for name in ("start", "end", "fetched_at"):
            data[name] = iso(data[name])
        data["available_at"] = iso(max(stamp(self.end), stamp(self.fetched_at)))
        return data

    def validate(self):
        if not self.ticker.strip():
            raise ValueError("Candle ticker is required")
        for name in ("open", "high", "low", "close", "volume"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"Invalid {name}")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise ValueError("Prices must be positive and volume non-negative")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Invalid OHLC price ordering")
        if stamp(self.start) >= stamp(self.end):
            raise ValueError("Candle end must follow start")
        if self.quote_unit not in {"GBp", "GBP"}:
            raise ValueError("Only explicit sterling GBp or GBP quote units are supported")
        if self.adjusted_close is not None and (not math.isfinite(self.adjusted_close) or self.adjusted_close <= 0):
            raise ValueError("Adjusted close must be positive")


@dataclass(frozen=True)
class Signal:
    ticker: str
    strategy: str
    direction: str
    signal_at: str
    available_at: str
    stop: float
    target: float
    reference_price: float
    quote_unit: str = "GBp"
    score: float = 0.0
    reason: str = "Existing scanner setup"
    zone_low: float | None = None
    zone_high: float | None = None

    def to_dict(self):
        result = asdict(self)
        result["signal_at"] = iso(self.signal_at)
        result["available_at"] = iso(self.available_at)
        result["id"] = key(self.ticker, self.strategy, self.direction, result["signal_at"])
        return result

    def validate(self):
        if self.direction not in {"Long", "Short"}:
            raise ValueError("Direction must be Long or Short")
        if self.quote_unit not in {"GBP", "GBp"}:
            raise ValueError("Explicit sterling quote units required")
        if stamp(self.available_at) < stamp(self.signal_at):
            raise ValueError("Signal cannot be available before its source candle closes")
        if not all(math.isfinite(float(value)) and value > 0 for value in (self.stop, self.target, self.reference_price)):
            raise ValueError("Signal prices must be finite and positive")
        if not (self.stop < self.reference_price < self.target if self.direction == "Long"
                else self.target < self.reference_price < self.stop):
            raise ValueError("Signal stop/target have invalid direction")
