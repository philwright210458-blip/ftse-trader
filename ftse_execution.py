"""Shared, as-of-time paper execution. No downloads, reporting or scanner imports.

Missing observations never prove a fill or exit. Only completed bars are used;
a daily range can bridge an intraday gap only after that session has closed.
"""

import math
from dataclasses import dataclass

import pandas as pd
from ftse_sessions import UK_TZ, is_session, session_open_close, sessions_from

SIMULATION_VERSION = "availability-session-v3"


def interval_minutes(interval: str) -> int | None:
    if not interval:
        return None
    unit = interval[-1].lower()
    try:
        number = int(interval[:-1])
    except ValueError:
        return None
    return number if unit == "m" else number * 60 if unit == "h" else None


def completed_candles(df: pd.DataFrame, interval: str, now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Return completed bars in market time, including single-bar downloads.

    A naive provider index is explicitly interpreted as London market time.
    Historical bars are retained; forming/future bars are never observations.
    """
    result = df.copy()
    result.index = pd.DatetimeIndex(result.index)
    result.index = (result.index.tz_localize(UK_TZ) if result.index.tz is None
                    else result.index.tz_convert(UK_TZ))
    result = result[~result.index.duplicated(keep="last")].sort_index()
    minutes = interval_minutes(interval)
    if minutes is None:
        return result
    now = pd.Timestamp.now(tz=UK_TZ) if now is None else pd.Timestamp(now)
    now = now.tz_localize(UK_TZ) if now.tzinfo is None else now.tz_convert(UK_TZ)
    return result[result.index + pd.Timedelta(minutes=minutes) <= now].copy()


def drop_untraded_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Remove zero-volume candles (no trades). Illiquid names print flat
    placeholder bars that otherwise create fake swing highs/lows."""
    if "volume" in df.columns:
        volume = pd.to_numeric(df["volume"], errors="coerce")
        return df[volume > 0].copy()
    return df


@dataclass
class Sim:
    status: str                 # done | skipped | pending | nodata
    outcome: str = ""           # target | stop | timeout | gap | no_candle
    fill: float = float("nan")
    exit_price: float = float("nan")
    exit_time: str = ""
    r_gross: float = float("nan")
    fill_risk_pct: float = float("nan")
    ambiguous: bool = False
    state: str = ""          # waiting | open | missing; terminal states use status
    mark_price: float = float("nan")
    observed_at: str = ""


def market_stamp(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(UK_TZ) if stamp.tzinfo is None else stamp.tz_convert(UK_TZ)


def simulate(side: str, stop: float, target: float, signal_close: pd.Timestamp,
             data: pd.DataFrame, horizon: int, now: pd.Timestamp, interval: str = "15m",
             daily: pd.DataFrame | None = None) -> Sim:
    """Conservative completed-bar simulation from actual signal availability.

    `signal_close` is the later of the signal candle close and scan availability.
    Intrabar outcomes become observable at bar close. Gaps in the path keep the
    outcome unresolved: a later candle cannot establish what happened earlier.
    Exception (22 Oct 2026): with `daily` prices (date-indexed high/low/close), a
    missing candle is skipped when that whole day's range never reached the stop
    or the target, because then nothing can have happened inside the gap.
    """
    available, now = market_stamp(signal_close), market_stamp(now)
    if now < available:
        return Sim("pending", "waiting_for_entry", state="waiting")
    minutes = interval_minutes(interval)
    if minutes is None or minutes <= 0 or horizon < 1 or side not in {"Long", "Short"}:
        raise ValueError("Use a positive intraday interval, horizon and valid direction")
    if not all(math.isfinite(float(v)) and float(v) > 0 for v in (stop, target)):
        return Sim("skipped", "invalid_levels", observed_at=available.isoformat())
    if (side == "Long" and stop >= target) or (side == "Short" and stop <= target):
        return Sim("skipped", "invalid_levels", observed_at=available.isoformat())
    if not is_session(available):
        return Sim("skipped", "outside_session", observed_at=available.isoformat())
    opening, closing = session_open_close(available)
    if available < opening or available >= closing:
        return Sim("skipped", "outside_session", observed_at=available.isoformat())
    step = pd.Timedelta(minutes=minutes)
    # Anchor intervals to this exchange session, not midnight or a UTC offset.
    expected_fill = opening + math.ceil((available - opening) / step) * step
    if expected_fill >= closing:
        return Sim("skipped", "no_candle", observed_at=available.isoformat())
    complete = drop_untraded_bars(completed_candles(data, interval, now))
    after = complete[complete.index >= expected_fill]
    expired = now - available > pd.Timedelta(days=max(10, horizon * 3 + 4))
    missing = Sim("nodata" if expired else "pending", "missing_coverage", state="missing")
    if after.empty:
        if now < min(expected_fill + step, closing):
            return Sim("pending", "waiting_for_entry", state="waiting")
        return missing
    first = after.index[0]
    # Unknown bars immediately before a fill cannot establish whether the setup
    # was still valid, so do not retrospectively fill at a later observed price.
    if first != expected_fill:
        return missing
    entry_bar = after.iloc[0]
    fill, high, low, close = (float(entry_bar[c]) for c in ("open", "high", "low", "close"))
    if (not all(math.isfinite(v) and v > 0 for v in (fill, high, low, close))
            or low > min(fill, close) or high < max(fill, close)):
        return missing
    if (side == "Long" and (fill <= stop or fill >= target)) or (side == "Short" and (fill >= stop or fill <= target)):
        return Sim("skipped", "gap", fill=fill, observed_at=min(first + step, closing).isoformat())
    risk = abs(fill - stop)
    risk_pct = risk / fill * 100

    def r_multiple(price):
        return round(((price - fill) if side == "Long" else (fill - price)) / risk, 6)

    final_close = closing
    last_row, last_end = None, None
    last_slot_missing = False
    for day in sessions_from(available, horizon):
        day_open, day_close = session_open_close(day)
        start = first if day.date() == first.date() else day_open
        for stamp in pd.date_range(start, day_close, freq=step, inclusive="left"):
            bar_end = min(stamp + step, day_close)
            if bar_end > now:
                return Sim("pending", "in_progress", fill=fill, fill_risk_pct=risk_pct, state="open",
                           mark_price=float(last_row["close"]) if last_row is not None else fill,
                           observed_at=last_end.isoformat() if last_end is not None else "")
            if stamp not in complete.index:
                if now < day_close or not day_range_misses(daily, day, side, stop, target):
                    return missing
                last_slot_missing = True
                continue
            last_slot_missing = False
            row = complete.loc[stamp]
            o, h, l, close = (float(row[c]) for c in ("open", "high", "low", "close"))
            if not all(math.isfinite(v) and v > 0 for v in (o, h, l, close)) or l > min(o, close) or h < max(o, close):
                return missing
            hit_stop, hit_target = (l <= stop, h >= target) if side == "Long" else (h >= stop, l <= target)
            ambiguous = hit_stop and hit_target
            if hit_stop:
                price = min(o, stop) if side == "Long" else max(o, stop)
                return Sim("done", "stop", fill, price, bar_end.isoformat(), r_multiple(price), risk_pct, ambiguous)
            if hit_target:
                price = max(o, target) if side == "Long" else min(o, target)
                return Sim("done", "target", fill, price, bar_end.isoformat(), r_multiple(price), risk_pct, False)
            last_row, last_end = row, bar_end
        final_close = day_close
    if now < final_close or last_row is None:
        return missing
    price, ended = float(last_row["close"]), last_end
    if last_slot_missing:               # the session's last candle is missing: use the official daily close
        close = daily_value(daily, final_close, "close")
        if close is None:
            return missing
        price, ended = close, final_close
    return Sim("done", "timeout", fill, price, ended.isoformat(), r_multiple(price), risk_pct, False)


def daily_value(daily: pd.DataFrame | None, day, column: str) -> float | None:
    if daily is None or daily.empty or column not in daily.columns:
        return None
    stamp = pd.Timestamp(day)
    key = (stamp.tz_convert(UK_TZ).tz_localize(None) if stamp.tzinfo else stamp).normalize()
    if key not in daily.index:
        return None
    value = float(daily.at[key, column])
    return value if math.isfinite(value) and value > 0 else None


def day_range_misses(daily: pd.DataFrame | None, day, side: str, stop: float, target: float) -> bool:
    """True when the whole day's high and low stayed strictly between the stop and the target."""
    high, low = daily_value(daily, day, "high"), daily_value(daily, day, "low")
    if high is None or low is None:
        return False
    if side == "Long":
        return low > stop and high < target
    return high < stop and low > target


