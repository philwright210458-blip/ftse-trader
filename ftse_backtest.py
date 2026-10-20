"""Backtest: replay the scanner over Yahoo's recent 15-minute history.

For every trading day in the downloaded history this script rebuilds what the
live system would have seen:

  * a full scan of every share at 10:00 (candles completed by 10:00),
  * re-checks of the top 15 shortlisted shares every 15 minutes until 12:45,

using the scanner's own rules (ftse_trade_scanner.py) on only the candles that
had finished by each scan time. It then scores every idea with the outcome
tracker's own trade rules (ftse_outcomes.simulate): entry at the open of the
first 15-minute candle after the scan, stop counted first if stop and target
fall in the same candle, gaps through the stop fill at the worse price.

Costs per £100 trade: dealing 0.10% (10p) on every trade, stamp duty 0.5%
(50p) on buys of real shares.

Side-by-side variants (ideas register):
  * baseline            the live rules as they are
  * trend filter        buys only above the 50-day average close, sells only below (idea 1)
  * stop at least 1%    stop moved out to at least 1% from entry, target kept at 3x the stop distance (idea 8)
  * stop at least 2%    the same with 2%

Run on a machine that can reach Yahoo (e.g. the GitHub "FTSE Backtest" workflow):

    python ftse_backtest.py --out results/ftse_backtest.xlsx

Limits: Yahoo keeps about 60 days of 15-minute candles, so the earliest days
have less history for finding zones than the live scanner has (the first
--warmup-days are used as history only). Prices are Yahoo's, not a broker's.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import ftse_outcomes as out
import ftse_pullback as pb
import ftse_trade_scanner as sc
from ftse_sessions import is_session, session_open_close

FULL_SCAN_CLOCK = "10:00"
WATCH_LAST_CLOCK = "12:45"
FULL_DELAY = pd.Timedelta(minutes=3)    # live full scans finish about 3 minutes after the candle closes
WATCH_DELAY = pd.Timedelta(minutes=2)   # live watch scans finish about 1-2 minutes after
DEALING_PCT, STAMP_PCT = 0.10, 0.5
VARIANTS = ["Baseline (live rules)", "Trend filter (50-day average)", "Stop at least 1%", "Stop at least 2%",
            "Trend + 50-day slope", "Trend + slope + 20-EMA momentum"]
SLOPE_DAYS = 10      # the 50-day average must be higher (buys) or lower (sells) than it was this many trading days earlier
EMA_SPAN, EMA_LOOKBACK = 20, 4   # 20-period EMA on 15-minute candles, rising/falling over the last 4 candles (1 hour)
PULLBACK = "Trend pullback (new strategy)"
SCENARIOS = ["Every idea (pool)", "Top 2 at 10:30", "Live: first 3 triggers"]


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------

def download(universe, interval: str, period: str, delay: float):
    """Intraday and daily history for each share. Returns ({ticker: intraday}, {ticker: daily closes})."""
    intraday, daily = {}, {}
    for number, company in enumerate(universe, 1):
        try:
            raw = sc.fetch_data(company.yahoo_ticker, interval, period)
            intraday[company.yahoo_ticker] = sc.drop_untraded_bars(sc.completed_candles(raw, interval))
            days = sc.fetch_data(company.yahoo_ticker, "1d", "1y")
            daily[company.yahoo_ticker] = days["close"].astype(float)
        except Exception as error:  # a few tickers always fail; carry on without them
            print(f"  could not fetch {company.yahoo_ticker}: {error}")
        if number % 25 == 0:
            print(f"  ...{number}/{len(universe)} downloaded")
        time.sleep(delay)
    return intraday, daily


def sma_before(daily_closes: pd.Series, day: pd.Timestamp, length: int = 50) -> float | None:
    """Average of the `length` daily closes strictly before `day` (no peeking at that day's close)."""
    index = pd.DatetimeIndex(daily_closes.index)
    dates = index.tz_localize(None).normalize() if index.tz is not None else index.normalize()
    prior = daily_closes[dates < pd.Timestamp(day.date())].dropna()
    return float(prior.tail(length).mean()) if len(prior) >= length else None


def sma_slope_before(daily_closes: pd.Series, day: pd.Timestamp, length: int = 50, lookback: int = SLOPE_DAYS):
    """(average now, average `lookback` trading days earlier), both from closes strictly before `day`."""
    index = pd.DatetimeIndex(daily_closes.index)
    dates = index.tz_localize(None).normalize() if index.tz is not None else index.normalize()
    prior = daily_closes[dates < pd.Timestamp(day.date())].dropna()
    if len(prior) < length + lookback:
        return None, None
    return float(prior.tail(length).mean()), float(prior.iloc[:-lookback].tail(length).mean())


def ema_momentum(data: pd.DataFrame, last_bar_start: pd.Timestamp, span: int = EMA_SPAN, lookback: int = EMA_LOOKBACK):
    """(last close, EMA now, EMA `lookback` candles earlier) using only candles up to the signal candle."""
    closes = data.loc[data.index <= last_bar_start, "close"].astype(float)
    if len(closes) < span + lookback:
        return None, None, None
    ema = closes.ewm(span=span, adjust=False).mean()
    return float(closes.iloc[-1]), float(ema.iloc[-1]), float(ema.iloc[-1 - lookback])


# ----------------------------------------------------------------------
# Replaying scans
# ----------------------------------------------------------------------

@dataclass
class Scan:
    day: str
    cut: pd.Timestamp          # candles that closed by this time are visible
    available: pd.Timestamp    # when the scan's results would have existed
    mode: str


def scan_times(day: pd.Timestamp) -> list[Scan]:
    opening, _ = session_open_close(day)
    full_cut = pd.Timestamp(f"{day.date()} {FULL_SCAN_CLOCK}", tz=sc.UK_TZ)
    last = pd.Timestamp(f"{day.date()} {WATCH_LAST_CLOCK}", tz=sc.UK_TZ)
    scans = [Scan(str(day.date()), full_cut, full_cut + FULL_DELAY, "full")]
    cut = full_cut + pd.Timedelta(minutes=15)
    while cut <= last:
        scans.append(Scan(str(day.date()), cut, cut + WATCH_DELAY, "watch"))
        cut += pd.Timedelta(minutes=15)
    return scans


def setups_at(company, data: pd.DataFrame, cut: pd.Timestamp, interval_min: int) -> list:
    visible = data[data.index + pd.Timedelta(minutes=interval_min) <= cut]
    cfg = sc.INDEX_RULES[company.index]
    if len(visible) <= cfg.swing_lookback * 2:
        return []
    zones = sc.qualify_zones(sc.cluster_zones(sc.find_swing_points(visible, cfg.swing_lookback), cfg.zone_tolerance_pct), visible, cfg)
    setups = sc.build_setups(company.company, company.yahoo_ticker, company.index, visible, zones, cfg)
    closes = visible.index[-1] + pd.Timedelta(minutes=interval_min)
    for setup in setups:
        setup.kind, setup.last_trade = company.kind, closes
    return setups


def run_scan(scan: Scan, companies, intraday, interval_min: int, max_stale: int = 30):
    """One simulated scan: returns (run row, setup rows) in the live CSV formats."""
    found = []
    for company in companies:
        data = intraday.get(company.yahoo_ticker)
        if data is not None:
            found.extend(setups_at(company, data, scan.cut, interval_min))
    if not found:
        return None, []
    newest = max(s.last_trade for s in found)
    sc.mark_stale(found, newest, max_stale)
    selected = sc.dedupe_setups([s for s in found if not s.stale])
    sc.annotate_conflicts(selected)
    data_time = (newest - pd.Timedelta(minutes=interval_min)).isoformat()
    run_time = scan.available.strftime("%Y-%m-%d %H:%M:%S")
    as_of = newest.strftime("%H:%M")
    run = {"day": scan.day, "as_of": as_of, "data_time": data_time, "run_time": run_time, "mode": scan.mode,
           "scanned": len(companies), "failed": 0, "flagged": len(selected)}
    rows = [{"day": scan.day, "as_of": as_of, "data_time": data_time, "run_time": run_time, "mode": scan.mode,
             "company": s.company, "ticker": s.ticker, "index": s.index_label, "setup": s.setup_type,
             "direction": s.direction, "score": s.quality_score, "entry": round(s.entry, 4), "stop": round(s.stop, 4),
             "target": round(s.target, 4), "risk_pct": round(abs(s.entry - s.stop) / s.entry * 100, 4),
             "touches": s.zone.touch_count, "conflict": s.conflict} for s in selected]
    return run, rows


def replay(universe, intraday, interval_min: int, warmup_days: int, watch_top: int = 15):
    """All simulated runs and setups across the history, day by day."""
    all_days = sorted({stamp.normalize() for data in intraday.values() for stamp in data.index})
    sessions = [day for day in all_days if is_session(day)]
    runs, setups = [], []
    by_ticker = {c.yahoo_ticker: c for c in universe}
    for number, day in enumerate(sessions[warmup_days:], 1):
        watchlist = None
        for scan in scan_times(day):
            companies = universe if scan.mode == "full" else [by_ticker[t] for t in (watchlist or []) if t in by_ticker]
            if not companies:
                continue
            run, rows = run_scan(scan, companies, intraday, interval_min)
            if run is None:
                continue
            if scan.mode == "watch" and runs and runs[-1]["day"] == run["day"] and runs[-1]["data_time"] == run["data_time"]:
                continue            # same candle as the previous scan: the live watch records nothing
            runs.append(run)
            setups.extend(rows)
            if scan.mode == "full":
                ranked = sorted((r for r in rows if not r["conflict"]), key=lambda r: r["score"], reverse=True)
                watchlist = list(dict.fromkeys(r["ticker"] for r in ranked))[:watch_top]
        print(f"  replayed {day.date()} ({number}/{len(sessions) - warmup_days})")
    return pd.DataFrame(runs), pd.DataFrame(setups)


# ----------------------------------------------------------------------
# Choosing trades and scoring them
# ----------------------------------------------------------------------

def with_keys(setups: pd.DataFrame) -> pd.DataFrame:
    frame = setups.copy()
    frame["key"] = (frame["data_time"] + "|" + frame["ticker"] + "|" + frame["direction"] + "|"
                    + frame["run_time"] + "|" + frame["mode"])
    return frame


def live_picks(candidates: pd.DataFrame, per_day: int = 3) -> pd.DataFrame:
    """Same rule as ftse_live_mock: in scan order, best score first, up to 3 a day, one per share."""
    chosen = []
    for day, group in candidates.groupby("day"):
        taken = set()
        for _, scan in group.groupby("run_time", sort=True):
            for _, row in scan.sort_values("score", ascending=False).iterrows():
                if len(taken) >= per_day:
                    break
                if row["ticker"] in taken:
                    continue
                taken.add(row["ticker"])
                chosen.append(row)
    return pd.DataFrame(chosen)


def widen(frame: pd.DataFrame, min_pct: float) -> pd.DataFrame:
    """Move each stop out to at least min_pct from entry, keeping the target at 3x the stop distance."""
    result = frame.copy()
    risk = (result["entry"] - result["stop"]).abs()
    wide = (result["entry"] * min_pct / 100).where(risk < result["entry"] * min_pct / 100, risk)
    long = result["direction"] == "Long"
    result["stop"] = (result["entry"] - wide).where(long, result["entry"] + wide)
    result["target"] = (result["entry"] + 3 * wide).where(long, result["entry"] - 3 * wide)
    return result


def score(frame: pd.DataFrame, intraday, horizon: int, now: pd.Timestamp, interval: str) -> pd.DataFrame:
    rows = []
    minutes = sc.interval_minutes(interval)
    for _, row in frame.iterrows():
        data = intraday.get(row["ticker"])
        if data is None:
            continue
        signal_close = out.market_stamp(row["data_time"]) + pd.Timedelta(minutes=minutes)
        available = max(signal_close, out.market_stamp(row["run_time"]))
        sim = out.simulate(row["direction"], float(row["stop"]), float(row["target"]), available, data, horizon, now, interval)
        rows.append({**row.to_dict(), "status": sim.status, "outcome": sim.outcome, "fill": sim.fill,
                     "exit_price": sim.exit_price, "exit_time": sim.exit_time, "r_gross": sim.r_gross,
                     "fill_risk_pct": sim.fill_risk_pct})
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    done = result["status"] == "done"
    gross = (result["r_gross"] * result["fill_risk_pct"]).where(done)          # pounds per £100
    stamp = (result["direction"] == "Long").astype(float) * STAMP_PCT
    result["gross_gbp"] = gross.round(2)
    result["costs_gbp"] = (DEALING_PCT + stamp).where(done).round(2)
    result["net_gbp"] = (gross - DEALING_PCT - stamp).where(done).round(2)
    return result


def summary_row(variant: str, scenario: str, trades: pd.DataFrame) -> dict:
    done = trades[trades["status"] == "done"] if not trades.empty else trades
    n = len(done)
    if n == 0:
        return {"Variant": variant, "Scenario": scenario, "Completed trades": 0}
    by_day = done.groupby("day")["net_gbp"].sum()
    return {
        "Variant": variant, "Scenario": scenario, "Completed trades": n, "Days traded": int(by_day.size),
        "Target hit": (done["outcome"] == "target").mean(), "Stop hit": (done["outcome"] == "stop").mean(),
        "Closed at end": (done["outcome"] == "timeout").mean(),
        "Avg before costs per £100 (£)": done["gross_gbp"].mean(), "Avg after costs per £100 (£)": done["net_gbp"].mean(),
        "Total after costs (£)": done["net_gbp"].sum(), "Profitable days": (by_day > 0).mean(),
        "Worst day (£)": by_day.min(), "Best day (£)": by_day.max(),
        "Buys: avg after costs (£)": done.loc[done["direction"] == "Long", "net_gbp"].mean(),
        "Sells: avg after costs (£)": done.loc[done["direction"] == "Short", "net_gbp"].mean(),
        "Skipped (gapped past level)": int((trades["status"] == "skipped").sum()),
        "Not resolved (missing data)": int(trades["status"].isin(["nodata", "pending"]).sum()),
    }


# ----------------------------------------------------------------------
# Workbook
# ----------------------------------------------------------------------

HEAD_FILL, GOOD, BAD, NOTE = "1F3B57", "D9EAD3", "F4CCCC", "EEF2F6"
MONEY = '"£"#,##0.00;[Red]-"£"#,##0.00;"£"0.00'


def write_table(ws, start_row: int, frame: pd.DataFrame, money_cols=(), pct_cols=()) -> int:
    for column, name in enumerate(frame.columns, 1):
        cell = ws.cell(start_row, column, name)
        cell.font, cell.fill = Font(name="Arial", bold=True, color="FFFFFF"), PatternFill("solid", fgColor=HEAD_FILL)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.row_dimensions[start_row].height = 45
    for r, (_, row) in enumerate(frame.iterrows(), start_row + 1):
        for column, name in enumerate(frame.columns, 1):
            value = row[name]
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                value = None
            cell = ws.cell(r, column, value)
            cell.font = Font(name="Arial")
            if name in money_cols and isinstance(value, (int, float)):
                cell.number_format = MONEY
            if name in pct_cols and isinstance(value, (int, float)):
                cell.number_format = "0%"
    return start_row + len(frame) + 1


def note(ws, row: int, text: str, last_col: int = 12) -> None:
    ws.cell(row, 1, text).font = Font(name="Arial")
    ws.cell(row, 1).fill = PatternFill("solid", fgColor=NOTE)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=last_col)
    ws.cell(row, 1).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[row].height = 15 * max(1, math.ceil(len(text) / 150))


def build_workbook(summary: pd.DataFrame, daily: pd.DataFrame, trades: pd.DataFrame, period_text: str, generated: str,
                   pullback_trades: pd.DataFrame | None = None) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.cell(1, 1, "FTSE scanner backtest: how the rules would have done").font = Font(name="Arial", bold=True, size=13)
    note(ws, 2, f"Generated {generated}. History replayed: {period_text}. Each scan used only the candles finished by its time. "
                "Every trade is £100, entered at the open of the next 15-minute candle after the scan, stop counted first if stop "
                "and target fall in one candle. Costs: 10p dealing per trade, plus 50p stamp duty on buys of real shares.")
    note(ws, 3, "Variants: Baseline = the live rules. Trend filter = buys only when the price is above its 50-day average close, sells "
                "only below. Stop at least 1% / 2% = stops moved further away (target still 3x the stop distance). Trend + 50-day slope = "
                "as Trend filter, and the 50-day average must also be rising for buys (falling for sells) compared with 10 trading days "
                "earlier. Trend + slope + 20-EMA momentum = as that, and on the 15-minute chart the price must be above a rising "
                "20-period average for buys (below a falling one for sells) at the signal. Scenarios: Every "
                "idea = all ideas at first sighting; Top 2 at 10:30 = your main mock trader; Live: first 3 triggers = the live mock trader.")
    note(ws, 5, "Trend pullback (new strategy, idea 17): a different engine, not a filter. Daily trend: price above a rising 50-day "
                "average (sells: below a falling one). On 15-minute candles the 20-period average must be rising with the 9-period above "
                "it; the price dips to the 20-period average and the next candle closes back above the 9-period. Stop below the dip, "
                "target 2x the stop distance, one trade per share per day, signals 09:00-15:30.")
    note(ws, 4, "Caution: results from the past can flatter a model, especially after tweaking. Use this to choose what to test, then "
                "judge it on new live results. The earliest days in the history had less data for finding zones than the live scanner has.")
    money = [c for c in summary.columns if "(£)" in c]
    pct = ["Target hit", "Stop hit", "Closed at end", "Profitable days"]
    end = write_table(ws, 7, summary, money, pct)
    for r in range(8, end):
        value = ws.cell(r, summary.columns.get_loc("Avg after costs per £100 (£)") + 1).value
        if isinstance(value, (int, float)):
            ws.cell(r, summary.columns.get_loc("Avg after costs per £100 (£)") + 1).fill = PatternFill("solid", fgColor=GOOD if value > 0 else BAD)
    for column, width in enumerate([30, 24, 11, 9, 9, 9, 10, 13, 13, 13, 10, 11, 11, 13, 13, 12, 12], 1):
        ws.column_dimensions[get_column_letter(column)].width = width
    ws.freeze_panes = "C8"

    days_ws = wb.create_sheet("Daily")
    note(days_ws, 1, "Profit or loss after costs for each day, per variant, for 'Every idea' and 'Top 2 at 10:30'. Look for whether losses "
                     "cluster on particular days (market-wide falls) or are spread evenly.", last_col=10)
    write_table(days_ws, 3, daily, [c for c in daily.columns if "(£)" in c])
    for column in range(1, len(daily.columns) + 1):
        days_ws.column_dimensions[get_column_letter(column)].width = 14 if column > 1 else 12
    days_ws.freeze_panes = "B4"

    def trade_sheet(title: str, frame: pd.DataFrame) -> None:
        trade_ws = wb.create_sheet(title)
        if frame is None or frame.empty:
            trade_ws.cell(1, 1, "No trades.")
            return
        columns = ["day", "run_time", "mode", "scenario", "company", "ticker", "direction", "setup", "score", "fill", "stop",
                   "target", "outcome", "exit_time", "gross_gbp", "costs_gbp", "net_gbp"]
        shown = frame[[c for c in columns if c in frame.columns]].rename(columns={
            "day": "Date", "run_time": "Signal time", "mode": "Scan", "scenario": "Scenario", "company": "Company",
            "ticker": "Ticker", "direction": "Direction", "setup": "Setup", "score": "Score", "fill": "Entry (filled)",
            "stop": "Stop", "target": "Target", "outcome": "Result", "exit_time": "Exit time",
            "gross_gbp": "Before costs (£)", "costs_gbp": "Costs (£)", "net_gbp": "After costs (£)"})
        write_table(trade_ws, 1, shown, ["Before costs (£)", "Costs (£)", "After costs (£)"])
        trade_ws.freeze_panes = "A2"

    trade_sheet("Trades (baseline)", trades)
    if pullback_trades is not None:
        trade_sheet("Trades (trend pullback)", pullback_trades)
    return wb


# ----------------------------------------------------------------------

def run(args) -> int:
    universe = sc.load_universe(Path(args.universe))
    if args.exclude_trusts:
        universe = [c for c in universe if c.kind != "trust"]
    if args.limit:
        universe = universe[:args.limit]
    minutes = sc.interval_minutes(args.interval)
    print(f"Downloading {len(universe)} shares ({args.period} of {args.interval} candles plus daily closes)...")
    intraday, daily = download(universe, args.interval, args.period, args.delay)
    if not intraday:
        print("No data downloaded (is Yahoo reachable?).")
        return 1
    universe = [c for c in universe if c.yahoo_ticker in intraday]
    print("Replaying scans day by day...")
    runs, setups = replay(universe, intraday, minutes, args.warmup_days)
    if setups.empty:
        print("No setups found in the replayed period.")
        return 1
    now = max(data.index[-1] for data in intraday.values()) + pd.Timedelta(minutes=minutes)
    candidates = with_keys(setups)
    candidates = candidates[(~candidates["conflict"]) & (candidates["run_time"].str[11:16] <= "15:30")]

    def choose(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
        picks = out.build_picks(frame, args.pick_time, runs)
        return {"Every idea (pool)": out.first_sightings(frame), "Top 2 at 10:30": picks["Top 2 per day"],
                "Live: first 3 triggers": live_picks(frame)}

    trend_ok = []
    for _, row in candidates.iterrows():
        closes = daily.get(row["ticker"])
        average = sma_before(closes, pd.Timestamp(row["day"]), 50) if closes is not None else None
        trend_ok.append(average is not None and ((row["direction"] == "Long" and row["entry"] > average)
                                                 or (row["direction"] == "Short" and row["entry"] < average)))
    trend = candidates[pd.Series(trend_ok, index=candidates.index)]

    slope_ok, momentum_ok = [], []
    for _, row in candidates.iterrows():
        closes = daily.get(row["ticker"])
        sma_now, sma_then = sma_slope_before(closes, pd.Timestamp(row["day"])) if closes is not None else (None, None)
        long = row["direction"] == "Long"
        ok = sma_now is not None and ((long and row["entry"] > sma_now and sma_now > sma_then)
                                      or (not long and row["entry"] < sma_now and sma_now < sma_then))
        slope_ok.append(ok)
        if not ok:
            momentum_ok.append(False)
            continue
        price, ema_now, ema_then = ema_momentum(intraday[row["ticker"]], out.market_stamp(row["data_time"]))
        momentum_ok.append(price is not None and ((long and price > ema_now and ema_now > ema_then)
                                                  or (not long and price < ema_now and ema_now < ema_then)))
    slope = candidates[pd.Series(slope_ok, index=candidates.index)]
    momentum = candidates[pd.Series(momentum_ok, index=candidates.index)]

    variant_sets = {VARIANTS[0]: (candidates, None), VARIANTS[1]: (trend, None),
                    VARIANTS[2]: (candidates, 1.0), VARIANTS[3]: (candidates, 2.0),
                    VARIANTS[4]: (slope, None), VARIANTS[5]: (momentum, None)}
    summary, daily_cols, baseline_trades = [], {}, []
    for variant, (frame, min_stop) in variant_sets.items():
        print(f"Scoring: {variant}...")
        for scenario, chosen in choose(frame).items():
            if chosen.empty:
                summary.append({"Variant": variant, "Scenario": scenario, "Completed trades": 0})
                continue
            levels = widen(chosen, min_stop) if min_stop else chosen
            trades = score(levels, intraday, args.horizon, now, args.interval)
            summary.append(summary_row(variant, scenario, trades))
            if scenario != "Live: first 3 triggers" and not trades.empty:
                done = trades[trades["status"] == "done"]
                daily_cols[f"{variant}: {scenario} (£)"] = done.groupby("day")["net_gbp"].sum()
            if variant == VARIANTS[0] and not trades.empty:
                baseline_trades.append(trades.assign(scenario=scenario))
    print("Scoring: trend pullback (new strategy)...")
    replay_days = set(setups["day"])
    pull_rows = []
    for company in universe:
        pull_rows.extend(pb.signals(company, intraday.get(company.yahoo_ticker), daily.get(company.yahoo_ticker),
                                    minutes, replay_days))
    pullback_trades = []
    pull = with_keys(pd.DataFrame(pull_rows)) if pull_rows else pd.DataFrame()
    pull_sets = {"Every signal": pull,
                 "First 3 per day": live_picks(pull) if not pull.empty else pull,
                 "First 3 per day, buys only": live_picks(pull[pull["direction"] == "Long"]) if not pull.empty else pull}
    for scenario, chosen in pull_sets.items():
        if chosen.empty:
            summary.append({"Variant": PULLBACK, "Scenario": scenario, "Completed trades": 0})
            continue
        trades = score(chosen, intraday, args.horizon, now, args.interval)
        summary.append(summary_row(PULLBACK, scenario, trades))
        if not trades.empty:
            done = trades[trades["status"] == "done"]
            daily_cols[f"{PULLBACK}: {scenario} (£)"] = done.groupby("day")["net_gbp"].sum()
            pullback_trades.append(trades.assign(scenario=scenario))
    summary_frame = pd.DataFrame(summary)
    daily_frame = pd.DataFrame(daily_cols).sort_index().round(2)
    daily_frame.index.name = "Date"
    daily_frame = daily_frame.reset_index()
    trades_frame = pd.concat(baseline_trades, ignore_index=True) if baseline_trades else pd.DataFrame()
    days = sorted(setups["day"].unique())
    period_text = (f"{days[0]} to {days[-1]} ({len(days)} trading days, {len(universe)} shares); each trade held up to "
                   f"{args.horizon} trading day(s), then closed at the last price if neither stop nor target was hit")
    generated = datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M UK time")
    pull_frame = pd.concat(pullback_trades, ignore_index=True) if pullback_trades else pd.DataFrame()
    workbook = build_workbook(summary_frame, daily_frame, trades_frame, period_text, generated, pull_frame)
    saved = sc.save_workbook(workbook, Path(args.out))
    print(f"\nReplayed {period_text}: {len(runs)} scans, {len(setups)} setups.")
    print(summary_frame[["Variant", "Scenario", "Completed trades", "Avg after costs per £100 (£)", "Total after costs (£)"]]
          .to_string(index=False))
    print(f"\nWorkbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay the FTSE scanner over recent history and score the trades")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--period", default="60d")
    parser.add_argument("--warmup-days", type=int, default=15,
                        help="Trading days at the start used only as history for finding zones (default: 15)")
    parser.add_argument("--horizon", type=int, default=1, help="Holding period in trading days (default: 1 = same day)")
    parser.add_argument("--pick-time", default="10:30")
    parser.add_argument("--exclude-trusts", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Only the first N shares (for a quick test)")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--out", default="results/ftse_backtest.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
