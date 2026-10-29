"""Trade notes: the trade-autopsy checklist for every completed live mock trade (from 29 Oct 2026).

Phil's trade autopsy of Haleon v Admiral (29 Oct) found ten things worth recording for every trade.
This script fills them in automatically each evening (FTSE Outcomes workflow, after the outcome
tracker) so the Friday review and the learning loop can group trades by them once there are 30+.
It only records; it never changes the scanner, its rules or its trades.

One row per completed trade in scan_data/trade_notes.csv (trades already noted are kept as they are;
skipped trades have no fill and are left out). Main columns:

  1. picker_view, picker_reason ........ the morning picker's label for the share (news / results days)
  2. gap_pct ............................ the day's open against the previous close, %
  3. stop_inside_range .................. Yes if the price had already traded at or beyond the stop
                                          earlier that day, before the entry candle
  4. planned_stop_pct, fill_stop_pct,
     stop_room_kept_pct ................. room to the stop as planned, after the fill, and the share kept
  5. trend_15m (Up / Down / Flat), ema9_sma20_gap_pct, sma20_slope_pct,
     daily_trend (vs the 50-day average), trend_4h (vs the 4-hour 200 SMA), trends_agreeing (0-3)
  6. fill_vs_ema9_pct ................... entry price against the 9 EMA, % (+ = above)
  7. signal_volume_ratio ................ the signal candle's volume / the average of the 20 before it
  8. target_pct, typical_range_pct,
     target_vs_typical .................. target distance against the share's typical daily range
  9. best_pct, worst_pct ................ furthest the trade went for and against us while open, %
     hold_to_close_gbp .................. what the trade would have made held to the 16:30 close
 10. opposite_ideas_1h, flip_flop ....... scanner ideas in the other direction on the same share
                                          within an hour of the signal

All indicator values use only candles completed before the entry candle. Money is per £100 after
spread-bet costs (10p), as in the live mock trades. Yahoo keeps 15-minute candles for about 60 days,
so a trade older than that cannot be noted.

    python ftse_trade_notes.py                # note every completed trade not yet noted
    python ftse_trade_notes.py --rebuild      # redo every trade still within Yahoo's 60 days
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import ftse_trade_scanner as sc
from ftse_execution import completed_candles, drop_untraded_bars
from ftse_picker import load_views

NOTES_FILE = "trade_notes.csv"
STAKE, COST_PCT = 100.0, 0.10
MAX_AGE_DAYS = 55                  # Yahoo keeps 15-minute candles for about 60 days
FLAT_GAP_PCT = 0.05                # 9 EMA within 0.05% of the 20 SMA = flat (Admiral 29 Oct: 0.02%)
SLOPE_CANDLES = 4                  # slope measured over the last hour of 15-minute candles
FLIP_WINDOW = pd.Timedelta(minutes=60)
SMA_4H_BARS = 200

COLUMNS = [
    "key", "day", "trade_no", "ticker", "company", "setup", "direction", "score", "result", "net_gbp",
    "signal_time", "fill_time", "exit_time", "minutes_held",
    "planned_entry", "fill", "stop", "target", "exit_price",
    "picker_view", "picker_reason", "gap_pct", "stop_inside_range",
    "planned_stop_pct", "fill_stop_pct", "stop_room_kept_pct",
    "trend_15m", "ema9", "sma20", "ema9_sma20_gap_pct", "sma20_slope_pct",
    "daily_trend", "daily_vs_sma50_pct", "trend_4h", "vs_4h_sma200_pct", "trends_agreeing",
    "fill_vs_ema9_pct", "signal_volume_ratio",
    "target_pct", "typical_range_pct", "target_vs_typical",
    "best_pct", "worst_pct", "close_1630", "hold_to_close_gbp",
    "opposite_ideas_1h", "flip_flop", "data_note", "noted_at",
]


def uk(stamp) -> pd.Timestamp:
    """Any timestamp text from the scan files as London time (naive text is already London time)."""
    t = pd.Timestamp(stamp)
    return t.tz_localize(sc.UK_TZ) if t.tzinfo is None else t.tz_convert(sc.UK_TZ)


def _r(value, places=2):
    return round(float(value), places) if value is not None and math.isfinite(float(value)) else None


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


def daily_by_date(daily: pd.DataFrame) -> pd.DataFrame:
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


def note_trade(trade: dict, outcome: dict, m15: pd.DataFrame, daily: pd.DataFrame,
               closes_4h: pd.Series, setups_day: pd.DataFrame, views: dict | None) -> dict | None:
    """The checklist for one completed trade, or None when the candles needed are missing.

    m15: completed 15-minute candles in London time (zero-volume candles removed).
    daily: daily_by_date() frame. closes_4h: four_hour_closes(). setups_day: that day's setups.csv rows.
    """
    day = str(trade["day"])
    sign = 1 if trade["direction"] == "Long" else -1
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
    notes = []
    if abs(float(later["open"].iloc[0]) - fill) / fill > 0.001:
        notes.append("entry candle's open differs from the recorded fill")

    # 3. was the stop inside today's range before the entry candle?
    earlier = today[today.index < fill_start]
    if earlier.empty:
        inside = "No"
    elif sign == 1:
        inside = "Yes" if float(earlier["low"].min()) <= stop else "No"
    else:
        inside = "Yes" if float(earlier["high"].max()) >= stop else "No"

    # 5, 6. averages from candles completed before the entry candle
    close = before["close"].astype(float)
    ema9 = close.ewm(span=9, adjust=False).mean()
    sma20 = close.rolling(20).mean()
    e, s = float(ema9.iloc[-1]), float(sma20.iloc[-1])
    gap = (e - s) / s * 100
    slope = (s - float(sma20.iloc[-1 - SLOPE_CANDLES])) / s * 100
    if gap > FLAT_GAP_PCT and slope > 0:
        t15 = "Up"
    elif gap < -FLAT_GAP_PCT and slope < 0:
        t15 = "Down"
    else:
        t15 = "Flat"

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
    agreeing = sum(1 for t in (t15, d_trend, h4_trend) if t == wanted)

    # 2. gap at the open, and the close for "held to 16:30"
    gap_pct, close_1630 = None, None
    if day in daily.index:
        close_1630 = float(daily.loc[day, "close"])
        if len(previous_days):
            prev = float(previous_days["close"].iloc[-1])
            gap_pct = (float(daily.loc[day, "open"]) - prev) / prev * 100
    else:
        close_1630 = float(today["close"].iloc[-1])
        notes.append("no daily candle: close from the last 15-minute candle")

    # 7. signal candle volume
    vol_ratio = None
    if "volume" in frame.columns and signal_candle in frame.index:
        n = frame.index.get_loc(signal_candle)
        prior = frame["volume"].iloc[max(0, n - 20):n].astype(float)
        if len(prior) and prior.mean() > 0:
            vol_ratio = float(frame["volume"].iloc[n]) / prior.mean()

    # 8. target against the typical day
    typical = sc.typical_daily_move_pct(frame[frame.index <= today.index[-1]])
    target_pct = abs(target - fill) / fill * 100

    # 9. best and worst while open
    held = today[(today.index >= fill_start) & (today.index < exit_time)]
    if held.empty:
        held = later.iloc[:1]
    if sign == 1:
        best = (float(held["high"].max()) - fill) / fill * 100
        worst = (fill - float(held["low"].min())) / fill * 100
    else:
        best = (fill - float(held["low"].min())) / fill * 100
        worst = (float(held["high"].max()) - fill) / fill * 100

    # 10. ideas the other way on the same share within an hour
    opposite = 0
    if setups_day is not None and len(setups_day):
        same = setups_day[(setups_day["ticker"] == trade["ticker"]) & (setups_day["direction"] != trade["direction"])]
        times = pd.Series([uk(t) for t in same["run_time"]]) if len(same) else pd.Series(dtype=object)
        opposite = int(sum(abs(t - signal_time) <= FLIP_WINDOW for t in times))

    view, reason = (views or {}).get(str(trade["ticker"]).upper(), ("", ""))
    planned_room = abs(planned - stop) / planned * 100
    fill_room = abs(fill - stop) / fill * 100
    net = sign * (exit_price - fill) / fill * STAKE - STAKE * COST_PCT / 100
    hold = sign * (close_1630 - fill) / fill * STAKE - STAKE * COST_PCT / 100
    return {
        "key": trade["key"], "day": day, "trade_no": trade.get("trade_no"), "ticker": trade["ticker"],
        "company": trade.get("company", ""), "setup": trade.get("setup", ""), "direction": trade["direction"],
        "score": trade.get("score"), "result": outcome["outcome"], "net_gbp": _r(net),
        "signal_time": signal_time.strftime("%H:%M"), "fill_time": fill_start.strftime("%H:%M"),
        "exit_time": exit_time.strftime("%H:%M"),
        "minutes_held": int((exit_time - fill_start).total_seconds() // 60),
        "planned_entry": _r(planned, 4), "fill": _r(fill, 4), "stop": _r(stop, 4), "target": _r(target, 4),
        "exit_price": _r(exit_price, 4),
        "picker_view": view or ("No view" if views is not None else "No picker file"), "picker_reason": reason,
        "gap_pct": _r(gap_pct), "stop_inside_range": inside,
        "planned_stop_pct": _r(planned_room), "fill_stop_pct": _r(fill_room),
        "stop_room_kept_pct": _r(fill_room / planned_room * 100 if planned_room else None, 0),
        "trend_15m": t15, "ema9": _r(e, 4), "sma20": _r(s, 4), "ema9_sma20_gap_pct": _r(gap, 3),
        "sma20_slope_pct": _r(slope, 3),
        "daily_trend": d_trend, "daily_vs_sma50_pct": _r(d_pct), "trend_4h": h4_trend, "vs_4h_sma200_pct": _r(h4_pct),
        "trends_agreeing": agreeing, "fill_vs_ema9_pct": _r((fill - e) / e * 100),
        "signal_volume_ratio": _r(vol_ratio, 1),
        "target_pct": _r(target_pct), "typical_range_pct": _r(typical),
        "target_vs_typical": _r(target_pct / typical if typical else None),
        "best_pct": _r(best), "worst_pct": _r(worst), "close_1630": _r(close_1630, 4), "hold_to_close_gbp": _r(hold),
        "opposite_ideas_1h": opposite, "flip_flop": "Yes" if opposite else "No",
        "data_note": "; ".join(notes),
    }


def completed_trades(data_dir: Path) -> pd.DataFrame:
    """Live mock trades joined to their resolved outcome (status done only)."""
    trades_path, outcomes_path = data_dir / "live_mock_trades.csv", data_dir / "outcomes.v3.csv"
    if not trades_path.exists() or not outcomes_path.exists():
        return pd.DataFrame()
    trades = pd.read_csv(trades_path, dtype={"day": str})
    outcomes = pd.read_csv(outcomes_path, dtype={"day": str})
    outcomes = outcomes[outcomes["status"] == "done"][["key", "outcome", "fill", "exit_price", "exit_time"]]
    return trades.merge(outcomes, on="key", how="inner").dropna(subset=["fill", "exit_price", "exit_time"])


def read_notes(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(path, dtype={"day": str})


def build_notes(todo: pd.DataFrame, m15: dict, daily: dict, hourly: dict, data_dir: Path,
                now_text: str) -> tuple[list[dict], list[str]]:
    setups = sc._read_table(data_dir / "setups.csv", sc.SETUP_COLUMNS)
    rows, missed = [], []
    closes4 = {t: four_hour_closes(h) for t, h in hourly.items()}
    dailies = {t: daily_by_date(d) for t, d in daily.items()}
    for _, trade in todo.iterrows():
        ticker, day = trade["ticker"], str(trade["day"])
        frame = m15.get(ticker)
        row = None
        if frame is not None and not frame.empty:
            row = note_trade(trade.to_dict(), trade.to_dict(), frame, dailies.get(ticker, daily_by_date(None)),
                             closes4.get(ticker, pd.Series(dtype=float)),
                             setups[setups["day"].astype(str) == day] if len(setups) else setups,
                             load_views(data_dir, day))
        if row is None:
            missed.append(f"{day} {ticker}")
            continue
        row["noted_at"] = now_text
        rows.append(row)
    return rows, missed


def summary(notes: pd.DataFrame) -> list[str]:
    """A few plain-English groupings for the log (the Friday review does the real comparison)."""
    lines = [f"{len(notes)} trades noted, £{notes['net_gbp'].sum():+.2f} in all."]
    for column in ("stop_inside_range", "trend_15m", "flip_flop", "picker_view"):
        parts = []
        for value, part in notes.groupby(column, dropna=False):
            parts.append(f"{value or 'blank'} {len(part)} (£{part['net_gbp'].sum():+.2f})")
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
    oldest = str((now - pd.Timedelta(days=MAX_AGE_DAYS)).date())
    in_reach = trades[trades["day"] >= oldest]
    todo = in_reach if rebuild else in_reach[~in_reach["key"].isin(set(old["key"]))]
    if todo.empty:
        print(f"All {len(old)} completed trades already noted.")
        return 0
    print(f"Trade notes: {len(todo)} trade(s) to note; downloading candles for {todo['ticker'].nunique()} share(s)...")
    m15, daily, hourly = fetch(sorted(todo["ticker"].unique()))
    rows, missed = build_notes(todo, m15, daily, hourly, data_dir, now.strftime("%Y-%m-%d %H:%M"))
    if missed:
        print(f"Could not note {len(missed)} trade(s) (candles missing; will retry next run): " + ", ".join(missed[:10]))
    if not rows:
        return 1 if missed else 0
    fresh = pd.DataFrame(rows, columns=COLUMNS)
    kept = old[~old["key"].isin(set(fresh["key"]))]
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
    parser = argparse.ArgumentParser(description="Record the trade-autopsy checklist for every completed live mock trade")
    parser.add_argument("--data-dir", default=str(sc.DEFAULT_DATA_DIR))
    parser.add_argument("--rebuild", action="store_true", help="redo every trade still within Yahoo's 60 days")
    args = parser.parse_args()
    sys.exit(run(Path(args.data_dir), args.rebuild))


if __name__ == "__main__":
    main()
