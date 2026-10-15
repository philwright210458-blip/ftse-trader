"""
FTSE Scanner Outcome Tracker and Paper-Trading Simulator
=========================================================
Looks back at every setup the scanner has logged (scan_data/setups.csv), works
out what the market did next, and builds a workbook that answers one question:
would following this model have made money?

What it does
  1. Walks each logged setup forward through the candles that followed it:
     did price reach the target or the stop first, or neither?
  2. Imagines a trader who takes the top 1 or 2 ideas each day and shows how
     those paper trades performed over time (equity curve, drawdown).
  3. Compares those picks with the average of EVERY idea the scanner flagged
     (a random pick). If the top-ranked ideas are not better than that
     average, the ranking is not adding value.

Run it once a day after the close (Yahoo only keeps about 60 days of 15-minute
candles, so results are cached in scan_data/outcomes.csv as they are resolved):

    python ftse_outcomes.py

Assumptions (also written into the workbook's Summary sheet)
  * You act on the scan, filling at the OPEN of the next 15-minute candle.
  * If the stop and target both fall inside one candle, the stop is assumed
    to have been hit first (pessimistic).
  * Gaps through a stop fill at the open, so a loss can exceed 1R.
  * Setups that have already gapped past their stop or target at the fill are
    skipped, not counted as trades.
  * Trading costs are applied when the report is built, so you can change them
    without re-downloading anything.
"""

import argparse
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import ftse_trade_scanner as sc

SCRIPT_DIR = Path(__file__).parent
DEFAULT_OUT = SCRIPT_DIR / "ftse_outcomes.xlsx"
OUTCOME_COLUMNS = ["key", "day", "ticker", "direction", "horizon", "status", "outcome", "fill",
                   "exit_price", "exit_time", "r_gross", "fill_risk_pct", "ambiguous"]
PICK_SCENARIOS = ["Top 1 per day", "Top 2 per day", "Top 2 per day, persistent"]
POOL = "Pool average (every first sighting)"


# ----------------------------------------------------------------------
# Simulating one setup
# ----------------------------------------------------------------------

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


MAX_FILL_DELAY = pd.Timedelta(minutes=30)   # no trade within this long of the signal = not filled


def simulate(side: str, stop: float, target: float, signal_close: pd.Timestamp,
             data: pd.DataFrame, horizon: int, now: pd.Timestamp) -> Sim:
    """Walk one setup forward. `signal_close` is when the signal candle closed;
    the fill is the open of the first candle that starts at or after it."""
    after = data[data.index >= signal_close]
    if after.empty:
        return Sim("pending") if (now - signal_close) < pd.Timedelta(days=4) else Sim("nodata", "no_candle")
    signal_day = signal_close.tz_convert(sc.UK_TZ).date()
    if after.index[0].date() != signal_day or after.index[0] - signal_close > MAX_FILL_DELAY:
        return Sim("skipped", "no_candle")          # a thin share that didn't trade again soon: you couldn't have filled
    days = sorted(set(after.index.date))
    if len(days) < horizon:
        stuck = (now - signal_close) > pd.Timedelta(days=horizon * 3 + 4)
        return Sim("nodata", "no_candle") if stuck else Sim("pending")
    horizon_day = days[horizon - 1]
    last_start = after.index[after.index.date == horizon_day][-1]
    day_over = (len(days) > horizon or last_start.time() >= dtime(16, 0)
                or now >= pd.Timestamp(f"{horizon_day} 16:45", tz=sc.UK_TZ))
    if not day_over:
        return Sim("pending")

    path = after[after.index.date <= horizon_day]
    fill = float(path["open"].iloc[0])
    if (side == "Long" and (fill <= stop or fill >= target)) or (side == "Short" and (fill >= stop or fill <= target)):
        return Sim("skipped", "gap", fill=fill)
    risk = abs(fill - stop)
    risk_pct = round(risk / fill * 100, 3)

    def r_multiple(exit_price: float) -> float:
        return round(((exit_price - fill) if side == "Long" else (fill - exit_price)) / risk, 3)

    ambiguous = False
    for stamp, row in path.iterrows():
        o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
        hit_stop, hit_target = (l <= stop, h >= target) if side == "Long" else (h >= stop, l <= target)
        if hit_stop and hit_target:
            ambiguous, hit_target = True, False     # cannot know the order: assume the stop came first
        if hit_stop:
            exit_price = min(o, stop) if side == "Long" else max(o, stop)
            return Sim("done", "stop", fill, exit_price, stamp.isoformat(), r_multiple(exit_price), risk_pct, ambiguous)
        if hit_target:
            exit_price = max(o, target) if side == "Long" else min(o, target)
            return Sim("done", "target", fill, exit_price, stamp.isoformat(), r_multiple(exit_price), risk_pct, ambiguous)
    close = float(path["close"].iloc[-1])
    return Sim("done", "timeout", fill, close, path.index[-1].isoformat(), r_multiple(close), risk_pct, False)


# ----------------------------------------------------------------------
# Which setups to evaluate
# ----------------------------------------------------------------------

def load_candidates(data_dir: Path, last_entry: str) -> pd.DataFrame:
    """Every logged, non-conflicting setup that was early enough in the day to act on."""
    setups = sc._read_table(data_dir / "setups.csv", sc.SETUP_COLUMNS)
    if setups.empty:
        return setups
    keep = setups[(~setups["conflict"]) & (setups["as_of"] <= last_entry)].copy()
    # the same candle can be logged by a full scan and a watch scan: keep one, preferring the full scan
    keep["_order"] = (keep["mode"] != "full").astype(int)
    keep = keep.sort_values(["data_time", "ticker", "direction", "_order"])
    keep = keep.drop_duplicates(["data_time", "ticker", "direction"]).drop(columns="_order")
    keep["key"] = keep["data_time"] + "|" + keep["ticker"] + "|" + keep["direction"]
    return keep.reset_index(drop=True)


def first_sightings(candidates: pd.DataFrame) -> pd.DataFrame:
    """The first time each idea (share + direction) appeared on each day."""
    ordered = candidates.sort_values(["day", "as_of", "score"], ascending=[True, True, False])
    return ordered.drop_duplicates(["day", "ticker", "direction"], keep="first")


def build_picks(candidates: pd.DataFrame, pick_time: str) -> dict[str, pd.DataFrame]:
    """The ideas a trader taking 1-2 trades a day would have chosen.

    Each day the trader looks at the first scan at or after `pick_time`, takes
    the highest-scoring ideas, one per share. 'Persistent' additionally needs the
    idea to have been flagged in at least two scans that day up to that moment
    (no peeking at later scans)."""
    picks = {name: [] for name in PICK_SCENARIOS}
    for day, group in candidates.groupby("day"):
        scans = [a for a in sorted(set(group["as_of"])) if a >= pick_time]
        if not scans:
            continue
        pick_scan = scans[0]
        seen = group[group["as_of"] <= pick_scan].groupby(["ticker", "direction"])["as_of"].nunique()
        here = group[group["as_of"] == pick_scan].sort_values("score", ascending=False)
        here = here.assign(seen=[int(seen.get((t, d), 1)) for t, d in zip(here["ticker"], here["direction"])])
        here = here.drop_duplicates("ticker")
        for name, chosen in (("Top 1 per day", here.head(1)),
                             ("Top 2 per day", here.head(2)),
                             ("Top 2 per day, persistent", here[here["seen"] >= 2].head(2))):
            if not chosen.empty:
                picks[name].append(chosen.assign(rank=range(1, len(chosen) + 1)))
    return {name: (pd.concat(parts) if parts else pd.DataFrame()) for name, parts in picks.items()}


# ----------------------------------------------------------------------
# Outcome cache
# ----------------------------------------------------------------------

def read_outcomes(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=OUTCOME_COLUMNS)
    table = pd.read_csv(path, dtype=str, keep_default_na=False)
    for column in ("fill", "exit_price", "r_gross", "fill_risk_pct"):
        table[column] = pd.to_numeric(table[column], errors="coerce")
    table["horizon"] = table["horizon"].astype(int)
    table["ambiguous"] = table["ambiguous"].str.lower().eq("true")
    return table


def evaluate_pending(rows: pd.DataFrame, cache: pd.DataFrame, horizons: list[int], interval: str,
                     period: str, delay: float, out_path: Path) -> tuple[int, int, int]:
    """Resolve whatever is not already cached. Returns (newly resolved, still pending, failed fetches)."""
    minutes = sc.interval_minutes(interval) or 15
    done = set(zip(cache["key"], cache["horizon"]))
    work: dict[str, list] = {}
    for _, row in rows.iterrows():
        for horizon in horizons:
            if (row["key"], horizon) not in done:
                work.setdefault(row["ticker"], []).append((row, horizon))
    now = pd.Timestamp.now(tz=sc.UK_TZ)
    new_rows, pending, failed = [], 0, 0
    total = len(work)
    for number, (ticker, items) in enumerate(work.items(), 1):
        try:
            data = sc.drop_untraded_bars(sc.fetch_data(ticker, interval, period))
        except Exception as error:
            failed += 1
            print(f"  could not fetch {ticker}: {error}")
            continue
        for row, horizon in items:
            signal_close = pd.Timestamp(row["data_time"]) + pd.Timedelta(minutes=minutes)
            sim = simulate(row["direction"], row["stop"], row["target"], signal_close, data, horizon, now)
            if sim.status == "pending":
                pending += 1
                continue
            new_rows.append({"key": row["key"], "day": row["day"], "ticker": ticker, "direction": row["direction"],
                             "horizon": horizon, "status": sim.status, "outcome": sim.outcome, "fill": sim.fill,
                             "exit_price": sim.exit_price, "exit_time": sim.exit_time, "r_gross": sim.r_gross,
                             "fill_risk_pct": sim.fill_risk_pct, "ambiguous": sim.ambiguous})
        if number % 25 == 0:
            print(f"  ...{number}/{total} shares checked")
        time.sleep(delay)
    if new_rows:
        sc._append_rows(out_path, OUTCOME_COLUMNS, new_rows)
    return len(new_rows), pending, failed


# ----------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------

def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def add_costs(frame: pd.DataFrame, cost_pct: float, stamp_pct: float) -> pd.DataFrame:
    frame = frame.copy()
    frame["cost_r"] = cost_pct / frame["fill_risk_pct"]
    frame["stamp_r"] = (stamp_pct / frame["fill_risk_pct"]).where(frame["direction"] == "Long", 0.0)
    frame["r_net"] = frame["r_gross"] - frame["cost_r"]
    frame["r_net_stamp"] = frame["r_net"] - frame["stamp_r"]
    return frame


def drawdown(values: list[float]) -> float:
    peak, worst, total = 0.0, 0.0, 0.0
    for value in values:
        total += value
        peak = max(peak, total)
        worst = min(worst, total - peak)
    return worst


def summarise(trades: pd.DataFrame) -> dict:
    """Headline numbers for a set of completed trades (already ordered by time)."""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    targets = int((trades["outcome"] == "target").sum())
    stops = int((trades["outcome"] == "stop").sum())
    low, high = wilson(targets, n)
    return {
        "n": n, "target": targets / n, "stop": stops / n, "timeout": 1 - (targets + stops) / n,
        "target_ci": (low, high), "gross": trades["r_gross"].mean(), "net": trades["r_net"].mean(),
        "net_stamp": trades["r_net_stamp"].mean(), "total_net": trades["r_net"].sum(),
        "drawdown": drawdown(trades["r_net"].tolist()),
        "breakeven": (1 + trades["cost_r"].mean()) / 4,     # with a 3R target and a 1R stop
        "ambiguous": trades["ambiguous"].mean(),
        "net_ci": (1.96 * trades["r_net"].std() / math.sqrt(n)) if n > 1 else float("nan"),
    }


def reliability(n: int) -> str:
    return ("too few to read" if n < 30 else "weak" if n < 100 else "indicative only" if n < 300 else "reasonable")


# ----------------------------------------------------------------------
# Workbook
# ----------------------------------------------------------------------

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
GOOD, BAD, NEUTRAL = "C6EFCE", "FFC7CE", "FFF2CC"


def header(ws, row: int, headers: list[str]) -> None:
    for column, text in enumerate(headers, 1):
        cell = ws.cell(row, column, text)
        cell.font, cell.fill = Font(name="Arial", bold=True, color="FFFFFF"), HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True, vertical="center")


def put(ws, row: int, values: list, fill: str | None = None, bold: bool = False) -> None:
    for column, value in enumerate(values, 1):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            value = None
        cell = ws.cell(row, column, value)
        cell.font = Font(name="Arial", bold=bold)
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)


def note_row(ws, row: int, text: str, last_col: int = 14, chars_per_line: int = 200,
             fill: str | None = None, bold: bool = False) -> None:
    """A long sentence merged across the table width and wrapped, so it reads without sideways scrolling."""
    put(ws, row, [text], fill=fill, bold=bold)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=last_col)
    ws.cell(row, 1).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[row].height = 15 * max(1, math.ceil(len(text) / chars_per_line))


def widths(ws, sizes: list[int]) -> None:
    for column, size in enumerate(sizes, 1):
        ws.column_dimensions[get_column_letter(column)].width = size


def pct(x: float | None) -> str:
    return "" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.0f}%"


def r_fill(value: float | None) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return GOOD if value > 0 else BAD


def build_report(args, candidates: pd.DataFrame, pool: pd.DataFrame, picks: dict[str, pd.DataFrame],
                 outcomes: pd.DataFrame, kinds: dict[str, str], generated: str) -> Workbook:
    horizons = [int(h) for h in args.horizons.split(",")]
    main_h = args.horizon
    wb = Workbook()
    summary, breakdown, mock, daily, allout = wb.active, *(wb.create_sheet(n) for n in ("Breakdowns", "Mock Trades", "Daily", "All Outcomes"))
    summary.title = "Summary"

    def resolved(keys: pd.Series, horizon: int) -> pd.DataFrame:
        part = outcomes[(outcomes["horizon"] == horizon) & (outcomes["key"].isin(set(keys)))]
        return part

    def trades_for(frame: pd.DataFrame, horizon: int) -> pd.DataFrame:
        """Completed trades for a set of setups, with costs, in time order."""
        joined = frame.merge(outcomes[outcomes["horizon"] == horizon], on="key", suffixes=("", "_o"))
        joined = joined[joined["status"] == "done"]
        if joined.empty:
            return joined
        joined = add_costs(joined, args.cost_pct, args.stamp_duty_pct)
        sort_cols = ["day", "as_of"] + (["rank"] if "rank" in joined.columns else [])
        return joined.sort_values(sort_cols).reset_index(drop=True)

    scenarios = {name: picks[name] for name in PICK_SCENARIOS}
    scenarios[POOL] = pool

    # ---------------- Summary ----------------
    days = sorted(set(candidates["day"])) if not candidates.empty else []
    put(summary, 1, ["FTSE scanner outcome tracker"], bold=True)
    note_row(summary, 2, f"Generated {generated}. Scanning days with history: {len(days)}"
                         f" ({days[0]} to {days[-1]})" if days else f"Generated {generated}. No scan history yet.")
    pool_n = len(trades_for(pool, 1)) if not pool.empty else 0
    note_row(summary, 3, f"Sample size: {pool_n} completed independent ideas so far. Win rates need a few hundred before they "
                         f"mean much; with fewer than ~30, treat every number here as noise.",
             fill=NEUTRAL if pool_n < 100 else None, bold=True)
    note_row(summary, 4, f"1R = the money you lose if the stop is hit. Each trade is filled at the next candle's open, costs of "
                     f"{args.cost_pct:.2f}% round trip are taken off, and if stop and target fall in one candle the stop counts first. "
                     f"Stamp duty ({args.stamp_duty_pct:.2f}% on long entries) is shown separately: it applies to real shares, not spread bets or CFDs.")
    note_row(summary, 5, f"Mock trader: takes the top 1 or 2 ideas from the first scan at or after {args.pick_time}. "
                     f"'Pool average' = every idea at its first sighting of the day (what a random pick earns). "
                     f"If the picks don't beat the pool, the ranking adds nothing.")
    head = ["Scenario", "Hold (days)", "Trades", "Skipped", "Not resolved yet", "Target hit", "Stop hit", "Timed out",
            "Avg R after costs", "Avg R incl. stamp duty (longs)", "+/- (95%)", "Total R after costs",
            "Worst drawdown (R)", "Break-even target-hit rate"]
    header(summary, 7, head)
    summary.row_dimensions[7].height = 45
    row = 8
    for name, frame in scenarios.items():
        if frame.empty:
            continue
        for horizon in horizons:
            trades = trades_for(frame, horizon)
            allrows = resolved(frame["key"], horizon)
            skipped = int((allrows["status"] == "skipped").sum())
            unresolved = len(frame) - len(allrows)
            stats = summarise(trades)
            if stats["n"] == 0:
                put(summary, row, [name, horizon, 0, skipped, unresolved])
            else:
                put(summary, row, [name, horizon, stats["n"], skipped, unresolved, pct(stats["target"]),
                                   pct(stats["stop"]), pct(stats["timeout"]), round(stats["net"], 3),
                                   round(stats["net_stamp"], 3), round(stats["net_ci"], 3), round(stats["total_net"], 2),
                                   round(stats["drawdown"], 2), pct(stats["breakeven"])])
                for column in (9, 10):
                    value = summary.cell(row, column).value
                    fill = r_fill(value)
                    if fill:
                        summary.cell(row, column).fill = PatternFill("solid", fgColor=fill)
            for column in range(2, len(head) + 1):
                summary.cell(row, column).alignment = Alignment(horizontal="center")
            row += 1
    row += 1
    for line in ("How to read this:",
                 "- 'Avg R after costs' is the number that matters. Above zero, with the +/- not reaching below zero, is the bar.",
                 "- 'Break-even target-hit rate': the share of trades that must hit the target to make money after costs.",
                 "- Compare the top picks with the Pool average row. Prices come from Yahoo and may be delayed. Not trading advice."):
        note_row(summary, row, line, bold=line.endswith(":"))
        row += 1
    widths(summary, [36, 10, 9, 9, 11, 10, 9, 10, 12, 16, 10, 12, 12, 14])
    summary.freeze_panes = "C8"

    # ---------------- Breakdowns ----------------
    base = trades_for(pool, main_h) if not pool.empty else pd.DataFrame()
    note_row(breakdown, 1, f"Every first-sighting idea, held up to {main_h} trading day(s), after costs. Slices with fewer than 30 trades are noise.", last_col=8, chars_per_line=110, bold=True)
    row = 3
    if not base.empty:
        base = base.assign(
            kind=base["ticker"].map(lambda t: "Trust" if kinds.get(t) == "trust" else "Company"),
            score_band=pd.cut(base["score"], [0, 50, 60, 70, 80, 90, 101], right=False,
                              labels=["under 50", "50-60", "60-70", "70-80", "80-90", "90+"]),
            touch_band=pd.cut(base["touches"], [0, 5, 7, 10, 10000], right=False, labels=["3-4", "5-6", "7-9", "10+"]))
        for title, column in (("By quality score", "score_band"), ("By type", "kind"), ("By setup", "setup"),
                              ("By direction", "direction"), ("By index", "index"), ("By meaningful touches", "touch_band")):
            put(breakdown, row, [title], bold=True)
            header(breakdown, row + 1, [column.replace("_", " ").title(), "Trades", "Target hit", "Stop hit",
                                         "Timed out", "Avg R before costs", "Avg R after costs", "Reliability"])
            row += 2
            for label, group in base.groupby(column, observed=True):
                stats = summarise(group)
                put(breakdown, row, [str(label), stats["n"], pct(stats["target"]), pct(stats["stop"]), pct(stats["timeout"]),
                                     round(stats["gross"], 3), round(stats["net"], 3), reliability(stats["n"])])
                fill = r_fill(stats["net"])
                if fill and stats["n"] >= 30:
                    breakdown.cell(row, 7).fill = PatternFill("solid", fgColor=fill)
                for column in range(2, 9):
                    breakdown.cell(row, column).alignment = Alignment(horizontal="center")
                row += 1
            row += 1
    else:
        put(breakdown, row, ["No completed trades yet."])
    widths(breakdown, [24, 9, 11, 10, 11, 16, 16, 18])

    # ---------------- Mock trades ----------------
    top2 = picks["Top 2 per day"]
    note_row(mock, 1, f"Each day the mock trader takes the top 2 ideas from the first scan at or after {args.pick_time}. "
                      f"Each mock trade is £{args.position_gbp:.0f} of shares bought (or sold short). "
                      f"Dealing costs = {args.cost_pct:.2f}% of the position (buy and sell together). Stamp duty = {args.stamp_duty_pct:.2f}% of the position "
                      f"on long trades in real shares (spread bets and CFDs don't pay it, and shorts never do). "
                      f"Grey rows were never entered (price had already passed the stop or target, or no trade within 30 min).",
             last_col=18, chars_per_line=190, fill=NEUTRAL)
    header(mock, 2, ["Date", "Scan time", "Rank", "Company", "Ticker", "Direction", "Setup", "Entry", "Stop", "Target",
                     "Result", "Exit time", "Amount traded (£)", "Gross profit / loss (£)", "Dealing costs (£)",
                     "Stamp duty (£)", "Net after all fees (£)", "Running net (£)"])
    mock.row_dimensions[2].height = 45
    if not top2.empty:
        joined = top2.merge(outcomes[outcomes["horizon"] == main_h], on="key", how="left", suffixes=("", "_o"))
        joined = add_costs(joined.assign(fill_risk_pct=joined["fill_risk_pct"]), args.cost_pct, args.stamp_duty_pct)
        joined = joined.sort_values(["day", "as_of", "rank"])
        running = 0.0
        for number, (_, r) in enumerate(joined.iterrows(), 3):
            status = r["status"] if isinstance(r["status"], str) else "pending"
            head = [r["day"], r["as_of"], int(r["rank"]), r["company"], r["ticker"], r["direction"], r["setup"]]
            if status == "done":
                position = args.position_gbp
                risk = position * r["fill_risk_pct"] / 100          # pounds lost if the stop is hit
                gross, dealing, stamp = r["r_gross"] * risk, r["cost_r"] * risk, r["stamp_r"] * risk
                net = gross - dealing - stamp
                running += net
                label = {"target": "Target hit", "stop": "Stopped out", "timeout": "Closed at end of hold"}[r["outcome"]]
                put(mock, number, head + [round(r["fill"], 2), round(r["stop"], 2), round(r["target"], 2), label,
                                          str(r["exit_time"])[:16].replace("T", " "), round(position, 0), round(gross, 2),
                                          round(-dealing, 2), (round(-stamp, 2) if stamp else 0), round(net, 2), round(running, 2)],
                    fill=GOOD if net > 0 else BAD)
            else:
                label = {"skipped": ("Skipped (no trade within 30 min)" if r["outcome"] == "no_candle"
                                     else "Skipped (price already past stop or target)"),
                         "nodata": "No data"}.get(status, "Not resolved yet")
                put(mock, number, head + [(round(r["fill"], 2) if pd.notna(r["fill"]) else None),
                                          round(r["stop"], 2), round(r["target"], 2), label], fill=NEUTRAL)
    widths(mock, [11, 10, 6, 28, 9, 10, 12, 9, 9, 9, 40, 16, 14, 14, 13, 12, 14, 13])
    mock.freeze_panes = "A3"

    # ---------------- Daily equity ----------------
    header(daily, 1, ["Date"] + [f"{n} (cumulative R)" for n in PICK_SCENARIOS] + ["Top 2 per day (cumulative £)"])
    daily.row_dimensions[1].height = 45
    curves = {}
    for name in PICK_SCENARIOS:
        trades = trades_for(picks[name], main_h) if not picks[name].empty else pd.DataFrame()
        curves[name] = trades.groupby("day")["r_net"].sum() if not trades.empty else pd.Series(dtype=float)
    all_days = sorted(set().union(*[set(c.index) for c in curves.values()])) if curves else []
    cumulative = {name: curves[name].reindex(all_days, fill_value=0.0).cumsum() for name in PICK_SCENARIOS}
    top2_trades = trades_for(picks["Top 2 per day"], main_h) if not picks["Top 2 per day"].empty else pd.DataFrame()
    if top2_trades.empty:
        pounds_by_day = pd.Series(dtype=float)
    else:
        net_gbp = (top2_trades["r_net"] - top2_trades["stamp_r"]) * top2_trades["fill_risk_pct"] / 100 * args.position_gbp
        pounds_by_day = net_gbp.groupby(top2_trades["day"]).sum().reindex(all_days, fill_value=0.0).cumsum()
    for number, day in enumerate(all_days, 2):
        put(daily, number, [day] + [round(float(cumulative[n][day]), 3) for n in PICK_SCENARIOS]
            + [round(float(pounds_by_day.get(day, 0.0)), 2)])
    widths(daily, [12, 22, 22, 26, 24])
    if len(all_days) >= 2:
        chart = LineChart()
        chart.title = f"Paper-trading equity, hold up to {main_h} day(s), R after costs"
        chart.y_axis.title, chart.x_axis.title = "Cumulative R", "Date"
        chart.add_data(Reference(daily, min_col=2, max_col=4, min_row=1, max_row=len(all_days) + 1), titles_from_data=True)
        chart.set_categories(Reference(daily, min_col=1, min_row=2, max_row=len(all_days) + 1))
        for series, colour in zip(chart.series, ("2F6DB5", "C0392B", "2E9E5B")):
            series.smooth = False                      # straight lines: an equity path has no values between trades
            series.graphicalProperties.line.solidFill = colour
            series.graphicalProperties.line.width = 22000
        chart.x_axis.tickLblPos = "low"                # day labels at the bottom, not across the zero line
        chart.x_axis.delete = False                    # newer openpyxl hides axes in Excel unless told otherwise
        chart.y_axis.delete = False
        chart.height, chart.width = 9, 22
        daily.add_chart(chart, "G2")
    put(daily, len(all_days) + 4, [f"£ column: each trade is £{args.position_gbp:.0f} of shares, after dealing costs and stamp duty. Change with --position-gbp."])

    # ---------------- All outcomes ----------------
    heads = ["Date", "Scan (as of)", "Company", "Ticker", "Type", "Index", "Setup", "Direction", "Score", "Touches", "Stop", "Target"]
    for horizon in horizons:
        heads += [f"Result, {horizon}d", f"R, {horizon}d (before costs)"]
    header(allout, 1, heads)
    allout.row_dimensions[1].height = 45
    by_h = {h: outcomes[outcomes["horizon"] == h].set_index("key") for h in horizons}
    for number, (_, r) in enumerate(pool.sort_values(["day", "as_of", "score"], ascending=[False, False, False]).iterrows(), 2):
        values = [r["day"], r["as_of"], r["company"], r["ticker"], "Trust" if kinds.get(r["ticker"]) == "trust" else "Company",
                  r["index"], r["setup"], r["direction"], r["score"], int(r["touches"]), r["stop"], r["target"]]
        for horizon in horizons:
            rows_h = by_h[horizon]
            if r["key"] in rows_h.index:
                o = rows_h.loc[r["key"]]
                result = o["outcome"] if o["status"] == "done" else ("skipped" if o["status"] == "skipped" else "no data")
                values += [result, o["r_gross"] if o["status"] == "done" else None]
            else:
                values += ["pending", None]
        put(allout, number, values)
    widths(allout, [11, 10, 30, 9, 9, 10, 9, 9, 7, 8, 9, 9] + [11, 12] * len(horizons))
    allout.freeze_panes = "A2"
    return wb


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Outcome tracker and paper-trading simulator for the FTSE scanner")
    parser.add_argument("--data-dir", default=str(sc.DEFAULT_DATA_DIR), help="Folder holding the scanner's history (default: scan_data)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="Workbook to write (default: ftse_outcomes.xlsx)")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH), help="Constituent CSV (for the trust/company label)")
    parser.add_argument("--interval", default="15m", help="Candle size the scanner used (default: 15m)")
    parser.add_argument("--period", default="60d", help="How much history to download (Yahoo max for 15m is 60d)")
    parser.add_argument("--horizons", default="1,2,5", help="Maximum holding periods to test, in trading days (default: 1,2,5)")
    parser.add_argument("--horizon", type=int, default=1, help="Holding period used for the detail sheets (default: 1 = same day)")
    parser.add_argument("--pick-time", default="10:30", help="The mock trader decides at the first scan at or after this time (default: 10:30)")
    parser.add_argument("--last-entry", default="15:30", help="Ignore setups from scans after this time (default: 15:30)")
    parser.add_argument("--cost-pct", type=float, default=0.10, help="Assumed round-trip cost (spread + slippage) as %% of price (default: 0.10)")
    parser.add_argument("--stamp-duty-pct", type=float, default=0.5, help="Stamp duty %% on long entries, for the 'if you buy actual shares' column; 0 turns it off (default: 0.5)")
    parser.add_argument("--position-gbp", type=float, default=100.0, help="Pounds put into each mock trade (default: 100)")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds between downloads (default: 0.3)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    if args.horizon not in horizons:
        horizons.append(args.horizon)
        args.horizons = ",".join(str(h) for h in sorted(horizons))
    data_dir = Path(args.data_dir)
    candidates = load_candidates(data_dir, args.last_entry)
    if candidates.empty:
        logged = (data_dir / "setups.csv").exists() and (data_dir / "setups.csv").stat().st_size > 0
        if logged:
            print(f"Nothing to follow up yet: every logged setup is either conflicting or from a scan after {args.last_entry}, "
                  f"which you couldn't have acted on. Come back after a few market-hours scans.")
        else:
            print("No scan history found yet. Run the scanner for a few days first (python ftse_trade_scanner.py).")
        sys.exit(1)

    pool = first_sightings(candidates)
    picks = build_picks(candidates, args.pick_time)
    needed = pd.concat([pool] + [p for p in picks.values() if not p.empty]).drop_duplicates("key")
    print(f"{len(candidates)} logged setups on {candidates['day'].nunique()} day(s); "
          f"{len(pool)} first sightings, {len(needed)} to follow up.")

    out_path = data_dir / "outcomes.csv"
    cache = read_outcomes(out_path)
    resolved, pending, failed = evaluate_pending(needed, cache, horizons, args.interval, args.period, args.delay, out_path)
    print(f"Resolved {resolved} new outcome(s); {pending} not resolvable yet (the hold period hasn't finished); {failed} download(s) failed.")

    outcomes = read_outcomes(out_path)
    kinds = {c.yahoo_ticker: c.kind for c in sc.load_universe(Path(args.universe))} if Path(args.universe).exists() else {}
    generated = datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M UK time")
    workbook = build_report(args, candidates, pool, picks, outcomes, kinds, generated)
    saved = sc.save_workbook(workbook, Path(args.out))

    top2 = picks["Top 2 per day"]
    if not top2.empty:
        merged = top2.merge(outcomes[outcomes["horizon"] == args.horizon], on="key", suffixes=("", "_o"))
        merged = merged[merged["status"] == "done"]
        if not merged.empty:
            merged = add_costs(merged, args.cost_pct, args.stamp_duty_pct)
            stats = summarise(merged.sort_values(["day", "as_of", "rank"]))
            print(f"\nMock trader (top 2 per day, hold up to {args.horizon}d): {stats['n']} trades, "
                  f"{pct(stats['target'])} hit target, average {stats['net']:.2f}R after costs, total {stats['total_net']:.1f}R.")
            if stats["n"] < 30:
                print("That is far too few trades to conclude anything.")
    print(f"\nWorkbook updated: {saved}")


if __name__ == "__main__":
    main()
