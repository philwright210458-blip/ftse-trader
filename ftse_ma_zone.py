"""MA-zone pullback (ideas register 22): buy dips into the zone between the 9 EMA and the 20 SMA.

From Phil's research note of 21 Oct 2026 (diagram: in a clear uptrend price
"trades off the 9 EMA", "trades off the 20 SMA", or falls in between; the zone
between the two lines is the buy area; the 20 SMA should slope at about 45
degrees, not too flat and not too steep).

Rules, set once before testing (every number is in candles, so the same rules
work on 15-minute and daily candles):

1. Trend: the 9 EMA is above the 20 SMA, and both are higher than they were
   SLOPE_BARS candles ago.
2. Dip into the zone: the candle's low reaches the 9 EMA or below, but stays
   no more than 0.25 ATR below the 20 SMA (a small poke through is allowed).
3. Bounce: the candle closes up (close above open) and in the top half of its
   range.
4. Entry at the next candle's open. Stop 0.25 ATR below the lower of the
   candle's low and the 20 SMA, at least 0.5 ATR away. Target 2x the stop
   distance. Buys only (a normal share account cannot sell short).

The "45 degrees" cannot be measured as an angle: it depends on how the chart is
stretched. Instead the slope is measured as how far the 20 SMA rose per candle,
in units of the share's typical candle range (ATR = median true range of the
last 14 candles). The signals are then split into three equal-sized groups by
slope, flat / medium / steep, using only the slopes themselves (not results),
and each group is scored separately. "About 45 degrees" is the medium group.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

EMA_FAST, SMA_SLOW, SLOPE_BARS, ATR_BARS = 9, 20, 8, 14
POKE_ATR, STOP_BUFFER_ATR, MIN_RISK_ATR = 0.25, 0.25, 0.5
REWARD_RISK = 2.0
CLOSE_LOCATION = 0.5


def indicators(data: pd.DataFrame) -> pd.DataFrame:
    frame = data[["open", "high", "low", "close"]].astype(float).copy()
    frame["ema9"] = frame["close"].ewm(span=EMA_FAST, adjust=False).mean()
    frame["sma20"] = frame["close"].rolling(SMA_SLOW, min_periods=SMA_SLOW).mean()
    previous = frame["close"].shift(1)
    true_range = pd.concat([frame["high"] - frame["low"], (frame["high"] - previous).abs(),
                            (frame["low"] - previous).abs()], axis=1).max(axis=1)
    frame["atr"] = true_range.rolling(ATR_BARS, min_periods=ATR_BARS).median()
    frame["slope"] = (frame["sma20"] - frame["sma20"].shift(SLOPE_BARS)) / (SLOPE_BARS * frame["atr"])
    frame["ema_up"] = frame["ema9"] > frame["ema9"].shift(SLOPE_BARS)
    return frame


def signal_mask(frame: pd.DataFrame) -> pd.Series:
    span = (frame["high"] - frame["low"]).replace(0, np.nan)
    location = (frame["close"] - frame["low"]) / span
    return ((frame["ema9"] > frame["sma20"]) & (frame["slope"] > 0) & frame["ema_up"]
            & (frame["low"] <= frame["ema9"]) & (frame["low"] >= frame["sma20"] - POKE_ATR * frame["atr"])
            & (frame["close"] > frame["open"]) & (location >= CLOSE_LOCATION) & (frame["atr"] > 0))


def trend_mask(frame: pd.DataFrame) -> pd.Series:
    """Rule 1 only (the uptrend), for the 'any day in an uptrend' yardstick."""
    return (frame["ema9"] > frame["sma20"]) & (frame["slope"] > 0) & frame["ema_up"] & (frame["atr"] > 0)


def levels(frame: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    """Signal candles with entry reference, stop and target (entry itself is the next candle's open)."""
    rows = frame[mask].copy()
    stop = np.minimum(rows["low"], rows["sma20"]) - STOP_BUFFER_ATR * rows["atr"]
    stop = np.minimum(stop, rows["close"] - MIN_RISK_ATR * rows["atr"])
    rows["stop"] = stop
    rows["target"] = rows["close"] + REWARD_RISK * (rows["close"] - stop)
    return rows


def slope_groups(slopes: pd.Series) -> pd.Series:
    """Flat / medium / steep: three equal-sized groups by slope (cut points from the slopes only)."""
    low, high = slopes.quantile([1 / 3, 2 / 3])
    return pd.Series(np.where(slopes <= low, "Flat", np.where(slopes <= high, "Medium (about 45 degrees)", "Steep")),
                     index=slopes.index)
