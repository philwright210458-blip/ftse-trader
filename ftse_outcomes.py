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
     those paper trades performed over time (realised balance and drawdown).
  3. Compares those picks with the average of EVERY idea the scanner flagged
     (a random pick). If the top-ranked ideas are not better than that
     average, the ranking is not adding value.

Run it once a day after the close (Yahoo only keeps about 60 days of 15-minute
candles, so corrected results are cached in scan_data/outcomes.v2.csv):

    python ftse_outcomes.py

Assumptions (also written into the workbook's Summary sheet)
  * Fill at the first candle OPEN on or after actual scan availability.
  * Require completed contiguous bars; missing coverage stays unresolved.
  * Legacy outcomes.csv is preserved; corrected results use outcomes.v2.csv.
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import ftse_trade_scanner as sc
from ftse_live_mock import LIVE_FILENAME, read_live
from ftse_sessions import is_session, session_open_close, sessions_from

SCRIPT_DIR = Path(__file__).parent
DEFAULT_OUT = SCRIPT_DIR / "ftse_outcomes.xlsx"
OUTCOME_COLUMNS = ["key", "day", "ticker", "direction", "horizon", "status", "outcome", "fill",
                   "exit_price", "exit_time", "r_gross", "fill_risk_pct", "ambiguous"]
SIMULATION_VERSION = "availability-session-v2"
CACHE_FILENAME = "outcomes.v2.csv"

PICK_SCENARIOS = ["Top 1 per day", "Top 2 per day", "Top 2 per day, persistent"]
POOL = "Pool average (every first sighting)"
LIVE = "Live: trades at trigger points (up to 5 a day)"


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


def market_stamp(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize(sc.UK_TZ) if stamp.tzinfo is None else stamp.tz_convert(sc.UK_TZ)


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
    minutes = sc.interval_minutes(interval)
    if minutes is None or minutes <= 0 or horizon < 1 or side not in {"Long", "Short"}:
        raise ValueError("Use a positive intraday interval, horizon and valid direction")
    if not all(math.isfinite(float(v)) and float(v) > 0 for v in (stop, target)):
        return Sim("skipped", "invalid_levels")
    if (side == "Long" and stop >= target) or (side == "Short" and stop <= target):
        return Sim("skipped", "invalid_levels")
    if not is_session(available):
        return Sim("skipped", "outside_session")
    opening, closing = session_open_close(available)
    if available < opening or available >= closing:
        return Sim("skipped", "outside_session")
    step = pd.Timedelta(minutes=minutes)
    # Anchor intervals to this exchange session, not midnight or a UTC offset.
    expected_fill = opening + math.ceil((available - opening) / step) * step
    if expected_fill >= closing:
        return Sim("skipped", "no_candle")
    complete = sc.drop_untraded_bars(sc.completed_candles(data, interval, now))
    after = complete[complete.index >= expected_fill]
    expired = now - available > pd.Timedelta(days=max(10, horizon * 3 + 4))
    missing = Sim("nodata", "missing_coverage") if expired else Sim("pending", "missing_coverage")
    if after.empty:
        return missing
    first = after.index[0]
    if first.date() != available.date() or first - available > MAX_FILL_DELAY:
        return Sim("skipped", "no_candle")
    # Unknown bars immediately before a fill cannot establish whether the setup
    # was still valid, so do not retrospectively fill at a later observed price.
    if first != expected_fill:
        return missing
    fill = float(after["open"].iloc[0])
    if not math.isfinite(fill) or fill <= 0:
        return missing
    if (side == "Long" and (fill <= stop or fill >= target)) or (side == "Short" and (fill >= stop or fill <= target)):
        return Sim("skipped", "gap", fill=fill)
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
                return missing
            if stamp not in complete.index:
                if not day_range_misses(daily, day, side, stop, target):
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
    key = (stamp.tz_convert(sc.UK_TZ).tz_localize(None) if stamp.tzinfo else stamp).normalize()
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


def build_report(args, candidates: pd.DataFrame, pool: pd.DataFrame, picks: dict[str, pd.DataFrame],
                 outcomes: pd.DataFrame, kinds: dict[str, str], generated: str) -> Workbook:
    horizons = [int(h) for h in args.horizons.split(",")]
    main_h = args.horizon
    wb = Workbook()
    summary, breakdown, mock, live_sheet, daily, allout = wb.active, *(wb.create_sheet(n) for n in ("Breakdowns", "Mock Trades", "Live Mock Trades", "Daily", "All Outcomes"))
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
        return realised_order(joined)

    daily_names = PICK_SCENARIOS + ([LIVE] if LIVE in picks else [])
    scenarios = {name: picks[name] for name in daily_names}
    scenarios[POOL] = pool

    # Main results use the actual per-trade cash flows, never average R * £100.
    def cash_flows(trades):
        if trades.empty:
            return pd.DataFrame(columns=["gross_gbp", "dealing_gbp", "stamp_gbp", "net_gbp"])
        risk_gbp = args.position_gbp * trades["fill_risk_pct"] / 100
        result = pd.DataFrame({"gross_gbp": (trades["r_gross"] * risk_gbp).round(2),
                               "dealing_gbp": (trades["cost_r"] * risk_gbp).round(2),
                               "stamp_gbp": (trades["stamp_r"] * risk_gbp).round(2)}, index=trades.index)
        result["net_gbp"] = (result["gross_gbp"] - result["dealing_gbp"] - result["stamp_gbp"]).round(2)
        return result

    def coverage(frame, horizon):
        rows = resolved(frame["key"], horizon) if not frame.empty else outcomes.iloc[:0]
        counts = {state: int((rows["status"] == state).sum()) for state in ("done", "skipped", "nodata")}
        counts["pending"] = len(frame) - sum(counts.values())
        return counts

    top2 = picks["Top 2 per day"]
    main_trades = trades_for(top2, main_h) if not top2.empty else pd.DataFrame()
    cash = cash_flows(main_trades)
    counts = coverage(top2, main_h)
    profits = int((cash["net_gbp"] > 1e-9).sum())
    losses = int((cash["net_gbp"] < -1e-9).sum())
    flat = len(main_trades) - profits - losses
    days = sorted(set(candidates["day"])) if not candidates.empty else []
    put(summary, 1, ["FTSE paper research: main results"], bold=True)
    note_row(summary, 2, f"Generated {generated}. Corrected simulation: {SIMULATION_VERSION}. Research only; no real orders.", last_col=8, chars_per_line=110)
    note_row(summary, 3, f"Top 2 per day, held up to {main_h} trading day(s). £{args.position_gbp:.2f} position value per simulated trade. "
                         f"Decision: first scan available at or after {args.pick_time}. These are recorded-history results, not a current trading shortlist.", last_col=8, chars_per_line=110)
    main_days = main_trades["day"].nunique() if not main_trades.empty else 0
    note_row(summary, 4, f"Evidence: {len(main_trades)} completed trades across {main_days} market day(s); pending and missing results are excluded. "
                         "This is a descriptive sample, not proof of an edge or a forecast.", last_col=8, chars_per_line=110)
    header(summary, 5, ["Net realised (£)", "Completed", "Profitable", "Losing", "Flat"])
    put(summary, 6, [round(float(cash["net_gbp"].sum()), 2) if len(main_trades) or top2.empty else "Awaiting results", len(main_trades), profits, losses, flat], bold=True)
    header(summary, 8, ["Gross P/L (£)", "Dealing costs (£)", "Stamp duty (£)", "Net realised (£)"])
    bridge = [round(float(cash["gross_gbp"].sum()), 2), round(-float(cash["dealing_gbp"].sum()), 2),
              round(-float(cash["stamp_gbp"].sum()), 2), round(float(cash["net_gbp"].sum()), 2)]
    put(summary, 9, bridge if len(main_trades) or top2.empty else ["Awaiting results"] * 4)
    header(summary, 11, ["Selected ideas", "Completed", "Skipped", "Pending", "Missing data"])
    put(summary, 12, [len(top2), counts["done"], counts["skipped"], counts["pending"], counts["nodata"]])
    period = f"{days[0]} to {days[-1]} ({len(days)} recorded session date(s))" if days else "No scan dates recorded"
    note_row(summary, 14, f"Coverage: {period}. Selected ideas = completed + skipped + pending + missing data. "
                          "Missing data is not a completed loss or a zero return; unresolved ideas are excluded from P/L.", last_col=8, chars_per_line=110)
    note_row(summary, 15, "Profitable, losing and flat use each completed trade's net pounds after dealing costs and stamp duty. "
                          "Target/stop/timeout labels describe price events and are not profit categories.", last_col=8, chars_per_line=110)
    outcomes_main = main_trades["outcome"] if not main_trades.empty else pd.Series(dtype=str)
    header(summary, 17, ["Price event", "Target", "Stop", "Timeout"])
    put(summary, 18, ["Completed trades", int((outcomes_main == "target").sum()), int((outcomes_main == "stop").sum()), int((outcomes_main == "timeout").sum())])
    note_row(summary, 20, "Currency components are rounded per trade to pennies, then totalled. Net realised pounds reconcile with Mock Trades and the final Daily pounds balance. "
                          "Open positions are not valued here. Trade fills are synthetic, based on completed candle evidence; missing intervals remain unresolved.", last_col=8, chars_per_line=110)
    note_row(summary, 22, "Use Strategy Comparison for alternative selections and holding periods. Alternatives are not additive portfolios. "
                          "A short or profitable sample does not demonstrate an edge; same-day trades may be correlated.", last_col=8, chars_per_line=110)
    widths(summary, [23, 20, 20, 20, 19, 3, 3, 3])
    summary.freeze_panes = "A5"

    # ---------------- Strategy comparison ----------------
    comparison = wb.create_sheet("Strategy Comparison", 1)
    put(comparison, 1, ["Alternative research strategies and holding periods"], bold=True)
    note_row(comparison, 2, f"The scanner finds many ideas each day. Each scenario below is a different rule for choosing which of "
                            f"those ideas to trade, so you can see which rule works best. Every scenario uses the same ideas, the same "
                            f"entry price (the next 15-minute candle's open), the same stop and target, and the same costs, so the only "
                            f"difference is the choosing rule. Figures are pounds per £{args.position_gbp:.0f} trade, after 10p dealing "
                            f"and 50p stamp duty on buys.", last_col=10, chars_per_line=140)
    header(comparison, 4, ["Scenario name", "What it does, in plain English"])
    guide = [
        ("Top 1 per day", f"Once a day at {args.pick_time}, take the scanner's single best-scoring idea. One trade a day, always at "
                          f"{args.pick_time}. Question it answers: is the scanner's number 1 idea better than an average idea?"),
        ("Top 2 per day", f"Once a day at {args.pick_time}, take the two best-scoring ideas (two different shares). Two trades a day. "
                          "This is the 'Mock Trades' sheet."),
        ("Top 2 per day, persistent", f"Like Top 2, but an idea only counts if the scanner had also flagged it in an earlier scan that "
                                      f"morning. Example: Burberry flagged at 10:00 and still flagged at {args.pick_time} qualifies; an idea "
                                      f"seen for the first time at {args.pick_time} does not. Up to two trades a day, sometimes none. "
                                      "Question: do ideas that have lasted a while do better than brand-new ones?"),
        (LIVE, "No fixed time: takes a trade only when a scan finds an idea that passes every entry rule (3 touches, a "
               "rejection candle or a fresh reclaim, and a target worth at least 1.5x the possible loss after costs). At "
               "most one new trade per scan and 5 a day (3 a day before 23 Oct 2026); FTSE 100 shares only from 23 Oct 2026; some days "
               "there may be none. This is "
               "the 'Live Mock Trades' sheet."),
        (POOL, "Every idea the scanner found, as if you had traded all of them (each idea once a day). This is the yardstick: a "
               "choosing rule is only useful if its average beats this one."),
    ]
    for offset, (name, text) in enumerate(guide, 5):
        put(comparison, offset, [name], bold=True)
        comparison.cell(offset, 2, text).font = Font(name="Arial")
        comparison.merge_cells(start_row=offset, start_column=2, end_row=offset, end_column=10)
        comparison.cell(offset, 2).alignment = Alignment(wrap_text=True, vertical="top")
        comparison.cell(offset, 1).alignment = Alignment(wrap_text=True, vertical="top")
        comparison.row_dimensions[offset].height = 15 * max(2, math.ceil(len(text) / 120))
    row = 5 + len(guide) + 1
    note_row(comparison, row, "How to read the tables: Selected = trades the rule chose. Completed = trades with a final result. "
                              "Pending = not finished yet. Missing data = Yahoo's prices had a gap, so the result can't be worked out "
                              "(left out, not counted as a loss). Skipped = never entered because the price had already jumped past the "
                              "stop or target. Avg net per trade = average profit or loss per £100 after costs. Difference vs pool = how "
                              "much better (+) or worse (-) than trading every idea.", last_col=10, chars_per_line=140)
    note_row(comparison, row + 1, "How much to trust it: with fewer than about 100 completed trades in a row, differences between "
                                  "scenarios are mostly luck. The three tables are the same comparison with trades allowed to stay open "
                                  "for up to 1, 2 or 5 trading days; they are alternatives, so don't add them together.",
             last_col=10, chars_per_line=140)
    row += 3
    for horizon in horizons:
        put(comparison, row, [f"Hold up to {horizon} exchange session(s): core comparison"], bold=True)
        header(comparison, row+1, ["Scenario", "Selected", "Completed", "Pending", "Missing data", "Skipped", "Avg net per trade (£)", "Pool avg per trade (£)", "Difference vs pool (£)", "Result status"])
        comparison.row_dimensions[row+1].height = 36
        row += 2
        section = []
        for name, frame in scenarios.items():
            trades = trades_for(frame, horizon) if not frame.empty else pd.DataFrame()
            other = trades_for(pool, horizon) if not pool.empty else pd.DataFrame()
            totals = coverage(frame, horizon)
            flow = cash_flows(trades)
            positive = int((flow["net_gbp"] > 1e-9).sum())
            negative = int((flow["net_gbp"] < -1e-9).sum())
            avg = float(pounds(trades, "r_net_stamp", args.position_gbp).mean()) if len(trades) else None
            baseline = float(pounds(other, "r_net_stamp", args.position_gbp).mean()) if len(other) else None
            events = trades["outcome"] if len(trades) else pd.Series(dtype=str)
            state = "Recorded completed results" if len(trades) else "Awaiting results" if len(frame) else "Not available: no ideas"
            unavailable = "Awaiting results" if len(frame) else "Not available"
            put(comparison, row, [name, len(frame), totals["done"], totals["pending"], totals["nodata"], totals["skipped"],
                round(avg, 2) if avg is not None else unavailable,
                round(baseline, 2) if baseline is not None else "Not available",
                round(avg-baseline, 2) if avg is not None and baseline is not None else "Not available", state])
            section.append((name,trades,flow,positive,negative,events,state,unavailable))
            row += 1
        row += 1
        put(comparison, row, [f"Hold up to {horizon} session(s): supporting price events and realised cash"], bold=True)
        header(comparison, row+1, ["Scenario", "Completed", "Target", "Stop", "Timeout", "Profitable", "Losing", "Flat", "Net realised (£)", "Worst fall from peak (£)"])
        comparison.row_dimensions[row+1].height = 36
        row += 2
        for name,trades,flow,positive,negative,events,state,unavailable in section:
            put(comparison,row,[name,len(trades),int((events=="target").sum()),int((events=="stop").sum()),int((events=="timeout").sum()),
                positive,negative,len(trades)-positive-negative,round(float(flow["net_gbp"].sum()),2) if len(trades) else unavailable,
                round(drawdown(flow["net_gbp"].tolist()),2) if len(trades) else unavailable])
            row += 1
        row += 2
    note_row(comparison, row, "Target / Stop / Timeout = how trades ended: reached the target, hit the stop, or were still open at the "
                             "end of the holding time and closed at the last price. Profitable / Losing count trades after costs, so a "
                             "trade can reach its target and still lose if the move was smaller than the costs. Worst fall from peak = "
                             "the biggest drop in running profit, adding trades up in the order they closed.", last_col=10, chars_per_line=140)
    widths(comparison, [36, 11, 12, 12, 15, 12, 22, 19, 22, 31])

    # ---------------- Breakdowns ----------------
    base = trades_for(pool, main_h) if not pool.empty else pd.DataFrame()
    overall = float(pounds(base, "r_net_stamp", args.position_gbp).mean()) if not base.empty else None
    note_row(breakdown, 1, f"Pool first observations, hold up to {main_h} exchange session(s). Figures are pounds per £{args.position_gbp:.0f} trade "
                            "after dealing costs and stamp duty. "
                            "Each section partitions the same completed pool; do not add sections together. Trades sharing market days may be correlated.", last_col=7, chars_per_line=125, bold=True)
    row = 4
    if not base.empty:
        base = base.assign(kind=base["ticker"].map(lambda t: "Trust" if kinds.get(t) == "trust" else "Company"),
            score_band=pd.cut(base["score"], [-float("inf"), 50, 60, 70, 80, 90, float("inf")], right=False,
                              labels=["under 50", "50-60", "60-70", "70-80", "80-90", "90+"]),
            touch_band=pd.cut(base["touches"], [-float("inf"), 5, 7, 10, float("inf")], right=False,
                              labels=["under 5", "5-6", "7-9", "10+"]))
        for title, column in (("Quality score", "score_band"), ("Setup", "setup"), ("Direction", "direction"),
                              ("Index", "index"), ("Meaningful touches", "touch_band"), ("Instrument type", "kind")):
            if column == "touch_band":
                cfg100, cfg250 = sc.INDEX_RULES["FTSE 100"], sc.INDEX_RULES["FTSE 250"]
                note_row(breakdown,row, f"Meaningful touches: separate zone tests at least {cfg100.min_touch_gap_bars} bars apart. Price must move away by at least "
                         f"{cfg100.rearm_move_pct*100:g}% (FTSE 100) or {cfg250.rearm_move_pct*100:g}% (FTSE 250) before a new test counts.", last_col=6, chars_per_line=120)
                row += 2
            put(breakdown, row, [title], bold=True)
            header(breakdown, row + 1, ["Group", "Completed", "Share of total", "Avg net per trade (£)", "Difference vs overall (£)", "Sample: trades / market days"])
            row += 2
            put(breakdown, row, ["Overall pool baseline", len(base), 1., round(overall, 2), 0., f"{len(base)} trades / {base['day'].nunique()} market days"], fill=NEUTRAL)
            breakdown.cell(row, 3).number_format = "0.0%"
            row += 1
            for label, group in base.groupby(column, observed=True):
                average = float(pounds(group, "r_net_stamp", args.position_gbp).mean())
                put(breakdown, row, [str(label), len(group), len(group)/len(base), round(average, 2), round(average-overall, 2),
                                     f"{len(group)} trades / {group['day'].nunique()} market days"])
                breakdown.cell(row, 3).number_format = "0.0%"
                row += 1
            row += 2
        put(breakdown, row, ["Supporting price-event counts"], bold=True)
        header(breakdown, row+1, ["Scope", "Completed", "Target", "Stop", "Timeout"])
        put(breakdown, row+2, ["Overall completed pool", len(base), int((base["outcome"]=="target").sum()),
                               int((base["outcome"]=="stop").sum()), int((base["outcome"]=="timeout").sum())])
    else:
        put(breakdown, row, ["No completed pool trades; baseline and subsets are unavailable."])
    widths(breakdown, [28, 13, 16, 16, 22, 34, 3])
    breakdown.freeze_panes = "A6"

    # ---------------- Mock trades ----------------
    fees_note = (f"Each mock trade is £{args.position_gbp:.0f} of shares bought (or sold short). "
                 f"Dealing costs = {args.cost_pct:.2f}% of the position (buy and sell together). Stamp duty = {args.stamp_duty_pct:.2f}% of the position "
                 f"on long trades in real shares (spread bets and CFDs don't pay it, and shorts never do). "
                 f"Grey rows were never entered (price had already passed the stop or target, or no trade within 30 min).")

    def mock_sheet(mock, chosen, intro, time_label):
        note_row(mock, 1, intro + fees_note, last_col=18, chars_per_line=190, fill=NEUTRAL)
        header(mock, 2, ["Date", time_label, "Rank", "Company", "Ticker", "Direction", "Setup", "Entry", "Stop", "Target",
                           "Result", "Exit time", "Amount traded (£)", "Gross profit / loss (£)", "Dealing costs (£)",
                           "Stamp duty (£)", "Net after all fees (£)", "Realised running net (£)"])
        mock.row_dimensions[2].height = 45
        top2 = chosen
        if not top2.empty:
            joined = top2.merge(outcomes[outcomes["horizon"] == main_h], on="key", how="left", suffixes=("", "_o"))
            joined = add_costs(joined.assign(fill_risk_pct=joined["fill_risk_pct"]), args.cost_pct, args.stamp_duty_pct)
            joined = realised_order(joined)
            mock_cash = cash_flows(joined)
            running = 0.0
            for number, (idx, r) in enumerate(joined.iterrows(), 3):
                status = r["status"] if isinstance(r["status"], str) else "pending"
                shown_time = str(r["run_time"])[11:16] if time_label == "Signal time" else r["as_of"]
                head = [r["day"], shown_time, int(r["rank"]), r["company"], r["ticker"], r["direction"], r["setup"]]
                if status == "done":
                    position = args.position_gbp
                    gross, dealing, stamp, net = (float(mock_cash.loc[idx, column]) for column in
                                                  ("gross_gbp", "dealing_gbp", "stamp_gbp", "net_gbp"))
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

    mock_sheet(mock, picks["Top 2 per day"],
               f"Each day the mock trader takes the top 2 ideas from the first scan available at or after {args.pick_time}. ", "Scan time")
    mock_sheet(live_sheet, picks.get(LIVE, pd.DataFrame()),
               "Live mock trader: enters ideas as they trigger during the day, one new trade per scan, up to 5 a day (3 a day before 23 Oct 2026), one per share, FTSE 100 shares only from 23 Oct 2026; only ideas that pass the entry rules, including the check that the target would pay more than the stop could cost after costs. "
               "Each entry was saved the moment the scan ran (see scan_data/live_mock_trades.csv); signal time = when that scan finished. "
               "It is filled at the open of the next 15-minute candle after the signal. ", "Signal time")

    # ---------------- Daily realised balance ----------------
    header(daily, 1, ["Date"] + [f"{n} (cumulative £)" for n in daily_names])
    daily.row_dimensions[1].height = 45
    curves = {}
    for name in daily_names:
        trades = trades_for(picks[name], main_h) if not picks[name].empty else pd.DataFrame()
        curves[name] = (cash_flows(trades)["net_gbp"].groupby(trades["realised_day"]).sum()
                        if not trades.empty else pd.Series(dtype=float))
    all_days = sorted(set().union(*[set(c.index) for c in curves.values()])) if curves else []
    cumulative = {name: curves[name].reindex(all_days, fill_value=0.0).cumsum() for name in daily_names}
    for number, day in enumerate(all_days, 2):
        put(daily, number, [day] + [round(float(cumulative[n][day]), 2) for n in daily_names])
    widths(daily, [12, 22, 22, 26, 26])
    if len(all_days) >= 2:
        chart = LineChart()
        chart.title = f"Realised paper balance (£), hold up to {main_h} day(s), after all fees"
        chart.y_axis.title, chart.x_axis.title = "Cumulative £", "Date"
        chart.add_data(Reference(daily, min_col=2, max_col=1 + len(daily_names), min_row=1, max_row=len(all_days) + 1), titles_from_data=True)
        chart.set_categories(Reference(daily, min_col=1, min_row=2, max_row=len(all_days) + 1))
        for series, colour in zip(chart.series, ("2F6DB5", "C0392B", "2E9E5B", "8E44AD")):
            series.smooth = False                      # straight lines: realised balance changes only on exits
            series.graphicalProperties.line.solidFill = colour
            series.graphicalProperties.line.width = 22000
        chart.x_axis.tickLblPos = "low"                # day labels at the bottom, not across the zero line
        chart.x_axis.delete = False                    # newer openpyxl hides axes in Excel unless told otherwise
        chart.y_axis.delete = False
        chart.height, chart.width = 9, 22
        daily.add_chart(chart, "G2")
    put(daily, len(all_days) + 4, [f"Each trade is £{args.position_gbp:.0f} of shares, after dealing costs and stamp duty. Change with --position-gbp."])

    # ---------------- All outcomes ----------------
    heads = ["Date", "Scan (as of)", "Company", "Ticker", "Type", "Index", "Setup", "Direction", "Score", "Touches", "Stop", "Target"]
    for horizon in horizons:
        heads += [f"Result, {horizon}d", f"Profit / loss per £{args.position_gbp:.0f}, {horizon}d (before costs, £)"]
    header(allout, 1, heads)
    allout.row_dimensions[1].height = 45
    by_h = {h: outcomes[outcomes["horizon"] == h].set_index("key") for h in horizons}
    ordered_pool = pool.sort_values(["day", "as_of", "score"], ascending=[False, False, False]) if not pool.empty else pool
    for number, (_, r) in enumerate(ordered_pool.iterrows(), 2):
        values = [r["day"], r["as_of"], r["company"], r["ticker"], "Trust" if kinds.get(r["ticker"]) == "trust" else "Company",
                  r["index"], r["setup"], r["direction"], r["score"], int(r["touches"]), r["stop"], r["target"]]
        for horizon in horizons:
            rows_h = by_h[horizon]
            if r["key"] in rows_h.index:
                o = rows_h.loc[r["key"]]
                result = o["outcome"] if o["status"] == "done" else ("skipped" if o["status"] == "skipped" else "no data")
                values += [result, round(o["r_gross"] * args.position_gbp * o["fill_risk_pct"] / 100, 2) if o["status"] == "done" else None]
            else:
                values += ["pending", None]
        put(allout, number, values)
    widths(allout, [11, 10, 30, 9, 9, 10, 9, 9, 7, 8, 9, 9] + [11, 12] * len(horizons))
    allout.freeze_panes = "A2"
    money_format = '"£"#,##0.00;[Red]-"£"#,##0.00;"£"0.00'
    for row_idx, cols in ((6,[1]),(9,[1,2,3,4])):
        for col in cols:
            summary.cell(row_idx,col).number_format=money_format
    for col in range(14, 14 + 2 * len(horizons), 2):
        for row_idx in range(2, allout.max_row + 1):
            if isinstance(allout.cell(row_idx, col).value, (float, int)):
                allout.cell(row_idx, col).number_format = money_format
    for ws in (mock,live_sheet,daily):
        start_row=2 if ws is daily else 3
        currency_cols=range(2, 2 + len(daily_names)) if ws is daily else range(13,19)
        for row_idx in range(start_row,ws.max_row+1):
            for col in currency_cols:
                if isinstance(ws.cell(row_idx,col).value,(float,int)):
                    ws.cell(row_idx,col).number_format=money_format
    for ws in (comparison,breakdown):
        for row_idx in range(1,ws.max_row+1):
            for col in range(1,ws.max_column+1):
                cell=ws.cell(row_idx,col)
                if not isinstance(cell.value,(float,int)):
                    continue
                if ws is breakdown:
                    block_header=next((r for r in range(row_idx-1,0,-1) if ws.cell(r,1).value in ("Group","Scope")),None)
                    label=str(ws.cell(block_header,col).value or "") if block_header else ""
                    if label in ("Completed","Target","Stop","Timeout"):
                        cell.number_format="0"
                    elif "(£)" in label:
                        cell.number_format=money_format
                elif ws is comparison:
                    # Locate the current block header above this data row.
                    previous=next((r for r in range(row_idx-1,0,-1) if ws.cell(r,1).value=="Scenario"),None)
                    if previous:
                        label=str(ws.cell(previous,col).value or "")
                        if "(£)" in label:cell.number_format=money_format
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
    runs = sc._read_table(data_dir / "runs.csv", sc.RUN_COLUMNS)
    picks = build_picks(candidates, args.pick_time, runs)
    live = read_live(data_dir / LIVE_FILENAME)
    if not live.empty:
        trade_no = dict(zip(live["key"], pd.to_numeric(live["trade_no"], errors="coerce")))
        chosen = candidates[candidates["key"].isin(trade_no)]
        picks[LIVE] = chosen.assign(rank=chosen["key"].map(trade_no).fillna(0).astype(int))
    needed = pd.concat([pool] + [p for p in picks.values() if not p.empty]).drop_duplicates("key")
    print(f"{len(candidates)} logged setups on {candidates['day'].nunique()} day(s); "
          f"{len(pool)} first sightings, {len(needed)} to follow up.")

    out_path = data_dir / CACHE_FILENAME
    if (data_dir / "outcomes.csv").exists():
        print("Original outcomes.csv retained as superseded v1 baseline; corrected results use " + CACHE_FILENAME)
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
            stats = summarise(merged)
            net = pounds(merged, "r_net_stamp", args.position_gbp)
            print(f"\nMock trader (top 2 per day, hold up to {args.horizon}d, £{args.position_gbp:.0f} per trade): {stats['n']} trades, "
                  f"{pct(stats['target'])} hit target, average £{net.mean():.2f} per trade after all fees, total £{net.sum():.2f}.")
            if stats["n"] < 30:
                print("That is far too few trades to conclude anything.")
    print(f"\nWorkbook updated: {saved}")


if __name__ == "__main__":
    main()
