"""Live scanner settings from config/scanner.json (from 30 Oct 2026). Every setting is OFF by default, so the
scanner behaves exactly as before until Phil agrees to switch one on.

  ignore_partial_candles, data_delay_minutes
      Yahoo's London prices arrive about 15 minutes late. A check of 283 live scans (30 Oct) found 31% acted on
      a part-formed candle. When on, a scan only uses candles that ended at least data_delay_minutes before it.
  chase_limit_pct, chase_from
      "Stop chasing": at the fill, skip a trade whose entry price is more than chase_limit_pct % beyond the
      9 EMA in the trade's direction (above it for a buy, below it for a sell). Applies to signals on or after
      chase_from (YYYY-MM-DD). null = off. Backtest lead: fills more than 0.5% beyond averaged -34p v -6.5p.
  no_breakouts_before
      "HH:MM": the live trader takes no Breakout (reclaim) ideas from scans that run before this time (the
      opening gap). Range ideas are unaffected. null = off.

A missing or broken file means every setting is off.
"""

from __future__ import annotations

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).parent / "config" / "scanner.json"
DEFAULTS = {"ignore_partial_candles": False, "data_delay_minutes": 15, "chase_limit_pct": None, "chase_from": None,
            "no_breakouts_before": None, "max_stop_pct": None, "min_target_vs_stop": None, "fill_rules_from": None}


def load(path: Path = SETTINGS_PATH) -> dict:
    settings = dict(DEFAULTS)
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        if Path(path).exists():
            print(f"Scanner settings {path} could not be read ({error}); every setting is off.")
        return settings
    for key in DEFAULTS:
        if key in raw:
            settings[key] = raw[key]
    return settings


def candle_delay_minutes(settings: dict) -> int:
    """Minutes to hold back when deciding which candles are complete (0 = off)."""
    if not settings.get("ignore_partial_candles"):
        return 0
    try:
        return max(0, int(settings.get("data_delay_minutes") or 0))
    except (TypeError, ValueError):
        return 0


def chase_kwargs(settings: dict) -> dict:
    """Keyword arguments for ftse_execution.simulate: empty when the chase check is off."""
    limit, start = settings.get("chase_limit_pct"), settings.get("chase_from")
    try:
        limit = float(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit = None
    if limit is None or limit <= 0 or not start:
        return {}
    return {"chase_limit_pct": limit, "chase_from": str(start)}


def _number(value) -> float | None:
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value > 0 else None


def fill_kwargs(settings: dict) -> dict:
    """Keyword arguments for ftse_execution.simulate: the chase check plus the fill rules (empty when all off)."""
    kwargs = chase_kwargs(settings)
    start = settings.get("fill_rules_from")
    stop, ratio = _number(settings.get("max_stop_pct")), _number(settings.get("min_target_vs_stop"))
    if start and (stop or ratio):
        kwargs.update({"max_stop_pct": stop, "min_target_vs_stop": ratio, "fill_rules_from": str(start)})
    return kwargs


def describe(settings: dict) -> str:
    on = []
    if candle_delay_minutes(settings):
        on.append(f"ignore part-formed candles ({candle_delay_minutes(settings)} min)")
    if chase_kwargs(settings):
        on.append(f"skip fills more than {settings['chase_limit_pct']}% past the 9 EMA (from {settings['chase_from']})")
    fill = fill_kwargs(settings)
    if fill.get("max_stop_pct"):
        on.append(f"skip stops more than {fill['max_stop_pct']}% away at the fill (from {fill['fill_rules_from']})")
    if fill.get("min_target_vs_stop"):
        on.append(f"skip when the target left is less than {fill['min_target_vs_stop']}x the stop (from {fill['fill_rules_from']})")
    if settings.get("no_breakouts_before"):
        on.append(f"no breakout trades before {settings['no_breakouts_before']}")
    return "Scanner settings on: " + "; ".join(on) if on else "Scanner settings: all off (as before)."
