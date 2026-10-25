"""
FTSE Scanner Outcome Tracker and Paper-Trading Simulator
=========================================================
Looks back at every setup the scanner has logged (scan_data/setups.csv), works
out what the market did next, and builds a workbook that answers one question:
would following this model have made money?

What it does
  1. Walks each logged setup forward through the candles that followed it:
     did price reach the target or the stop first, or neither?
  2. Scores the live mock trader's paper trades (taken at trigger points
     during the day, saved in scan_data/live_mock_trades.csv) and shows how
     they performed over time (realised balance and drawdown).
  3. Compares those trades with the average of EVERY idea the scanner flagged
     (a random pick). If the chosen trades are not better than that
     average, the entry rules are not adding value.

(The old fixed-time "top 2 ideas at 10:30" mock trader was removed on
23 Oct 2026; build_picks is kept only for ftse_backtest.py.)

Run it once a day after the close (Yahoo only keeps about 60 days of 15-minute
candles, so corrected results are cached in scan_data/outcomes.v3.csv):

    python ftse_outcomes.py

Assumptions (also written into the workbook's Summary sheet)
  * Fill at the first candle OPEN on or after actual scan availability.
  * Require completed contiguous bars; missing coverage stays unresolved.
  * Legacy outcomes.csv is preserved; corrected results use outcomes.v3.csv.
  * If the stop and target both fall inside one candle, the stop is assumed
    to have been hit first (pessimistic).
  * Gaps through a stop fill at the open, so a loss can exceed the planned stop amount.
  * Setups that have already gapped past their stop or target at the fill are
    skipped, not counted as trades.
  * Trading costs are applied when the report is built, so you can change them
    without re-downloading anything.
"""

import argparse
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import ftse_trade_scanner as sc
from ftse_live_mock import LIVE_FILENAME, read_live
from ftse_execution import SIMULATION_VERSION, Sim, market_stamp, simulate, daily_value, day_range_misses

SCRIPT_DIR = Path(__file__).parent
DEFAULT_OUT = SCRIPT_DIR / "ftse_outcomes.xlsx"
OUTCOME_COLUMNS = ["key", "day", "ticker", "direction", "horizon", "status", "outcome", "fill",
                   "exit_price", "exit_time", "r_gross", "fill_risk_pct", "ambiguous"]
CACHE_FILENAME = "outcomes.v3.csv"

PICK_SCENARIOS = ["Top 1 per day", "Top 2 per day", "Top 2 per day, persistent"]
POOL = "Pool average (every first sighting)"
LIVE = "Live: trades at trigger points"


# ----------------------------------------------------------------------
# Simulating one setup
# ----------------------------------------------------------------------

def daily_prices(raw: pd.DataFrame) -> pd.DataFrame:
    """Yahoo daily candles indexed by plain date, for day_range_misses."""
    frame = raw.rename(columns=str.lower)[["high", "low", "close"]].astype(float).dropna()
    index = pd.DatetimeIndex(frame.index)
    frame.index = (index.tz_convert(sc.UK_TZ).tz_localize(None) if index.tz is not None else index).normalize()
    return frame[~frame.index.duplicated(keep="last")]


# ----------------------------------------------------------------------
# Which setups to evaluate
# ----------------------------------------------------------------------

def load_candidates(data_dir: Path, last_entry: str) -> pd.DataFrame:
    """Every logged, non-conflicting setup that was early enough in the day to act on."""
    setups = sc._read_table(data_dir / "setups.csv", sc.SETUP_COLUMNS)
    if setups.empty:
        return setups
    keep = setups[(~setups["conflict"]) & (setups["as_of"] <= last_entry)].copy()
    # Missing availability must never turn into a backdated executable signal.
    keep["_available"] = keep["run_time"].map(lambda v: market_stamp(v) if str(v).strip() else pd.NaT)
    keep = keep[keep["_available"].notna()].copy()
    before_cutoff = [stamp.strftime("%H:%M:%S") <= last_entry + ":00" and stamp.date().isoformat() == str(day)
                     for stamp, day in zip(keep["_available"], keep["day"])]
    keep = keep.loc[before_cutoff].copy()
    # Retain distinct decision runs even if two runners saw the same candle.
    keep = keep.sort_values(["_available", "ticker", "direction", "mode"])
    keep = keep.drop_duplicates(["run_time", "mode", "data_time", "ticker", "direction"])
    keep["key"] = (keep["data_time"] + "|" + keep["ticker"] + "|" + keep["direction"]
                   + "|" + keep["run_time"] + "|" + keep["mode"])
    keep = keep.drop(columns="_available")
    return keep.reset_index(drop=True)


def first_sightings(candidates: pd.DataFrame) -> pd.DataFrame:
    """The first time each idea (share + direction) appeared on each day."""
    ordered = candidates.assign(_available=candidates["run_time"].map(market_stamp))
    ordered = ordered.sort_values(["day", "_available", "score"], ascending=[True, True, False])
    return ordered.drop_duplicates(["day", "ticker", "direction"], keep="first").drop(columns="_available")


def build_picks(candidates: pd.DataFrame, pick_time: str, runs: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    """Choose a recorded decision run first, even when it contains no ideas.

    Callers must supply runs; absence of a run log cannot safely establish an
    empty decision time and therefore yields no simulated selections.
    """
    picks = {name: [] for name in PICK_SCENARIOS}
    if runs is None or runs.empty or candidates.empty:
        return {name: pd.DataFrame() for name in PICK_SCENARIOS}
    for day, day_runs in runs.groupby("day"):
        eligible = day_runs.assign(_available=day_runs["run_time"].map(market_stamp))
        eligible = eligible[eligible["_available"].map(lambda t: t.date().isoformat() == str(day) and t.strftime("%H:%M:%S") >= pick_time + ":00")]
        if eligible.empty:
            continue
        decision = eligible.sort_values(["_available", "mode"]).iloc[0]
        group = candidates[candidates["day"] == day]
        observed = group[group["run_time"].map(market_stamp) <= decision["_available"]]
        seen = observed.groupby(["ticker", "direction"])["run_time"].nunique()
        here = group[(group["run_time"] == decision["run_time"]) & (group["mode"] == decision["mode"])
                     & (group["data_time"] == decision["data_time"])].sort_values(["score", "ticker"], ascending=[False, True])
        here = here.assign(seen=[int(seen.get((t, d), 0)) for t, d in zip(here["ticker"], here["direction"])])
        here = here.drop_duplicates("ticker")
        for name, chosen in (("Top 1 per day", here.head(1)), ("Top 2 per day", here.head(2)),
                             ("Top 2 per day, persistent", here[here["seen"] >= 2].head(2))):
            if not chosen.empty:
                picks[name].append(chosen.assign(rank=range(1, len(chosen) + 1)))
    return {name: pd.concat(parts) if parts else pd.DataFrame() for name, parts in picks.items()}


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
    terminal = cache[cache["status"].isin(["done", "skipped"])]
    done = set(zip(terminal["key"], terminal["horizon"]))
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
        try:
            daily = daily_prices(sc.fetch_data(ticker, "1d", "3mo"))
        except Exception:
            daily = None            # gaps then stay unresolved, as before
        for row, horizon in items:
            signal_close = market_stamp(row["data_time"]) + pd.Timedelta(minutes=minutes)
            available = max(signal_close, market_stamp(row["run_time"]))
            sim = simulate(row["direction"], row["stop"], row["target"], available, data, horizon, now, interval,
                           daily=daily)
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
        # Replace retried missing-data records atomically, preserving the v1 file.
        combined = (pd.concat([cache, pd.DataFrame(new_rows)], ignore_index=True)
                    if not cache.empty else pd.DataFrame(new_rows))
        combined = combined.drop_duplicates(["key", "horizon"], keep="last")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = out_path.with_suffix(out_path.suffix + ".tmp")
        combined[OUTCOME_COLUMNS].to_csv(temporary, index=False)
        temporary.replace(out_path)
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


def pounds(frame: pd.DataFrame, column: str, position_gbp: float) -> pd.Series:
    """Convert a per-trade result held in risk units into pounds for a position of `position_gbp`."""
    return frame[column] * position_gbp * frame["fill_risk_pct"] / 100


def drawdown(values: list[float]) -> float:
    peak, worst, total = 0.0, 0.0, 0.0
    for value in values:
        total += value
        peak = max(peak, total)
        worst = min(worst, total - peak)
    return worst


def realised_order(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    result = trades.copy()
    result["_exit"] = pd.to_datetime(result["exit_time"], utc=True, errors="raise")
    result["realised_day"] = result["_exit"].dt.tz_convert(sc.UK_TZ).dt.strftime("%Y-%m-%d")
    return result.sort_values(["_exit", "key"] if "key" in result else ["_exit"]).drop(columns="_exit").reset_index(drop=True)


def summarise(trades: pd.DataFrame) -> dict:
    """Headline numbers, with realised drawdown always calculated in exit order."""
    trades = realised_order(trades)
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


MONEY_FORMAT = '"£"#,##0.00;[Red]-"£"#,##0.00;"£"0.00'
TRADE_HEADERS = ["Date", "Signal time", "Trade no.", "Company", "Ticker", "Direction", "Setup", "Entry", "Stop", "Target",
                 "Result", "Exit time", "Amount traded (£)", "Gross profit / loss (£)", "Dealing costs (£)",
                 "Stamp duty (£)", "Net after all fees (£)", "Realised running net (£)"]
TOTALS_ROW, COMPARE_ROW, TRADES_HEADER_ROW = 5, 8, 12


def build_report(args, candidates: pd.DataFrame, pool: pd.DataFrame, picks: dict[str, pd.DataFrame],
                 outcomes: pd.DataFrame, kinds: dict[str, str], generated: str) -> Workbook:
    """Two sheets: the live mock trades (with totals and one comparison line on top) and the daily balance."""
    main_h = args.horizon
    wb = Workbook()
    live_sheet = wb.active
    live_sheet.title = "Live Mock Trades"
    daily = wb.create_sheet("Daily")
    chosen = picks.get(LIVE, pd.DataFrame())
    at_h = outcomes[outcomes["horizon"] == main_h]

    def cash_flows(trades):
        if trades.empty:
            return pd.DataFrame(columns=["gross_gbp", "dealing_gbp", "stamp_gbp", "net_gbp"])
        risk_gbp = args.position_gbp * trades["fill_risk_pct"] / 100
        result = pd.DataFrame({"gross_gbp": (trades["r_gross"] * risk_gbp).round(2),
                               "dealing_gbp": (trades["cost_r"] * risk_gbp).round(2),
                               "stamp_gbp": (trades["stamp_r"] * risk_gbp).round(2)}, index=trades.index)
        result["net_gbp"] = (result["gross_gbp"] - result["dealing_gbp"] - result["stamp_gbp"]).round(2)
        return result

    def completed(frame: pd.DataFrame) -> pd.DataFrame:
        if frame.empty:
            return pd.DataFrame()
        joined = frame.merge(at_h, on="key", suffixes=("", "_o"))
        joined = joined[joined["status"] == "done"]
        return realised_order(add_costs(joined, args.cost_pct, args.stamp_duty_pct)) if not joined.empty else joined

    trades = completed(chosen)
    cash = cash_flows(trades)
    done = len(trades)
    waiting = len(chosen) - done

    # ---------------- Live mock trades: title and totals ----------------
    put(live_sheet, 1, ["Live mock trades (paper only, no real orders)"], bold=True)
    note_row(live_sheet, 2, f"Generated {generated}. The live mock trader enters ideas as they trigger during the day: every "
                            "idea that passes the rules, up to 5 open at once and 10 a day (before 26 Oct 2026: 1 new trade per scan and 5 a day; before 23 Oct: 3 a day), one per share, FTSE 100 shares only from 23 Oct 2026, "
                            "and only ideas that pass the entry rules, including the check that the target would pay more than the stop "
                            "could cost after costs. Each entry was saved the moment the scan ran (scan_data/live_mock_trades.csv) and "
                            "is filled at the open of the next 15-minute candle. Trades close at the target, the stop, or the end of the day.",
             last_col=18, chars_per_line=190, fill=NEUTRAL)
    note_row(live_sheet, 3, f"Each trade is £{args.position_gbp:.0f}. Dealing costs = {args.cost_pct:.2f}% (buy and sell together); stamp duty "
                            f"= {args.stamp_duty_pct:.2f}% on buys of real shares (sells never pay it). Grey rows were never entered or "
                            "have no result yet. Results rechecked with version 3 missing-price safeguards; older totals may differ. "
                            "Only resolved trades enter the totals.", last_col=18, chars_per_line=190, fill=NEUTRAL)
    header(live_sheet, TOTALS_ROW - 1, ["Net after all fees (£)", "Completed", "Profitable", "Losing", "Flat", "Not resolved / skipped",
                                        "Gross profit / loss (£)", "Dealing costs (£)", "Stamp duty (£)"])
    live_sheet.row_dimensions[TOTALS_ROW - 1].height = 32
    profits, losses = int((cash["net_gbp"] > 1e-9).sum()), int((cash["net_gbp"] < -1e-9).sum())
    if done or chosen.empty:
        put(live_sheet, TOTALS_ROW, [round(float(cash["net_gbp"].sum()), 2), done, profits, losses, done - profits - losses, waiting,
                                     round(float(cash["gross_gbp"].sum()), 2), round(-float(cash["dealing_gbp"].sum()), 2),
                                     round(-float(cash["stamp_gbp"].sum()), 2)], bold=True)
    else:
        put(live_sheet, TOTALS_ROW, ["Awaiting results", 0, 0, 0, 0, waiting, "Awaiting results", "Awaiting results", "Awaiting results"],
            bold=True)

    # ---------------- One comparison line ----------------
    header(live_sheet, COMPARE_ROW - 1, ["Comparison", "Completed", "Average per trade (£)", "Every idea: completed",
                                         "Every idea: average per trade (£)", "Difference (£)"])
    live_sheet.row_dimensions[COMPARE_ROW - 1].height = 32
    every = completed(pool)
    avg = float(pounds(trades, "r_net_stamp", args.position_gbp).mean()) if done else None
    base = float(pounds(every, "r_net_stamp", args.position_gbp).mean()) if len(every) else None
    put(live_sheet, COMPARE_ROW, ["Live trades vs every idea", done, round(avg, 2) if avg is not None else "Awaiting results",
                                  len(every), round(base, 2) if base is not None else "Not available",
                                  round(avg - base, 2) if avg is not None and base is not None else "Not available"])
    note_row(live_sheet, COMPARE_ROW + 1, "Every idea = the first sighting each day of every idea the scanner flagged (all shares, before the "
                                          "entry rules), traded the same way with the same costs. If the live trades don't beat it, the entry "
                                          "rules aren't helping. With fewer than about 100 completed trades the difference is mostly luck.",
             last_col=18, chars_per_line=190)

    # ---------------- Trade list ----------------
    header(live_sheet, TRADES_HEADER_ROW, TRADE_HEADERS)
    live_sheet.row_dimensions[TRADES_HEADER_ROW].height = 45
    if not chosen.empty:
        joined = chosen.merge(at_h, on="key", how="left", suffixes=("", "_o"))
        joined = realised_order(add_costs(joined, args.cost_pct, args.stamp_duty_pct))
        flows = cash_flows(joined)
        running = 0.0
        for number, (idx, r) in enumerate(joined.iterrows(), TRADES_HEADER_ROW + 1):
            status = r["status"] if isinstance(r["status"], str) else "pending"
            head = [r["day"], str(r["run_time"])[11:16], int(r["rank"]), r["company"], r["ticker"], r["direction"], r["setup"]]
            if status == "done":
                gross, dealing, stamp, net = (float(flows.loc[idx, c]) for c in ("gross_gbp", "dealing_gbp", "stamp_gbp", "net_gbp"))
                running += net
                label = {"target": "Target hit", "stop": "Stopped out", "timeout": "Closed at end of day"}[r["outcome"]]
                put(live_sheet, number, head + [round(r["fill"], 2), round(r["stop"], 2), round(r["target"], 2), label,
                                                str(r["exit_time"])[:16].replace("T", " "), round(args.position_gbp, 0), round(gross, 2),
                                                round(-dealing, 2), (round(-stamp, 2) if stamp else 0), round(net, 2), round(running, 2)],
                    fill=GOOD if net > 0 else BAD)
            else:
                label = {"skipped": ({"no_candle": "Skipped (no entry candle before close)",
                                       "outside_session": "Skipped (outside market hours)",
                                       "invalid_levels": "Skipped (invalid stop or target)"}.get(
                                           r["outcome"], "Skipped (price already past stop or target)")),
                         "nodata": "No data"}.get(status, "Not resolved yet")
                put(live_sheet, number, head + [(round(r["fill"], 2) if pd.notna(r["fill"]) else None),
                                                round(r["stop"], 2), round(r["target"], 2), label], fill=NEUTRAL)
    widths(live_sheet, [14, 11, 12, 28, 14, 14, 12, 12, 12, 10, 40, 16, 14, 14, 13, 12, 14, 13])
    live_sheet.freeze_panes = f"A{TRADES_HEADER_ROW + 1}"

    # ---------------- Daily realised balance ----------------
    header(daily, 1, ["Date", f"{LIVE} (cumulative £)"])
    daily.row_dimensions[1].height = 45
    curve = cash["net_gbp"].groupby(trades["realised_day"]).sum() if done else pd.Series(dtype=float)
    all_days = sorted(curve.index)
    for number, (day, value) in enumerate(curve.reindex(all_days).cumsum().items(), 2):
        put(daily, number, [day, round(float(value), 2)])
    widths(daily, [12, 26])
    if len(all_days) >= 2:
        chart = LineChart()
        chart.title = "Live mock trades: running balance after all fees (£)"
        chart.y_axis.title, chart.x_axis.title = "Cumulative £", "Date"
        chart.add_data(Reference(daily, min_col=2, max_col=2, min_row=1, max_row=len(all_days) + 1), titles_from_data=True)
        chart.set_categories(Reference(daily, min_col=1, min_row=2, max_row=len(all_days) + 1))
        for series in chart.series:
            series.smooth = False                      # straight lines: realised balance changes only on exits
            series.graphicalProperties.line.solidFill = "2F6DB5"
            series.graphicalProperties.line.width = 22000
        chart.x_axis.tickLblPos = "low"
        chart.x_axis.delete = False                    # newer openpyxl hides axes in Excel unless told otherwise
        chart.y_axis.delete = False
        chart.legend = None
        chart.height, chart.width = 9, 22
        daily.add_chart(chart, "D2")
    put(daily, len(all_days) + 4, [f"Each trade is £{args.position_gbp:.0f}, after dealing costs and stamp duty."])

    # ---------------- Money formats ----------------
    for col in (1, 7, 8, 9):
        live_sheet.cell(TOTALS_ROW, col).number_format = MONEY_FORMAT
    for col in (3, 5, 6):
        live_sheet.cell(COMPARE_ROW, col).number_format = MONEY_FORMAT
    for row_idx in range(TRADES_HEADER_ROW + 1, live_sheet.max_row + 1):
        for col in range(13, 19):
            if isinstance(live_sheet.cell(row_idx, col).value, (float, int)):
                live_sheet.cell(row_idx, col).number_format = MONEY_FORMAT
    for row_idx in range(2, daily.max_row + 1):
        if isinstance(daily.cell(row_idx, 2).value, (float, int)):
            daily.cell(row_idx, 2).number_format = MONEY_FORMAT
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
    parser.add_argument("--horizon", type=int, default=1, help="Holding period in trading days (default: 1 = same day)")
    parser.add_argument("--last-entry", default="15:30", help="Ignore setups from scans after this time (default: 15:30)")
    parser.add_argument("--cost-pct", type=float, default=0.10, help="Assumed round-trip cost (spread + slippage) as %% of price (default: 0.10)")
    parser.add_argument("--stamp-duty-pct", type=float, default=0.5, help="Stamp duty %% on long entries, for the 'if you buy actual shares' column; 0 turns it off (default: 0.5)")
    parser.add_argument("--position-gbp", type=float, default=100.0, help="Pounds put into each mock trade (default: 100)")
    parser.add_argument("--delay", type=float, default=0.3, help="Seconds between downloads (default: 0.3)")
    args = parser.parse_args()

    horizons = [args.horizon]
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
    picks = {}
    live = read_live(data_dir / LIVE_FILENAME)
    if not live.empty:
        trade_no = dict(zip(live["key"], pd.to_numeric(live["trade_no"], errors="coerce")))
        chosen = candidates[candidates["key"].isin(trade_no)]
        picks[LIVE] = chosen.assign(rank=chosen["key"].map(trade_no).fillna(0).astype(int))
    needed = pd.concat([pool] + [p for p in picks.values() if not p.empty]).drop_duplicates("key")
    print(f"{len(candidates)} logged setups on {candidates['day'].nunique()} day(s); "
          f"{len(pool)} first sightings, {len(needed)} to follow up.")

    out_path = data_dir / CACHE_FILENAME
    if (data_dir / "outcomes.csv").exists() or (data_dir / "outcomes.v2.csv").exists():
        print("Original v1/v2 outcomes retained; shared execution results use " + CACHE_FILENAME)
    cache = read_outcomes(out_path)
    resolved, pending, failed = evaluate_pending(needed, cache, horizons, args.interval, args.period, args.delay, out_path)
    print(f"Resolved {resolved} new outcome(s); {pending} not resolvable yet (the hold period hasn't finished); {failed} download(s) failed.")

    outcomes = read_outcomes(out_path)
    kinds = {c.yahoo_ticker: c.kind for c in sc.load_universe(Path(args.universe))} if Path(args.universe).exists() else {}
    generated = datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M UK time")
    workbook = build_report(args, candidates, pool, picks, outcomes, kinds, generated)
    saved = sc.save_workbook(workbook, Path(args.out))

    top2 = picks.get(LIVE, pd.DataFrame())
    if not top2.empty:
        merged = top2.merge(outcomes[outcomes["horizon"] == args.horizon], on="key", suffixes=("", "_o"))
        merged = merged[merged["status"] == "done"]
        if not merged.empty:
            merged = add_costs(merged, args.cost_pct, args.stamp_duty_pct)
            stats = summarise(merged)
            net = pounds(merged, "r_net_stamp", args.position_gbp)
            print(f"\nLive mock trader (trigger points, hold up to {args.horizon}d, £{args.position_gbp:.0f} per trade): {stats['n']} trades, "
                  f"{pct(stats['target'])} hit target, average £{net.mean():.2f} per trade after all fees, total £{net.sum():.2f}.")
            if stats["n"] < 30:
                print("That is far too few trades to conclude anything.")
    print(f"\nWorkbook updated: {saved}")


if __name__ == "__main__":
    main()
