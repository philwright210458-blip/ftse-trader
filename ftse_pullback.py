"""Trend-pullback strategy (ideas register 17): a separate signal source for the backtest.

Rules, written so a computer can check them without judgement, from the
moving-average method in the second research video:

1. Daily trend: buys only when yesterday's close-based 50-day average is above
   the price and rising compared with 10 trading days earlier (sells: below and
   falling). Uses only closes before the trading day.
2. Intraday trend (15-minute candles): the 20-period EMA is rising over the
   last 4 candles (one hour) and the 9-period EMA is above it (sells mirror).
3. Pullback: in the last 3 candles the price dipped to the 20-period EMA (a
   candle low within 0.1% of it, or below it).
4. Turn: the latest candle closed back above the 9-period EMA and above its own
   open (sells mirror), and not more than 1% above the 20-period EMA (not
   overstretched).
5. Stop just below the lowest low of those 3 candles (0.1% buffer), at least
   as far as the scanner's minimum risk; target 2x the stop distance.
6. One trade per share per day, signals between 09:00 and 15:30 only.

Each signal becomes available 2 minutes after its candle closes; the backtest
then fills it at the next candle's open, exactly like every other idea.
"""

from __future__ import annotations

import pandas as pd

import ftse_trade_scanner as sc

EMA_FAST, EMA_SLOW, SLOPE_BARS = 9, 20, 4
PULLBACK_BARS = 3
TOUCH_PCT, EXTENSION_PCT, STOP_BUFFER_PCT = 0.001, 0.01, 0.001
REWARD_RISK = 2.0
FIRST_SIGNAL, LAST_SIGNAL = "09:00", "15:30"      # candle close times allowed
AVAILABLE_DELAY = pd.Timedelta(minutes=2)


def daily_trend(daily_closes: pd.Series, day, length: int = 50, lookback: int = 10) -> int:
    """+1 rising trend, -1 falling trend, 0 neither/unknown, judged from closes before `day`."""
    index = pd.DatetimeIndex(daily_closes.index)
    dates = index.tz_localize(None).normalize() if index.tz is not None else index.normalize()
    prior = daily_closes[dates < pd.Timestamp(pd.Timestamp(day).date())].dropna()
    if len(prior) < length + lookback:
        return 0
    now, then, last = prior.tail(length).mean(), prior.iloc[:-lookback].tail(length).mean(), prior.iloc[-1]
    if last > now and now > then:
        return 1
    if last < now and now < then:
        return -1
    return 0


def signals(company, data: pd.DataFrame, daily_closes: pd.Series | None, interval_min: int = 15,
            days: set[str] | None = None) -> list[dict]:
    """All trend-pullback signals for one share, at most one per day, as backtest setup rows."""
    if data is None or len(data) < EMA_SLOW + SLOPE_BARS + PULLBACK_BARS or daily_closes is None:
        return []
    frame = data[["open", "high", "low", "close"]].astype(float).copy()
    frame["fast"] = frame["close"].ewm(span=EMA_FAST, adjust=False).mean()
    frame["slow"] = frame["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    cfg = sc.INDEX_RULES.get(company.index, sc.INDEX_RULES["FTSE 100"])
    rows, used_days, trend_cache = [], set(), {}
    step = pd.Timedelta(minutes=interval_min)
    for position in range(EMA_SLOW + SLOPE_BARS, len(frame)):
        stamp = frame.index[position]
        closes_at = stamp + step
        clock = closes_at.strftime("%H:%M")
        day = str(stamp.date())
        if clock < FIRST_SIGNAL or clock > LAST_SIGNAL or day in used_days or (days is not None and day not in days):
            continue
        if day not in trend_cache:
            trend_cache[day] = daily_trend(daily_closes, stamp)
        trend = trend_cache[day]
        if trend == 0:
            continue
        bar, window = frame.iloc[position], frame.iloc[position - PULLBACK_BARS + 1: position + 1]
        slow_then = frame["slow"].iloc[position - SLOPE_BARS]
        if trend > 0:
            ok = (bar["slow"] > slow_then and bar["fast"] > bar["slow"]
                  and (window["low"] <= window["slow"] * (1 + TOUCH_PCT)).any()
                  and bar["close"] > bar["fast"] and bar["close"] > bar["open"]
                  and bar["close"] <= bar["slow"] * (1 + EXTENSION_PCT))
            stop = window["low"].min() * (1 - STOP_BUFFER_PCT)
        else:
            ok = (bar["slow"] < slow_then and bar["fast"] < bar["slow"]
                  and (window["high"] >= window["slow"] * (1 - TOUCH_PCT)).any()
                  and bar["close"] < bar["fast"] and bar["close"] < bar["open"]
                  and bar["close"] >= bar["slow"] * (1 - EXTENSION_PCT))
            stop = window["high"].max() * (1 + STOP_BUFFER_PCT)
        if not ok:
            continue
        entry = float(bar["close"])
        typical = sc.typical_true_range(frame.iloc[: position + 1])
        min_risk = max(cfg.min_risk_pct * entry, cfg.min_risk_atr * typical)
        risk = max(abs(entry - stop), min_risk)
        direction = "Long" if trend > 0 else "Short"
        stop = entry - risk if trend > 0 else entry + risk
        target = entry + REWARD_RISK * risk if trend > 0 else entry - REWARD_RISK * risk
        slope_pct = abs(bar["slow"] / slow_then - 1) * 100
        used_days.add(day)
        rows.append({
            "day": day, "as_of": clock, "data_time": stamp.isoformat(),
            "run_time": (closes_at + AVAILABLE_DELAY).strftime("%Y-%m-%d %H:%M:%S"), "mode": "pullback",
            "company": company.company, "ticker": company.yahoo_ticker, "index": company.index,
            "setup": "Trend pullback", "direction": direction, "score": round(float(slope_pct) * 100, 2),
            "entry": round(entry, 4), "stop": round(float(stop), 4), "target": round(float(target), 4),
            "risk_pct": round(float(risk / entry * 100), 4), "touches": 0, "conflict": False})
    return rows
