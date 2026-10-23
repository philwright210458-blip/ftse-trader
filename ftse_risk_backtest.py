"""Backtest: stop distance, target distance and trade size for the live mock trader (23 Oct 2026).

Phil noticed stops ranging from 0.3% to 3.5% and targets from 1% to 10% on the same day. This replays
the live system over Yahoo's last ~60 days of 15-minute candles and compares:

  1. Current live rules (control)
  2. Skip ideas whose stop is closer than 0.5%
  3. Skip ideas whose stop is closer than one typical 15-minute candle range (ATR)
  4. Cap the target at the share's typical daily range, and re-run the "worth it after costs" check
     with the capped target (skip the idea if it now fails)
  5. The same with the target capped at half the typical daily range
  6. The same amount at risk on every trade: size each trade so hitting the stop loses £1
     (before costs), instead of £100 in every trade (largest trade £1,000)
  7. All three together: stop at least 0.5%, target capped at the daily range, £1 at risk

Replay (same as the live system since 23 Oct 2026):
  * FTSE 100 shares (no trusts), every 15-minute scan with candles completed by 08:30 ... 15:15,
    results available 2 minutes later (the live watch finishes about then);
  * the scanner's own rules: zones, setups, stale guard, conflicts, entry rules (rejection candle /
    fresh 0.5 ATR reclaim / seen-both-ways / worth it after costs), de-duplication;
  * live picker: best score first, at most 1 new trade per scan, 5 a day, one per share;
  * outcome tracker's trade rules (ftse_outcomes.simulate): fill at the next candle's open, stop
    counted first if both are hit in one candle, closed at the end of the day.

"Typical daily range" = median of (day's high - day's low) / day's open over the 10 previous
trading days, from the same 15-minute candles. ATR = the scanner's typical 15-minute candle range.

Costs: dealing 0.10% of the amount traded; stamp duty 0.5% on buys.
The fair comparison across trade sizes is "profit per £1 put at risk" (net profit / total that the
stops put at risk).

    python ftse_risk_backtest.py --out results/ftse_risk_backtest.xlsx
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook

import ftse_backtest as bt
import ftse_outcomes as out
import ftse_trade_scanner as sc
from ftse_sessions import is_session

FIRST_CUT, LAST_CUT = "08:30", "15:15"       # candles completed by these times; last entry 15:30
RUN_DELAY = pd.Timedelta(minutes=2)
MAX_PER_DAY, MAX_PER_SCAN = 5, 1
STAKE = 100.0
RISK_GBP, MAX_STAKE = 1.0, 1000.0
DEALING_PCT, STAMP_PCT = 0.10, 0.5
RANGE_DAYS = 10

CONTROL = "1. Current live rules"
MIN_STOP = "2. Stop at least 0.5% away"
MIN_STOP_ATR = "3. Stop at least 1 typical 15-min candle away"
CAP_1 = "4. Target capped at the typical daily range"
CAP_HALF = "5. Target capped at half the typical daily range"
SAME_RISK = "6. Same £1 at risk on every trade"
ALL_THREE = "7. All three (0.5% stop, daily-range target, £1 at risk)"
VARIANTS = [CONTROL, MIN_STOP, MIN_STOP_ATR, CAP_1, CAP_HALF, SAME_RISK, ALL_THREE]
SCOPES = ["Live trader (5 a day)", "Every idea passing the rules"]


# ----------------------------------------------------------------------
# Replay
# ----------------------------------------------------------------------

def scan_cuts(day: pd.Timestamp) -> list[pd.Timestamp]:
    first = pd.Timestamp(f"{day.date()} {FIRST_CUT}", tz=sc.UK_TZ)
    last = pd.Timestamp(f"{day.date()} {LAST_CUT}", tz=sc.UK_TZ)
    return list(pd.date_range(first, last, freq="15min"))


def daily_ranges(data: pd.DataFrame) -> dict[str, float]:
    """day -> typical daily range (%) from the RANGE_DAYS sessions before it (no peeking at that day)."""
    days = data.groupby(data.index.date).agg(high=("high", "max"), low=("low", "min"), open=("open", "first"))
    pct = (days["high"] - days["low"]) / days["open"] * 100
    typical = pct.rolling(RANGE_DAYS, min_periods=RANGE_DAYS).median().shift(1)
    return {str(day): float(value) for day, value in typical.items() if pd.notna(value)}


def share_setups(job) -> list[dict]:
    """Every raw setup for one share at every scan cut (run in a worker process)."""
    company, data, cuts, interval_min = job
    ranges = daily_ranges(data)
    step = pd.Timedelta(minutes=interval_min)
    ends = data.index + step
    found = []
    for cut in cuts:
        visible = data[ends <= cut]
        cfg = sc.INDEX_RULES[company.index]
        if len(visible) <= cfg.swing_lookback * 2 or visible.index[-1].date() != cut.date():
            continue
        zones = sc.qualify_zones(sc.cluster_zones(sc.find_swing_points(visible, cfg.swing_lookback), cfg.zone_tolerance_pct),
                                 visible, cfg)
        setups = sc.build_setups(company.company, company.yahoo_ticker, company.index, visible, zones, cfg)
        if not setups:
            continue
        atr = sc.typical_true_range(visible)
        for setup in setups:
            setup.kind, setup.last_trade = company.kind, visible.index[-1] + step
            setup.filter_note = sc.entry_rule_note(setup, visible)
            found.append({"cut": cut, "setup": setup, "atr": atr, "day_range": ranges.get(str(cut.date()), float("nan"))})
    return found


def replay(universe, intraday, interval_min: int, warmup_days: int, workers: int) -> pd.DataFrame:
    """Every idea that passed the live rules, scan by scan, as rows."""
    all_days = sorted({stamp.normalize() for data in intraday.values() for stamp in data.index})
    sessions = [day for day in all_days if is_session(day)][warmup_days:]
    cuts = [cut for day in sessions for cut in scan_cuts(day)]
    jobs = [(c, intraday[c.yahoo_ticker], cuts, interval_min) for c in universe if c.yahoo_ticker in intraday]
    print(f"Replaying {len(cuts)} scans over {len(sessions)} days for {len(jobs)} shares ({workers} workers)...")
    raw = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for number, rows in enumerate(pool.map(share_setups, jobs, chunksize=1), 1):
            raw.extend(rows)
            if number % 10 == 0:
                print(f"  ...{number}/{len(jobs)} shares replayed")
    by_cut: dict[pd.Timestamp, list[dict]] = {}
    for row in raw:
        by_cut.setdefault(row["cut"], []).append(row)
    rows = []
    for cut in sorted(by_cut):
        found = by_cut[cut]
        setups = [item["setup"] for item in found]
        extra = {id(item["setup"]): item for item in found}
        newest = max(s.last_trade for s in setups)
        sc.mark_stale(setups, newest, 30)
        fresh = [s for s in setups if not s.stale]
        sc.annotate_conflicts(fresh)
        sc.apply_entry_rules(fresh)
        for s in sc.dedupe_setups(fresh):
            if s.conflict:
                continue
            info = extra[id(s)]
            data_time = (newest - pd.Timedelta(minutes=interval_min)).isoformat()
            run_time = (cut + RUN_DELAY).strftime("%Y-%m-%d %H:%M:%S")
            rows.append({"day": str(cut.date()), "as_of": cut.strftime("%H:%M"), "data_time": data_time,
                         "run_time": run_time, "mode": "watch", "company": s.company, "ticker": s.ticker,
                         "index": s.index_label, "setup": s.setup_type, "direction": s.direction,
                         "score": s.quality_score, "entry": s.entry, "stop": s.stop, "target": s.target,
                         "stop_pct": abs(s.entry - s.stop) / s.entry * 100,
                         "target_pct": abs(s.target - s.entry) / s.entry * 100,
                         "atr_pct": info["atr"] / s.entry * 100, "day_range_pct": info["day_range"]})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# Variants
# ----------------------------------------------------------------------

def worth_it(direction: str, stop_pct: float, target_pct: float) -> bool:
    """The live scanner's check: target's gain after costs at least 1.5x the stop's loss after costs."""
    cost = sc.COSTS_PCT.get(direction, 0.6)
    return target_pct - cost >= sc.MIN_NET_REWARD_RISK * (stop_pct + cost)


def cap_target(frame: pd.DataFrame, share: float) -> pd.DataFrame:
    """Bring the target in to `share` x the typical daily range; drop ideas that then fail the costs check."""
    result = frame.copy()
    cap = result["day_range_pct"] * share
    capped = np.minimum(result["target_pct"], cap)
    long = result["direction"] == "Long"
    result["target"] = np.where(long, result["entry"] * (1 + capped / 100), result["entry"] * (1 - capped / 100))
    result["target_pct"] = capped
    passes = pd.Series([worth_it(d, s, t) for d, s, t in zip(result["direction"], result["stop_pct"], result["target_pct"])],
                       index=result.index, dtype=bool)
    keep = result["day_range_pct"].notna() & passes
    return result[keep]


def variant_ideas(ideas: pd.DataFrame) -> dict[str, tuple[pd.DataFrame, bool]]:
    """variant -> (ideas allowed, same-risk sizing?)."""
    wide = ideas["stop_pct"] >= 0.5
    return {CONTROL: (ideas, False),
            MIN_STOP: (ideas[wide], False),
            MIN_STOP_ATR: (ideas[ideas["stop_pct"] >= ideas["atr_pct"]], False),
            CAP_1: (cap_target(ideas, 1.0), False),
            CAP_HALF: (cap_target(ideas, 0.5), False),
            SAME_RISK: (ideas, True),
            ALL_THREE: (cap_target(ideas[wide], 1.0), True)}


def live_picks(ideas: pd.DataFrame) -> pd.DataFrame:
    """Live rule: scans in time order, best score first, 1 new trade per scan, 5 a day, one per share."""
    chosen = []
    for _, day in ideas.groupby("day"):
        taken = set()
        for _, scan in day.groupby("run_time", sort=True):
            added = 0
            for _, row in scan.sort_values("score", ascending=False).iterrows():
                if len(taken) >= MAX_PER_DAY or added >= MAX_PER_SCAN:
                    break
                if row["ticker"] in taken:
                    continue
                taken.add(row["ticker"])
                chosen.append(row)
                added += 1
    return pd.DataFrame(chosen, columns=ideas.columns)


def first_sightings(ideas: pd.DataFrame) -> pd.DataFrame:
    return ideas.sort_values(["day", "run_time", "score"], ascending=[True, True, False]).drop_duplicates(["day", "ticker", "direction"])


def simulate(frame: pd.DataFrame, intraday, now: pd.Timestamp, interval: str, same_risk: bool) -> pd.DataFrame:
    rows = []
    minutes = sc.interval_minutes(interval)
    for _, row in frame.iterrows():
        data = intraday.get(row["ticker"])
        if data is None:
            continue
        signal_close = out.market_stamp(row["data_time"]) + pd.Timedelta(minutes=minutes)
        available = max(signal_close, out.market_stamp(row["run_time"]))
        sim = out.simulate(row["direction"], float(row["stop"]), float(row["target"]), available, data, 1, now, interval)
        stake = min(MAX_STAKE, RISK_GBP / (row["stop_pct"] / 100)) if same_risk else STAKE
        rows.append({**row.to_dict(), "status": sim.status, "outcome": sim.outcome, "fill": sim.fill,
                     "exit_price": sim.exit_price, "r_gross": sim.r_gross, "fill_risk_pct": sim.fill_risk_pct, "stake": stake})
    result = pd.DataFrame(rows)
    if result.empty:
        return result
    done = result["status"] == "done"
    pct = (result["r_gross"] * result["fill_risk_pct"]).where(done)              # % move in the trade's favour
    costs_pct = DEALING_PCT + (result["direction"] == "Long") * STAMP_PCT
    result["gross_gbp"] = (pct * result["stake"] / 100).round(2)
    result["costs_gbp"] = (costs_pct * result["stake"] / 100).where(done).round(2)
    result["net_gbp"] = (result["gross_gbp"] - result["costs_gbp"]).round(2)
    result["at_risk_gbp"] = (result["fill_risk_pct"] * result["stake"] / 100).where(done)
    return result


SPLIT_DAY: str | None = None


def summary_row(variant: str, scope: str, trades: pd.DataFrame) -> dict:
    done = trades[trades["status"] == "done"] if not trades.empty else trades
    if done.empty:
        return {"Variant": variant, "Trades": "Live trader" if scope == SCOPES[0] else "Every idea", "Completed": 0}
    by_day = done.groupby("day")["net_gbp"].sum()
    per_risk = lambda part: part["net_gbp"].sum() / part["at_risk_gbp"].sum() if len(part) and part["at_risk_gbp"].sum() > 0 else None
    first, second = done[done["day"] < SPLIT_DAY], done[done["day"] >= SPLIT_DAY]
    return {"Variant": variant, "Trades": "Live trader" if scope == SCOPES[0] else "Every idea",
            "Completed": len(done), "Days with trades": int(by_day.size),
            "Target hit": (done["outcome"] == "target").mean(), "Stop hit": (done["outcome"] == "stop").mean(),
            "Closed at end of day": (done["outcome"] == "timeout").mean(),
            "Avg stop distance": done["stop_pct"].mean() / 100, "Avg target distance": done["target_pct"].mean() / 100,
            "Avg amount traded (£)": done["stake"].mean(), "Avg at risk (£)": done["at_risk_gbp"].mean(),
            "Avg profit per trade after costs (£)": done["net_gbp"].mean(), "Total after costs (£)": done["net_gbp"].sum(),
            "Profit per £1 at risk (£)": per_risk(done), "Profitable days": (by_day > 0).mean(),
            "Worst day (£)": by_day.min(),
            "Buys: profit per £1 at risk (£)": per_risk(done[done["direction"] == "Long"]),
            "Sells: profit per £1 at risk (£)": per_risk(done[done["direction"] == "Short"]),
            "First half: profit per £1 at risk (£)": per_risk(first),
            "Second half: profit per £1 at risk (£)": per_risk(second)}


# ----------------------------------------------------------------------
# Workbook and main
# ----------------------------------------------------------------------

def build_workbook(table: pd.DataFrame, trades: pd.DataFrame, period: str, generated: str) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.cell(1, 1, "Stops, targets and trade size: live mock trader replayed").font = bt.Font(name="Arial", bold=True, size=13)
    bt.note(ws, 2, f"Generated {generated}. {period}. FTSE 100 shares, every 15-minute scan 08:30-15:15, the live scanner's rules "
                   "and the live picker (1 new trade per scan, 5 a day, one per share), closed by the end of the day.")
    bt.note(ws, 3, "Variants 1-5: £100 in every trade. Variants 6-7: each trade sized so that hitting the stop loses £1 before "
                   "costs (largest £1,000). Costs: 0.10% dealing, plus 0.5% stamp duty on buys. Compare variants on 'Profit per £1 "
                   "at risk': net profit divided by what the stops put at risk.")
    bt.note(ws, 4, "Typical daily range = median high-to-low move of the previous 10 days. A capped target is re-checked with the "
                   "'worth it after costs' rule (gain at least 1.5x the possible loss), and the idea is skipped if it fails. "
                   "About 40 trading days of one market period: treat differences as hints, not proof.")
    money = [c for c in table.columns if "(£)" in c]
    pct = ["Target hit", "Stop hit", "Closed at end of day", "Avg stop distance", "Avg target distance", "Profitable days"]
    bt.write_table(ws, 6, table.round(4), money, pct)
    ws.column_dimensions["A"].width = 52
    for col in range(2, len(table.columns) + 1):
        ws.column_dimensions[bt.get_column_letter(col)].width = 13

    if not trades.empty:
        sheet = wb.create_sheet("Live trader trades")
        shown = trades[trades["scope"] == SCOPES[0]].sort_values(["variant", "day", "run_time"])
        shown = shown[["variant", "day", "as_of", "company", "ticker", "setup", "direction", "entry", "stop", "target",
                       "stop_pct", "target_pct", "day_range_pct", "outcome", "stake", "at_risk_gbp", "gross_gbp", "costs_gbp",
                       "net_gbp"]].copy()
        shown["setup"] = shown["setup"].replace({"Breakout": "Reclaim"})
        shown["direction"] = shown["direction"].map({"Long": "Buy", "Short": "Sell"})
        shown["outcome"] = shown["outcome"].replace({"target": "Target hit", "stop": "Stopped out", "timeout": "Closed at end of day"})
        shown.columns = ["Variant", "Date", "Scan", "Company", "Ticker", "Setup", "Buy/Sell", "Entry", "Stop", "Target",
                         "Stop distance %", "Target distance %", "Typical daily range %", "Result", "Amount traded (£)",
                         "At risk (£)", "Before costs (£)", "Costs (£)", "After costs (£)"]
        bt.write_table(sheet, 1, shown.round(3), [c for c in shown.columns if "(£)" in c])
        sheet.freeze_panes = "A2"
        sheet.column_dimensions["A"].width = 44
        sheet.column_dimensions["D"].width = 28
    return wb


def run(args) -> int:
    global SPLIT_DAY
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.kind != "trust" and c.index == args.index]
    if args.limit:
        universe = universe[:args.limit]
    print(f"Downloading {args.period} of {args.interval} candles for {len(universe)} shares...")
    raw = sc.fetch_many([c.yahoo_ticker for c in universe], args.interval, args.period)
    intraday = {}
    for ticker, frame in raw.items():
        frame = sc.drop_untraded_bars(sc.completed_candles(frame, args.interval))
        if len(frame) > 100:
            intraday[ticker] = frame
    if not intraday:
        print("No data downloaded (is Yahoo reachable?).")
        return 1
    ideas = replay(universe, intraday, sc.interval_minutes(args.interval), args.warmup_days, args.workers)
    if ideas.empty:
        print("No ideas passed the live rules.")
        return 1
    days = sorted(ideas["day"].unique())
    SPLIT_DAY = days[len(days) // 2]
    now = pd.Timestamp.now(tz=sc.UK_TZ)
    rows, kept = [], []
    for variant, (allowed, same_risk) in variant_ideas(ideas).items():
        for scope, chosen in ((SCOPES[0], live_picks(allowed)), (SCOPES[1], first_sightings(allowed))):
            trades = simulate(chosen, intraday, now, args.interval, same_risk) if not chosen.empty else pd.DataFrame()
            rows.append(summary_row(variant, scope, trades))
            if not trades.empty:
                kept.append(trades.assign(variant=variant, scope=scope))
        print(f"  scored: {variant}")
    table = pd.DataFrame(rows)
    table = table.sort_values("Trades", key=lambda s: s.map({"Live trader": 0, "Every idea": 1}), kind="stable")
    all_trades = pd.concat(kept) if kept else pd.DataFrame()
    period = f"{days[0]} to {days[-1]} ({len(days)} trading days after a {args.warmup_days}-day warm-up)"
    generated = datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M UK time")
    saved = sc.save_workbook(build_workbook(table, all_trades, period, generated), Path(args.out))
    pd.set_option("display.width", 250)
    print(table[["Variant", "Trades", "Completed", "Target hit", "Stop hit", "Avg profit per trade after costs (£)",
                 "Profit per £1 at risk (£)"]].round(3).to_string(index=False))
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Stop / target / trade-size backtest of the live mock trader")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--index", default="FTSE 100")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--period", default="60d")
    parser.add_argument("--warmup-days", type=int, default=12)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--out", default="results/ftse_risk_backtest.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
