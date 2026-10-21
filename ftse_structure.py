"""Daily structure pullback (ideas register 24), from the Market Structure report (21 Oct 2026).

Rules, set once before testing (daily candles; ATR = median true range of the
last 14 days):

1. Swing points: a swing high is a day whose high is above the 3 days before
   it and not below the 3 days after it; swing lows likewise. A swing is only known 3 days later, and is
   only used from then on.
2. Impulse: a swing low L followed by a swing high H where
   * H - L is at least 3 ATR (a sharp move);
   * it is an uptrend: H is above the previous swing high (break of structure)
     and L is above the previous swing low (a higher low);
   * the candles from L to H look impulsive: at least 60% close up, their
     average body is at least 1.5x the average body of the 10 days before L,
     and on average they close in the top 40% of their range.
3. Pullback: after H, the lowest low so far, P, must retrace 38.2% to 61.8% of
   the impulse (Fibonacci). Deeper than 61.8%, a new high above H, or more than
   30 days without an entry cancels the setup.
4. Breaking entry: once the pullback has reached 38.2%, a strong up day that
   closes above the highs of the previous 3 days (body at least 0.75 ATR,
   closing in the top 30% of its range). Buy at the next day's open.
5. Stop 0.25 ATR below the whole pullback (P). Target: measured move, the
   impulse's height added to the pullback low (P + H - L). Anything still open
   after 60 trading days is sold at the close. Buys only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

PIVOT = 3
ATR_BARS = 14
MIN_IMPULSE_ATR = 3.0
MIN_GREEN_SHARE, MIN_BODY_RATIO, MIN_CLOSE_LOCATION = 0.6, 1.5, 0.6
FIB_LOW, FIB_HIGH = 0.382, 0.618
MAX_WAIT = 30
BREAK_BARS, TRIGGER_BODY_ATR, TRIGGER_CLOSE_LOCATION = 3, 0.75, 0.7
STOP_BUFFER_ATR = 0.25
MAX_HOLD = 60


def atr(frame: pd.DataFrame) -> np.ndarray:
    previous = frame["close"].shift(1)
    true_range = pd.concat([frame["high"] - frame["low"], (frame["high"] - previous).abs(),
                            (frame["low"] - previous).abs()], axis=1).max(axis=1)
    return true_range.rolling(ATR_BARS, min_periods=ATR_BARS).median().to_numpy()


def pivots(values: np.ndarray, high: bool) -> np.ndarray:
    """True where the value is the extreme of PIVOT bars either side (known PIVOT bars later).

    Strictly beyond the bars before it and at least level with the bars after it, so a double
    top counts once (at its first day)."""
    n = len(values)
    flags = np.zeros(n, dtype=bool)
    for i in range(PIVOT, n - PIVOT):
        left, right = values[i - PIVOT:i], values[i + 1:i + PIVOT + 1]
        if high:
            flags[i] = values[i] > left.max() and values[i] >= right.max()
        else:
            flags[i] = values[i] < left.min() and values[i] <= right.min()
    return flags


def impulse_ok(frame: pd.DataFrame, a: int, b: int, atr_b: float) -> bool:
    o, h, l, c = (frame[k].to_numpy() for k in ("open", "high", "low", "close"))
    span = slice(a + 1, b + 1)
    if b - a < 1 or a < 10 or not np.isfinite(atr_b) or atr_b <= 0:
        return False
    bodies = np.abs(c[span] - o[span])
    before = np.abs(c[a - 10:a] - o[a - 10:a]).mean()
    ranges = np.where(h[span] - l[span] > 0, h[span] - l[span], np.nan)
    location = np.nanmean((c[span] - l[span]) / ranges)
    return ((c[span] > o[span]).mean() >= MIN_GREEN_SHARE and before > 0 and bodies.mean() >= MIN_BODY_RATIO * before
            and location >= MIN_CLOSE_LOCATION)


def signals(frame: pd.DataFrame) -> list[dict]:
    """Every entry signal for one share (signal day index, levels), using only data up to that day."""
    o, h, l, c = (frame[k].to_numpy() for k in ("open", "high", "low", "close"))
    ranges = atr(frame)
    swing_high, swing_low = pivots(h, True), pivots(l, False)
    found, setup = [], None
    known_highs, known_lows = [], []
    for t in range(len(frame)):
        confirmed = t - PIVOT                                   # a pivot at this index is known from today
        if confirmed >= 0:
            if swing_low[confirmed]:
                known_lows.append(confirmed)
            if swing_high[confirmed]:
                known_highs.append(confirmed)
                b = confirmed
                lows_before = [i for i in known_lows if i < b]
                highs_before = [i for i in known_highs if i < b]
                if lows_before and len(highs_before) >= 1 and len(lows_before) >= 2:
                    a = lows_before[-1]
                    previous_high, previous_low = h[highs_before[-1]], l[lows_before[-2]]
                    size = h[b] - l[a]
                    if (size >= MIN_IMPULSE_ATR * ranges[b] and h[b] > previous_high and l[a] > previous_low
                            and impulse_ok(frame, a, b, ranges[b])):
                        setup = {"a": a, "b": b, "L": l[a], "H": h[b], "P": l[b + 1:t + 1].min() if t > b else h[b]}
        if setup is None or t <= setup["b"]:
            continue
        setup["P"] = min(setup["P"], l[t])
        height = setup["H"] - setup["L"]
        depth = (setup["H"] - setup["P"]) / height
        if depth > FIB_HIGH or h[t] > setup["H"] or t - setup["b"] > MAX_WAIT:
            setup = None
            continue
        if depth < FIB_LOW or t < BREAK_BARS or not np.isfinite(ranges[t]):
            continue
        span = h[t] - l[t]
        strong = (c[t] > o[t] and c[t] - o[t] >= TRIGGER_BODY_ATR * ranges[t] and span > 0
                  and (c[t] - l[t]) / span >= TRIGGER_CLOSE_LOCATION and c[t] > h[t - BREAK_BARS:t].max())
        if strong:
            found.append({"t": t, "L": setup["L"], "H": setup["H"], "P": setup["P"], "depth": depth,
                          "stop": setup["P"] - STOP_BUFFER_ATR * ranges[t],
                          "target": setup["P"] + height, "atr": ranges[t]})
            setup = None
    return found
