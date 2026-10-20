"""Range model version 2: Patrick Nill's "fair value to fair value" method, rebuilt.

Written from the transcript of the original video (see docs: "FTSE Trader: how
the current model works", section 9). His exact zone-drawing rules are secret,
so every number below is our best reconstruction, set once and not tuned.

1. Big move: in the last 8 candles (2 hours) price moved at least 5 typical
   candle ranges in one direction and closed near the end of the move.
2. Fair-value box: after the big move, price goes sideways. The box is the
   highest high and lowest low since the move. It must be no taller than 60%
   of the big move and at least 2 typical candle ranges tall.
3. Ready: at least 16 candles (4 hours) old with 3 touches of its edges (at
   least one on each), price moving away between touches. Then it is frozen.
4. Three trades from a ready box (one of each type per box):
   * Range: price dips to the bottom edge from inside and closes back in the
     lower third: buy, target the top edge. (Sells mirror at the top.)
   * Breakout: a candle closes outside the box, then within 4 candles another
     closes beyond the highest point since the break (his confirmation). Trade
     with the break; stop under the lowest point since the break.
   * Reclaim: a candle closes outside the box, then within 3 candles price
     closes back inside: trade back the other way; stop beyond the excursion.
5. Targets: the next fair-value level (edges of earlier boxes and the start
   points of their big moves, from the last 10 trading days). Range trades aim
   at the far edge of their own box; reclaims aim beyond it. With no level,
   breakouts copy the size of the big move; reclaims add one box height.
   A trade is only taken if the target is at least 3x the stop distance away.
6. Stops use a buffer of max(0.1% of price, half a typical candle range) and
   are never moved. Trades are held up to 3 trading days.
7. New signals only from 08:30 to 15:30 UK time, and none from 13:15 to 14:45
   (US data and the US open, which Patrick says often change the market).

Every signal is available 2 minutes after its candle closes and filled at the
next candle's open by ftse_outcomes.simulate, like every other backtest idea.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import ftse_trade_scanner as sc

IMPULSE_BARS, IMPULSE_MULT, IMPULSE_CLOSE_FRAC = 8, 5.0, 0.75
BOX_MIN_BARS, BOX_MAX_FRAC, BOX_MIN_TR = 16, 0.6, 2.0
TOUCH_TOL, REARM_FRAC, MIN_TOUCHES = 0.15, 0.35, 3
CONFIRM_BARS, RECLAIM_BARS = 4, 3
BOX_MAX_AGE_BARS = 3 * 34
LEVEL_BARS = 10 * 34
MIN_REWARD_RISK = 3.0
TR_WINDOW = 50
STOP_BUFFER_PCT, STOP_BUFFER_TR = 0.001, 0.5
FIRST_SIGNAL, LAST_SIGNAL = "08:30", "15:30"
QUIET_FROM, QUIET_TO = "13:15", "14:45"
AVAILABLE_DELAY = pd.Timedelta(minutes=2)
SETUPS = ("Range", "Breakout", "Reclaim")


def count_touches(highs, lows, closes, top: float, bottom: float) -> tuple[int, int]:
    """Separate visits to the top and bottom edges (price must move away between visits)."""
    height = top - bottom
    tol, rearm = TOUCH_TOL * height, REARM_FRAC * height
    top_n = bottom_n = 0
    top_armed = bottom_armed = True
    for high, low, close in zip(highs, lows, closes):
        if top_armed and high >= top - tol:
            top_n, top_armed = top_n + 1, False
        elif not top_armed and close <= top - rearm:
            top_armed = True
        if bottom_armed and low <= bottom + tol:
            bottom_n, bottom_armed = bottom_n + 1, False
        elif not bottom_armed and close >= bottom + rearm:
            bottom_armed = True
    return top_n, bottom_n


def find_impulse(h, l, c, i: int, med: float):
    """(direction, start price, size) if candles i-7..i make a big one-way move, else None."""
    j = i - IMPULSE_BARS + 1
    if j < 0:
        return None
    window_h, window_l = h[j:i + 1], l[j:i + 1]
    hi, lo = float(window_h.max()), float(window_l.min())
    size = hi - lo
    if size < IMPULSE_MULT * med:
        return None
    at_hi, at_lo = int(window_h.argmax()), int(window_l.argmin())
    if at_lo < at_hi and c[i] >= lo + IMPULSE_CLOSE_FRAC * size:
        return "up", lo, size
    if at_hi < at_lo and c[i] <= hi - IMPULSE_CLOSE_FRAC * size:
        return "down", hi, size
    return None


def nearest_level(levels, i: int, beyond: float, direction: str, extra=()) -> float | None:
    """Closest fair-value level past `beyond` in the trade direction, from the last 10 trading days."""
    prices = [p for p, made in levels if i - made <= LEVEL_BARS] + [p for p in extra if p is not None]
    if direction == "Long":
        above = [p for p in prices if p > beyond]
        return min(above) if above else None
    below = [p for p in prices if p < beyond]
    return max(below) if below else None


def signals(company, data: pd.DataFrame | None, interval_min: int = 15, days: set[str] | None = None,
            cfg: sc.IndexConfig | None = None, delay: pd.Timedelta = AVAILABLE_DELAY) -> list[dict]:
    """All version-2 signals for one share (or the index), as backtest setup rows."""
    if data is None or len(data) < TR_WINDOW + IMPULSE_BARS + BOX_MIN_BARS:
        return []
    frame = data[["open", "high", "low", "close"]].astype(float)
    o, h, l, c = (frame[col].to_numpy() for col in ("open", "high", "low", "close"))
    prev_close = np.concatenate([[np.nan], c[:-1]])
    true_range = np.nanmax(np.vstack([h - l, np.abs(h - prev_close), np.abs(l - prev_close)]), axis=0)
    med_tr = pd.Series(true_range).rolling(TR_WINDOW).median().to_numpy()
    cfg = cfg or sc.INDEX_RULES.get(getattr(company, "index", "FTSE 100"), sc.INDEX_RULES["FTSE 100"])
    step = pd.Timedelta(minutes=interval_min)
    stamps = frame.index
    levels: list[tuple[float, int]] = []
    box = None
    rows: list[dict] = []

    def retire(i: int) -> None:
        nonlocal box
        levels.extend([(box["top"], i), (box["bottom"], i), (box["imp_start"], i)])
        box = None

    def emit(i: int, setup: str, direction: str, entry: float, stop: float, target: float | None) -> None:
        closes_at = stamps[i] + step
        clock, day = closes_at.strftime("%H:%M"), str(stamps[i].date())
        if target is None or clock < FIRST_SIGNAL or clock > LAST_SIGNAL or QUIET_FROM <= clock <= QUIET_TO:
            return
        if days is not None and day not in days:
            return
        min_risk = max(cfg.min_risk_pct * entry, cfg.min_risk_atr * med_tr[i])
        risk = abs(entry - stop)
        if risk < min_risk:
            risk = min_risk
            stop = entry - risk if direction == "Long" else entry + risk
        reward = (target - entry) if direction == "Long" else (entry - target)
        if reward < MIN_REWARD_RISK * risk:
            return
        rows.append({
            "day": day, "as_of": clock, "data_time": stamps[i].isoformat(),
            "run_time": (closes_at + delay).strftime("%Y-%m-%d %H:%M:%S"), "mode": "range_v2",
            "company": company.company, "ticker": company.yahoo_ticker, "index": company.index,
            "setup": setup, "direction": direction, "score": round(reward / risk, 2),
            "entry": round(entry, 4), "stop": round(float(stop), 4), "target": round(float(target), 4),
            "risk_pct": round(risk / entry * 100, 4), "touches": box["touches"] if box else 0, "conflict": False})

    for i in range(TR_WINDOW, len(frame)):
        med = med_tr[i]
        if not np.isfinite(med) or med <= 0:
            continue
        buffer = max(STOP_BUFFER_PCT * c[i], STOP_BUFFER_TR * med)

        if box is not None and box["ready"]:
            top, bottom = box["top"], box["bottom"]
            height = top - bottom
            tol = TOUCH_TOL * height
            state = box["state"]
            if state == "inside":
                if c[i] > top:
                    box.update(state="up", broke=i)
                elif c[i] < bottom:
                    box.update(state="down", broke=i)
                else:
                    came_from_inside = bottom + tol < c[i - 1] < top - tol
                    if ("Range", "Long") not in box["used"] and came_from_inside and l[i] <= bottom + tol \
                            and bottom < c[i] <= bottom + height / 3:
                        box["used"].add(("Range", "Long"))
                        emit(i, "Range", "Long", c[i], min(l[i], bottom) - buffer, top)
                    elif ("Range", "Short") not in box["used"] and came_from_inside and h[i] >= top - tol \
                            and top - height / 3 <= c[i] < top:
                        box["used"].add(("Range", "Short"))
                        emit(i, "Range", "Short", c[i], max(h[i], top) + buffer, bottom)
                    if i - box["ready_at"] > BOX_MAX_AGE_BARS:
                        retire(i)
                continue
            broke = box["broke"]
            if state == "up":
                if c[i] < top:                                     # failed break: reclaim short
                    if ("Reclaim", "Short") not in box["used"]:
                        box["used"].add(("Reclaim", "Short"))
                        target = nearest_level(levels, i, bottom, "Short",
                                               [box["imp_start"]]) or bottom - height
                        emit(i, "Reclaim", "Short", c[i], h[broke:i + 1].max() + buffer, target)
                    box["state"] = "inside"
                elif i > broke and c[i] > h[broke:i].max():         # confirmed breakout up
                    target = nearest_level(levels, i, c[i], "Long", [box["imp_start"]]) or top + box["imp_size"]
                    emit(i, "Breakout", "Long", c[i], l[broke + 1:i + 1].min() - buffer, target)
                    retire(i)
                elif i - broke >= CONFIRM_BARS:
                    retire(i)
            else:
                if c[i] > bottom:                                  # failed break: reclaim long
                    if ("Reclaim", "Long") not in box["used"]:
                        box["used"].add(("Reclaim", "Long"))
                        target = nearest_level(levels, i, top, "Long",
                                               [box["imp_start"]]) or top + height
                        emit(i, "Reclaim", "Long", c[i], l[broke:i + 1].min() - buffer, target)
                    box["state"] = "inside"
                elif i > broke and c[i] < l[broke:i].min():         # confirmed breakout down
                    target = nearest_level(levels, i, c[i], "Short", [box["imp_start"]]) or bottom - box["imp_size"]
                    emit(i, "Breakout", "Short", c[i], h[broke + 1:i + 1].max() + buffer, target)
                    retire(i)
                elif i - broke >= CONFIRM_BARS:
                    retire(i)
            continue

        impulse = find_impulse(h, l, c, i, med)
        if impulse is not None:
            direction, start, size = impulse
            end = start + size if direction == "up" else start - size
            # A big move starts a new box, unless it is just the tail of the move that started the current one:
            # the move must reach a new extreme, or be a bigger move the other way.
            fresh = (box is None
                     or (direction == box["imp_dir"] and (end > box["imp_end"] if direction == "up" else end < box["imp_end"]))
                     or (direction != box["imp_dir"] and size >= box["imp_size"]))
            if fresh:
                if box is not None and direction == box["imp_dir"] and i + 1 - box["start"] <= IMPULSE_BARS:
                    start, size = box["imp_start"], abs(end - box["imp_start"])   # same move carrying on
                box = {"ready": False, "start": i + 1, "imp_dir": direction, "imp_start": start, "imp_size": size,
                       "imp_end": end, "top": None, "bottom": None}
                continue
        if box is None:
            continue
        top = h[i] if box["top"] is None else max(box["top"], h[i])
        bottom = l[i] if box["bottom"] is None else min(box["bottom"], l[i])
        box.update(top=top, bottom=bottom)
        if top - bottom > BOX_MAX_FRAC * box["imp_size"]:
            box = None                                             # too tall: not a fair-value box
            continue
        bars = i - box["start"] + 1
        if bars >= BOX_MIN_BARS and top - bottom >= BOX_MIN_TR * med:
            span = slice(box["start"], i + 1)
            top_n, bottom_n = count_touches(h[span], l[span], c[span], top, bottom)
            if top_n >= 1 and bottom_n >= 1 and top_n + bottom_n >= MIN_TOUCHES:
                box.update(ready=True, ready_at=i, state="inside", used=set(), touches=top_n + bottom_n)
    return rows
