"""Auditable, paper-only FTSE opening-watch research.

No broker integration or order execution is provided by this package.
"""
from .models import ResearchConfig, Candle, Signal
from .store import ResearchStore
from .engine import ResearchEngine

__all__ = ["ResearchConfig", "Candle", "Signal", "ResearchStore", "ResearchEngine"]
