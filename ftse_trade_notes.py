"""Trade notes: the trade-autopsy and trade-feasibility checklist for every completed live mock trade.

From 29 Oct 2026 (Phil's trade autopsy of Haleon v Admiral, and the research review that followed).
Run each evening in the FTSE Outcomes workflow, after the outcome tracker. Each completed trade gets
one row in scan_data/trade_notes.csv, so the Friday review can group trades by these features.

RECORDED FEATURES ONLY. Nothing here changes the scanner, its rules or its trades. A feature can be
promoted to a rule only after it holds up on trades it was not chosen from (out-of-sample), with
Phil's agreement and a backtest.

The trade-feasibility bundle (assessed between the signal and the mock entry):
  fill_room_pct, fill_room_band ...... room to the stop left after the fill, as % of the planned room
                                       (bands: 90%+, 75-90%, 50-75%, under 50%)
  stop_pretested ..................... Yes if the price had already traded at or beyond the stop earlier
                                       that day, before the entry candle
  target_vs_daily_range .............. target distance / the share's typical daily range
  signal_intact, fill_vs_zone,
  fill_zone_margin_pct ............... where the fill sat against the scanner's zone: Beyond (past the
                                       zone on the trade's side), Inside, or Through (back out the
                                       wrong side). Breakout: Yes only if Beyond; Range: No if Through
  regime, ema_state, flip_count_60m .. Sideways / Trending / Mixed (see regime_label); flips are ideas the
                                       other way on the same share in the 60 minutes up to the signal
  zone_age_minutes, zone_formed_today,
  zone_touches ....................... trading minutes from the zone's first touch to the signal candle,
                                       whether every touch was that day, and the number of touches
  zones_near, zones_between .......... zone density (from 30 Oct 2026): how many of the scanner's zones sat
                                       within 1.2% of the fill, and how many between the fill and the target
  ema_chase_pct ...................... "chasing": the fill's distance beyond the 9 EMA in the trade's
                                       direction, % (+ = above for a buy, below for a sell)

Skipped trades (from 30 Oct 2026): a trade the fill check skipped (too little room to the stop at the entry
price) or the chase check skipped (a setting, off unless switched on) is noted too, with entered = "No: fill
check" or "No: chase check", net_gbp left empty, and counterfactual_result /
counterfactual_gbp saying what it would have done had it been entered. So the review can tell whether the
fill check removes losers or cuts winners. Totals of real results must use entered = "Yes" rows only.

Context and results:
  news_day, picker_view, picker_reason, gap_pct, higher_tf_state (daily 50-day / 4-hour 200 SMA),
  signal_volume_ratio, mfe_pct / mae_pct (furthest for / against while open), close_outcome_gbp
  (the result had it been held to the 16:30 close), data_quality_flag (extreme single-candle wicks, a stop
  hit only by a wick, volume spikes: check against broker data before blaming the strategy).

Indicators use only candles completed before the entry candle. Money is per £100 after spread-bet costs
(10p), as in the live mock trades. Yahoo keeps 15-minute candles for about 60 days, so older trades
cannot be noted. Data are Yahoo's, about 15 minutes delayed; volume can be incomplete.

    python ftse_trade_notes.py                # note every completed trade not yet noted
    python ftse_trade_notes.py --rebuild      # redo every trade still within Yahoo's 60 days
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import ftse_trade_scanner as sc
from ftse_execution import completed_candles, drop_untraded_bars, simulate
from ftse_picker import load_views

NOTES_FILE = "trade_notes.csv"
SKIP_REASONS = {"thin_stop": ("No: fill check", "skipped: thin stop"),
                "chased": ("No: chase check", "skipped: too far past the 9 EMA")}
STAKE, COST_PCT = 100.0, 0.10
MAX_AGE_DAYS = 55                  # Yahoo keeps 15-minute candles for about 60 days
FLAT_GAP_PCT = 0.05                # 9 EMA within 0.05% of the 20 SMA = flat (Admiral 29 Oct: 0.03%)
SLOPE_CANDLES = 4                  # slope measured over the last hour of 15-minute candles
FLIP_WINDOW = pd.Timedelta(minutes=60)
SMA_4H_BARS = 200
NEAR_AVERAGE_PCT = 1.0             # within 1% of the daily 50-day and 4-hour 200 SMA = no higher-timeframe trend
EXTREME_CANDLE = 4.0               # a wick 4x the median candle range of the 20 before entry
VOLUME_SPIKE = 5.0                 # a candle's volume 5x the average of the 20 before it
NEAR_ZONE_PCT = 1.2                # zone density: zones within 1.2% of the fill
NEWS_WORDS = re.compile(r"result|trading update|update|statement|production|report|profit warning|guidance|"
                        r"takeover|offer|bid|ex-dividend|agm|capital markets|announcement", re.IGNORECASE)

COLUMNS = [
    "key", "day", "trade_no", "ticker", "company", "setup", "direction", "score", "result", "net_gbp",
    "signal_time", "fill_time", "exit_time", "minutes_held",
    "entered", "counterfactual_result", "counterfactual_gbp",
    "planned_entry", "fill", "stop", "target", "exit_price", "zone_low", "zone_high",
    "zone_age_minutes", "zone_formed_today", "zone_touches", "zones_near", "zones_between",
    # trade feasibility
    "planned_stop_pct", "fill_stop_pct", "fill_room_pct", "fill_room_band", "stop_pretested",
    "target_pct", "typical_range_pct", "target_vs_daily_range",
    "fill_vs_zone", "fill_zone_margin_pct", "signal_intact",
    "regime", "ema_state", "flip_count_60m",
    # context
    "news_day", "picker_view", "picker_reason", "gap_pct",
    "higher_tf_state", "daily_trend", "daily_vs_sma50_pct", "trend_4h", "vs_4h_sma200_pct", "trends_agreeing",
    "ema9", "sma20", "ema9_sma20_gap_pct", "sma20_slope_pct", "fill_vs_ema9_pct", "ema_chase_pct", "signal_volume_ratio",
    # what happened
    "mfe_pct", "mae_pct", "close_1630", "close_outcome_gbp", "data_quality_flag", "noted_at",
]


def uk(stamp) -> pd.Timestamp:
    """Any timestamp text from the scan files as London time (naive text is already London time)."""
    t = pd.Timestamp(stamp)
    return t.tz_localize(sc.UK_TZ) if t.tzinfo is None else t.tz_convert(sc.UK_TZ)


def _r(value, places=2):
    if value is None:
        return None
    value = float(value)
    return round(value, places) if math.isfinite(value) else None


def four_hour_closes(hourly: pd.DataFrame) -> pd.Series:
    """Close of each 4-hour block from the 08:00 open (08-12, 12-16, 16-16:30), indexed by its END time,
    as in the model check backtest."""
    if hourly is None or hourly.empty:
        return pd.Series(dtype=float)
    opening = pd.Timedelta(hours=8)
    since_open = hourly.index - hourly.index.normalize() - opening
    block = np.floor(since_open / pd.Timedelta(hours=4)).astype(int)
    rows = []
    for (day, number), part in hourly.groupby([hourly.index.date, np.asarray(block)]):
        start = pd.Timestamp(day).tz_localize(hourly.index.tz) + opening + pd.Timedelta(hours=4 * number)
        end = min(start + pd.Timedelta(hours=4), part.index[-1] + pd.Timedelta(hours=1))
        rows.append((end, float(part["close"].iloc[-1])))
    return pd.Series([v for _, v in rows], index=pd.DatetimeIndex([t for t, _ in rows])).sort_index()


def daily_by_date(daily: pd.DataFrame | None) -> pd.DataFrame:
    """Daily candles keyed by UK date text."""
    if daily is None or daily.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close"])
    index = pd.DatetimeIndex(daily.index)
    index = index.tz_convert(sc.UK_TZ) if index.tz is not None else index
    frame = daily.copy()
    frame.index = [str(d.date()) for d in index]
    frame = frame[~frame.index.duplicated(keep="last")]
    return frame[pd.to_numeric(frame["close"], errors="coerce") > 0]


def trend_word(above: bool | None) -> str:
    return "" if above is None else ("Up" if above else "Down")


def room_band(pct: float | None) -> str:
    if pct is None:
        return ""
    return "90%+" if pct >= 90 else "75-90%" if pct >= 75 else "50-75%" if pct >= 50 else "under 50%"


def zone_position(direction: str, fill: float, low: float | None, high: float | None) -> tuple[str, float | None]:
    """Beyond / Inside / Through, and the margin past the zone's far edge on the trade's side, % of the fill
    (negative = not past it)."""
    if low is None or high is None or not (math.isfinite(low) and math.isfinite(high)):
        return "", None
    if direction == "Long":
        place = "Beyond" if fill > high else "Through" if fill < low else "Inside"
        return place, (fill - high) / fill * 100
    place = "Beyond" if fill < low else "Through" if fill > high else "Inside"
    return place, (low - fill) / fill * 100


def signal_intact(setup: str, place: str) -> str:
    """Breakout (reclaim): the price must still be past the zone. Range: it must not have gone back through it."""
    if not place:
        return ""
    if setup == "Breakout":
        return "Yes" if place == "Beyond" else "No"
    return "No" if place == "Through" else "Yes"


def regime_label(ema_state: str, flips: int, daily_pct: float | None, h4_pct: float | None,
                 daily_trend: str, trend_4h: str) -> str:
    """Sideways: flat 9 EMA / 20 SMA and either the scanner flip-flopped on the share within the hour, or the
    price sat within 1% of both higher-timeframe averages. Trending: the 15-minute trend agrees with at least
    one higher timeframe. Mixed: anything else."""
    near = (daily_pct is not None and h4_pct is not None
            and abs(daily_pct) < NEAR_AVERAGE_PCT and abs(h4_pct) < NEAR_AVERAGE_PCT)
    if ema_state == "Flat" and (flips > 0 or near):
        return "Sideways"
    if ema_state in ("Up", "Down") and ema_state in (daily_trend, trend_4h):
        return "Trending"
    return "Mixed"


def quality_flags(direction: str, result: str, stop: float, held: pd.DataFrame, before: pd.DataFrame,
                  frame: pd.DataFrame, exit_time: pd.Timestamp) -> list[str]:
    """Things in the candles that may be bad prices or one-off prints rather than the market."""
    flags = []
    ranges = (before["high"] - before["low"]).astype(float).tail(20)
    typical = float(ranges.median()) if len(ranges) else 0.0
    if typical > 0:
        for t, bar in held.iterrows():
            o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
            wick = max(h - max(o, c), min(o, c) - l)
            if wick >= EXTREME_CANDLE * typical:
                flags.append(f"extreme wick {t.strftime('%H:%M')} ({wick / typical:.0f}x a normal candle)")
    if result == "stop" and len(held):
        last = held.iloc[-1]
        back = float(last["close"]) > stop if direction == "Long" else float(last["close"]) < stop
        if back:
            text = f"stop hit only by a wick that closed back ({held.index[-1].strftime('%H:%M')})"
            if "volume" in frame.columns:
                n = frame.index.get_loc(held.index[-1])
                prior = frame["volume"].iloc[max(0, n - 20):n].astype(float)
                if len(prior) and prior.mean() > 0 and float(last["volume"]) >= VOLUME_SPIKE * prior.mean():
                    text += f" on a volume spike ({float(last['volume']) / prior.mean():.0f}x)"
            flags.append(text)
    return flags


def zones_at(frame: pd.DataFrame, signal_candle: pd.Timestamp, index_label: str = "FTSE 100") -> list:
    """Every qualified zone the scanner would see at the signal, from the candles completed by then."""
    cfg = sc.INDEX_RULES.get(index_label, sc.INDEX_RULES["FTSE 100"])
    visible = frame[frame.index <= signal_candle]
    if len(visible) <= cfg.swing_lookback * 2:
        return []
    return sc.qualify_zones(sc.cluster_zones(sc.find_swing_points(visible, cfg.swing_lookback), cfg.zone_tolerance_pct),
                            visible, cfg)


def zone_density(zones: list, fill: float, target: float) -> tuple[int, int]:
    """Zones whose middle is within NEAR_ZONE_PCT of the fill, and zones whose middle lies between fill and target."""
    low, high = min(fill, target), max(fill, target)
    near = sum(abs(z.mid - fill) / fill * 100 <= NEAR_ZONE_PCT for z in zones)
    between = sum(low < z.mid < high for z in zones)
    return near, between


def zone_history(frame: pd.DataFrame, signal_candle: pd.Timestamp, low: float | None, high: float | None,
                 index_label: str = "FTSE 100", zones: list | None = None) -> tuple[pd.Timestamp | None, int]:
    """First touch time and touch count of the scanner's zone, rebuilt from the candles completed by the signal
    with the scanner's own zone rules. (None, 0) if the zone cannot be matched."""
    if low is None or high is None or not (math.isfinite(low) and math.isfinite(high)):
        return None, 0
    if zones is None:
        zones = zones_at(frame, signal_candle, index_label)
    tolerance = max(abs(high), abs(low)) * 0.0005
    for zone in zones:
        if abs(zone.low - low) <= tolerance and abs(zone.high - high) <= tolerance and zone.touches:
            return min(point.ts for point in zone.touches), zone.touch_count
    return None, 0


def note_trade(trade: dict, outcome: dict, m15: pd.DataFrame, daily: pd.DataFrame,
               closes_4h: pd.Series, setups_day: pd.DataFrame, views: dict | None,
               zone_info: tuple | None = None) -> dict | None:
    """The checklist for one completed trade, or None when the candles needed are missing.

    m15: completed 15-minute candles in London time (zero-volume candles removed).
    daily: daily_by_date() frame. closes_4h: four_hour_closes(). setups_day: that day's setups.csv rows.
    views: that day's picker views (None = no picker file).
    zone_info: (zone_low, zone_high, first touch, touches) when the caller already knows the zone (backtests);
    otherwise the zone comes from setups_day and is rebuilt from the candles.
    """
    day = str(trade["day"])
    direction = trade["direction"]
    sign = 1 if direction == "Long" else -1
    fill, stop, target = float(outcome["fill"]), float(trade["stop"]), float(trade["target"])
    planned, exit_price = float(trade["entry"]), float(outcome["exit_price"])
    signal_time, exit_time = uk(trade["signal_time"]), uk(outcome["exit_time"])
    signal_candle = uk(trade["data_time"])

    frame = m15.sort_index()
    today = frame[frame.index.strftime("%Y-%m-%d") == day]
    later = today[today.index >= signal_time]
    if today.empty or later.empty:
        return None
    fill_start = later.index[0]
    before = frame[frame.index < fill_start]
    if len(before) < 25:
        return None
    flags = []
    if abs(float(later["open"].iloc[0]) - fill) / fill > 0.001:
        flags.append("entry candle's open differs from the recorded fill")

    # stop already traded through earlier today?
    earlier = today[today.index < fill_start]
    if earlier.empty:
        pretested = "No"
    elif sign == 1:
        pretested = "Yes" if float(earlier["low"].min()) <= stop else "No"
    else:
        pretested = "Yes" if float(earlier["high"].max()) >= stop else "No"

    # averages from candles completed before the entry candle
    close = before["close"].astype(float)
    ema9 = close.ewm(span=9, adjust=False).mean()
    sma20 = close.rolling(20).mean()
    e, s = float(ema9.iloc[-1]), float(sma20.iloc[-1])
    gap = (e - s) / s * 100
    slope = (s - float(sma20.iloc[-1 - SLOPE_CANDLES])) / s * 100
    ema_state = "Up" if gap > FLAT_GAP_PCT and slope > 0 else "Down" if gap < -FLAT_GAP_PCT and slope < 0 else "Flat"

    previous_days = daily[daily.index < day]
    d_trend, d_pct = "", None
    if len(previous_days) >= 50:
        last = float(previous_days["close"].iloc[-1])
        sma50 = float(previous_days["close"].astype(float).tail(50).mean())
        d_trend, d_pct = trend_word(last > sma50), (last - sma50) / sma50 * 100
    h4_done = closes_4h[closes_4h.index <= fill_start] if len(closes_4h) else closes_4h
    h4_trend, h4_pct = "", None
    if len(h4_done) >= SMA_4H_BARS:
        sma200 = float(h4_done.tail(SMA_4H_BARS).mean())
        h4_trend, h4_pct = trend_word(fill > sma200), (fill - sma200) / sma200 * 100
    wanted = "Up" if sign == 1 else "Down"
    agreeing = sum(1 for t in (ema_state, d_trend, h4_trend) if t == wanted)
    higher = " / ".join(f"{name} {t}" for name, t in (("Daily", d_trend), ("4h", h4_trend)) if t)

    # gap at the open, and the close for "held to 16:30"
    gap_pct = None
    if day in daily.index:
        close_1630 = float(daily.loc[day, "close"])
        if len(previous_days):
            prev = float(previous_days["close"].iloc[-1])
            gap_pct = (float(daily.loc[day, "open"]) - prev) / prev * 100
    else:
        close_1630 = float(today["close"].iloc[-1])
        flags.append("no daily candle: close from the last 15-minute candle")

    # signal candle volume
    vol_ratio = None
    if "volume" in frame.columns and signal_candle in frame.index:
        n = frame.index.get_loc(signal_candle)
        prior = frame["volume"].iloc[max(0, n - 20):n].astype(float)
        if len(prior) and prior.mean() > 0:
            vol_ratio = float(frame["volume"].iloc[n]) / prior.mean()

    # target against the typical day
    typical = sc.typical_daily_move_pct(frame[frame.index <= today.index[-1]])
    target_pct = abs(target - fill) / fill * 100

    # furthest for and against while open
    held = today[(today.index >= fill_start) & (today.index < exit_time)]
    if held.empty:
        held = later.iloc[:1]
    if sign == 1:
        mfe = (float(held["high"].max()) - fill) / fill * 100
        mae = (fill - float(held["low"].min())) / fill * 100
    else:
        mfe = (fill - float(held["low"].min())) / fill * 100
        mae = (float(held["high"].max()) - fill) / fill * 100
    flags += quality_flags(direction, str(outcome["outcome"]), stop, held, before, frame, exit_time)

    # the scanner's own setup row (zone) and ideas the other way within the hour
    flips, zone_low, zone_high = 0, None, None
    if setups_day is not None and len(setups_day):
        same_share = setups_day[setups_day["ticker"] == trade["ticker"]]
        mine = same_share[(same_share["direction"] == direction)
                          & (same_share["run_time"].astype(str) == str(trade["signal_time"]))]
        if "setup" in mine.columns:
            mine = mine[mine["setup"] == trade.get("setup")]
        if len(mine) and {"zone_low", "zone_high"} <= set(mine.columns):
            zone_low, zone_high = float(mine["zone_low"].iloc[0]), float(mine["zone_high"].iloc[0])
        other = same_share[same_share["direction"] != direction]
        flips = int(sum(signal_time - FLIP_WINDOW <= uk(t) <= signal_time for t in other["run_time"]))
    every_zone = zones_at(frame, signal_candle, str(trade.get("index") or "FTSE 100"))
    if zone_info is not None:
        zone_low, zone_high, first_touch, touches = zone_info
    else:
        first_touch, touches = zone_history(frame, signal_candle, zone_low, zone_high, str(trade.get("index") or "FTSE 100"),
                                            zones=every_zone)
    zones_near, zones_between = zone_density(every_zone, fill, target)
    place, margin = zone_position(direction, fill, zone_low, zone_high)
    zone_age, zone_today = None, ""
    if first_touch is not None:
        zone_age = int(((frame.index >= first_touch) & (frame.index <= signal_candle)).sum()) * 15
        zone_today = "Yes" if first_touch.strftime("%Y-%m-%d") == day else "No"

    if views is None:
        view, reason, news = "No picker file", "", ""
    else:
        view, reason = views.get(str(trade["ticker"]).upper(), ("No view", ""))
        news = "Yes" if NEWS_WORDS.search(reason or "") else "No"
    planned_room = abs(planned - stop) / planned * 100
    fill_room = abs(fill - stop) / fill * 100
    kept = fill_room / planned_room * 100 if planned_room else None
    net = sign * (exit_price - fill) / fill * STAKE - STAKE * COST_PCT / 100
    hold = sign * (close_1630 - fill) / fill * STAKE - STAKE * COST_PCT / 100
    return {
        "key": trade["key"], "day": day, "trade_no": trade.get("trade_no"), "ticker": trade["ticker"],
        "company": trade.get("company", ""), "setup": trade.get("setup", ""), "direction": direction,
        "score": trade.get("score"), "result": outcome["outcome"], "net_gbp": _r(net),
        "signal_time": signal_time.strftime("%H:%M"), "fill_time": fill_start.strftime("%H:%M"),
        "exit_time": exit_time.strftime("%H:%M"),
        "minutes_held": int((exit_time - fill_start).total_seconds() // 60),
        "planned_entry": _r(planned, 4), "fill": _r(fill, 4), "stop": _r(stop, 4), "target": _r(target, 4),
        "exit_price": _r(exit_price, 4), "zone_low": _r(zone_low, 4), "zone_high": _r(zone_high, 4),
        "zone_age_minutes": zone_age, "zone_formed_today": zone_today, "zone_touches": touches or None,
        "zones_near": zones_near, "zones_between": zones_between,
        "entered": "Yes",
        "planned_stop_pct": _r(planned_room), "fill_stop_pct": _r(fill_room),
        "fill_room_pct": _r(kept, 0), "fill_room_band": room_band(kept), "stop_pretested": pretested,
        "target_pct": _r(target_pct), "typical_range_pct": _r(typical),
        "target_vs_daily_range": _r(target_pct / typical if typical else None),
        "fill_vs_zone": place, "fill_zone_margin_pct": _r(margin, 3),
        "signal_intact": signal_intact(str(trade.get("setup", "")), place),
        "regime": regime_label(ema_state, flips, d_pct, h4_pct, d_trend, h4_trend),
        "ema_state": ema_state, "flip_count_60m": flips,
        "news_day": news, "picker_view": view, "picker_reason": reason, "gap_pct": _r(gap_pct),
        "higher_tf_state": higher, "daily_trend": d_trend, "daily_vs_sma50_pct": _r(d_pct),
        "trend_4h": h4_trend, "vs_4h_sma200_pct": _r(h4_pct), "trends_agreeing": agreeing,
        "ema9": _r(e, 4), "sma20": _r(s, 4), "ema9_sma20_gap_pct": _r(gap, 3), "sma20_slope_pct": _r(slope, 3),
        "fill_vs_ema9_pct": _r((fill - e) / e * 100), "ema_chase_pct": _r(sign * (fill - e) / e * 100),
        "signal_volume_ratio": _r(vol_ratio, 1),
        "mfe_pct": _r(mfe), "mae_pct": _r(mae), "close_1630": _r(close_1630, 4), "close_outcome_gbp": _r(hold),
        "data_quality_flag": "; ".join(flags),
    }


def completed_trades(data_dir: Path) -> pd.DataFrame:
    """Live mock trades joined to their resolved outcome (status done only)."""
    trades_path, outcomes_path = data_dir / "live_mock_trades.csv", data_dir / "outcomes.v3.csv"
    if not trades_path.exists() or not outcomes_path.exists():
        return pd.DataFrame()
    trades = pd.read_csv(trades_path, dtype={"day": str})
    outcomes = pd.read_csv(outcomes_path, dtype={"day": str})
    done = outcomes["status"] == "done"
    skipped = (outcomes["status"] == "skipped") & (outcomes["outcome"].isin(SKIP_REASONS))
    outcomes = outcomes[done | skipped][["key", "status", "outcome", "fill", "exit_price", "exit_time"]]
    merged = trades.merge(outcomes, on="key", how="inner")
    return merged[(merged["status"] == "skipped") | merged[["fill", "exit_price", "exit_time"]].notna().all(axis=1)]


def counterfactual(trade: dict, frame: pd.DataFrame, now: pd.Timestamp) -> dict | None:
    """What a skipped trade would have done had it been entered at the next candle's open (no fill check)."""
    signal_close = max(uk(trade["data_time"]) + pd.Timedelta(minutes=15), uk(trade["signal_time"]))
    sim = simulate(trade["direction"], float(trade["stop"]), float(trade["target"]), signal_close, frame, 1, now, "15m")
    if sim.status != "done":
        return None
    return {"fill": sim.fill, "exit_price": sim.exit_price, "exit_time": sim.exit_time, "outcome": sim.outcome}


def read_notes(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path, dtype={"day": str})


def build_notes(todo: pd.DataFrame, m15: dict, daily: dict, hourly: dict, data_dir: Path,
                now_text: str, now: pd.Timestamp | None = None) -> tuple[list[dict], list[str]]:
    now = now or pd.Timestamp.now(tz=sc.UK_TZ)
    setups = sc._read_table(data_dir / "setups.csv", sc.SETUP_COLUMNS)
    rows, missed = [], []
    closes4 = {t: four_hour_closes(h) for t, h in hourly.items()}
    dailies = {t: daily_by_date(d) for t, d in daily.items()}
    for _, trade in todo.iterrows():
        ticker, day = trade["ticker"], str(trade["day"])
        frame = m15.get(ticker)
        row = None
        if frame is not None and not frame.empty:
            outcome = trade.to_dict()
            was_skipped = str(trade.get("status", "done")) == "skipped"
            if was_skipped:
                outcome = counterfactual(trade.to_dict(), frame, now)
            if outcome is not None:
                row = note_trade(trade.to_dict(), outcome, frame, dailies.get(ticker, daily_by_date(None)),
                                 closes4.get(ticker, pd.Series(dtype=float)),
                                 setups[setups["day"].astype(str) == day] if len(setups) else setups,
                                 load_views(data_dir, day))
            if row is not None and was_skipped:
                entered, result = SKIP_REASONS.get(str(trade.get("outcome")), SKIP_REASONS["thin_stop"])
                row.update({"entered": entered, "counterfactual_result": row["result"],
                            "counterfactual_gbp": row["net_gbp"], "result": result, "net_gbp": None})
        if row is None:
            missed.append(f"{day} {ticker}")
            continue
        row["noted_at"] = now_text
        rows.append(row)
    return rows, missed


def summary(notes: pd.DataFrame) -> list[str]:
    """A few plain-English groupings for the log (the Friday review does the real comparison)."""
    skipped = notes[notes["entered"] != "Yes"]
    notes = notes[notes["entered"] == "Yes"]
    lines = [f"{len(notes)} trades noted, £{notes['net_gbp'].sum():+.2f} in all "
             f"(held to 16:30 instead: £{notes['close_outcome_gbp'].sum():+.2f})."]
    if len(skipped):
        lines.append(f"  {len(skipped)} trade(s) skipped by the fill check would have made "
                     f"£{skipped['counterfactual_gbp'].sum():+.2f} if entered.")
    for column in ("fill_room_band", "stop_pretested", "signal_intact", "regime", "news_day", "zone_formed_today"):
        parts = []
        for value, part in notes.groupby(notes[column].fillna("blank").replace("", "blank")):
            parts.append(f"{value} {len(part)} (£{part['net_gbp'].sum():+.2f})")
        lines.append(f"  {column}: " + ", ".join(parts))
    return lines


def download(tickers: list[str]) -> tuple[dict, dict, dict]:
    m15 = {t: drop_untraded_bars(completed_candles(f, "15m")) for t, f in sc.fetch_many(tickers, "15m", "60d").items()}
    hourly = {t: completed_candles(f, "1h") for t, f in sc.fetch_many(tickers, "1h", "1y").items()}
    daily = sc.fetch_many(tickers, "1d", "1y")
    return m15, daily, hourly


def run(data_dir: Path, rebuild: bool, now: pd.Timestamp | None = None, fetch=download) -> int:
    now = now or pd.Timestamp.now(tz=sc.UK_TZ)
    trades = completed_trades(data_dir)
    path = data_dir / NOTES_FILE
    old = read_notes(path)
    if trades.empty:
        print("No completed live mock trades yet. Nothing to note.")
        return 0
    if len(old) and list(old.columns) != COLUMNS:
        print("Trade notes file has an older layout: rebuilding every trade still in reach.")
        rebuild = True
    oldest = str((now - pd.Timedelta(days=MAX_AGE_DAYS)).date())
    in_reach = trades[trades["day"] >= oldest]
    todo = in_reach if rebuild else in_reach[~in_reach["key"].isin(set(old["key"]))]
    if todo.empty:
        print(f"All {len(old)} completed trades already noted.")
        return 0
    print(f"Trade notes: {len(todo)} trade(s) to note; downloading candles for {todo['ticker'].nunique()} share(s)...")
    m15, daily, hourly = fetch(sorted(todo["ticker"].unique()))
    rows, missed = build_notes(todo, m15, daily, hourly, data_dir, now.strftime("%Y-%m-%d %H:%M"), now)
    if missed:
        print(f"Could not note {len(missed)} trade(s) (candles missing; will retry next run): " + ", ".join(missed[:10]))
    if not rows:
        return 1 if missed else 0
    fresh = pd.DataFrame(rows, columns=COLUMNS)
    kept = old[~old["key"].isin(set(fresh["key"]))].reindex(columns=COLUMNS)
    every = fresh if kept.empty else pd.concat([kept, fresh], ignore_index=True)
    every = every.reindex(columns=COLUMNS).sort_values(["day", "trade_no"]).reset_index(drop=True)
    tmp = path.with_suffix(".csv.tmp")
    every.to_csv(tmp, index=False)
    tmp.replace(path)
    for line in summary(every):
        print(line)
    print(f"Saved {path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Record the trade-feasibility checklist for every completed live mock trade")
    parser.add_argument("--data-dir", default=str(sc.DEFAULT_DATA_DIR))
    parser.add_argument("--rebuild", action="store_true", help="redo every trade still within Yahoo's 60 days")
    args = parser.parse_args()
    sys.exit(run(Path(args.data_dir), args.rebuild))


if __name__ == "__main__":
    main()
