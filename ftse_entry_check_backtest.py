"""Entry check and daily trend filter backtest (27 Oct 2026; ideas register 30 and 31).

Phil's points from 27 Oct:
  * Pearson's stop was planned 0.44% away, but the trade filled at the next candle's open, which left
    only 0.10% to the stop: stopped out in 30 minutes, then the share fell 3.4%.
  * Croda and Segro were sells while the shares had been rising all year.

This replays the live system (FTSE 100 shares, every 15-minute scan, the scanner's rules with the target
cap and spread-bet costs, up to 5 trades open at once and 10 a day) over Yahoo's last ~60 days, and
compares:

  A. Current live rules
  B. Fill check (half): at the actual entry price, skip the trade if less than half of the planned
     stop distance is left
  C. Fill check (0.2%): skip the trade if less than 0.2% is left to the stop at the actual entry price
  D. Daily trend filter: buys only above the share's 50-day average close, sells only below
     (average of the 50 closes before the signal day)
  E. B + D
  F. C + D

A skipped trade frees its place as soon as the entry candle opens (the same moment the live trader would
know). Costs: 0.10% of £100 (spread bet). Compared on profit per £1 at risk and per £100 trade.

    python ftse_entry_check_backtest.py --out results/ftse_entry_check.xlsx
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook

import ftse_backtest as bt
import ftse_model_check_backtest as mc
import ftse_outcomes as out
import ftse_trade_scanner as sc

COST_PCT = 0.10
STAKE = 100.0
VARIANTS = ["A. Current live rules", "B. Fill check: at least half the planned stop left",
            "C. Fill check: at least 0.2% left to the stop", "D. Daily trend filter (50-day average)",
            "E. Fill check (half) + daily trend", "F. Fill check (0.2%) + daily trend"]


def half_left(row, result) -> bool:
    return result["fill_risk_pct"] >= 0.5 * float(row["stop_pct"])


def two_tenths_left(row, result) -> bool:
    return result["fill_risk_pct"] >= 0.2


def room_at_least(pct: float):
    """Fill check (half the planned room) and at least `pct`% room to the stop at the entry price."""
    def check(row, result) -> bool:
        return half_left(row, result) and result["fill_risk_pct"] >= pct
    check.__name__ = f"room_at_least_{pct}"
    return check


MIN_STOP_VARIANTS = ["A. Live rules from 28 Oct (fill check, half)", "B. + at least 0.5% room at entry",
                     "C. + at least 0.6% room at entry", "D. + at least 0.7% room at entry",
                     "E. + at least 0.8% room at entry"]


def simulate_checked(row, intraday, now, check) -> dict:
    """The live simulation, plus the fill check: a trade whose stop is too close at the fill is never entered."""
    result = mc.simulate_one(row, intraday, now)
    fill_known = isinstance(result["fill"], float) and math.isfinite(result["fill"]) and \
        isinstance(result["fill_risk_pct"], float) and math.isfinite(result["fill_risk_pct"])
    if check is not None and fill_known and result["status"] in ("done", "pending") and not check(row, result):
        signal_close = out.market_stamp(row["data_time"]) + pd.Timedelta(minutes=15)
        filled_at = max(signal_close, out.market_stamp(row["run_time"])).ceil("15min")
        result = {**result, "status": "skipped", "outcome": "thin_stop", "observed_at": filled_at.isoformat()}
    return result


def live_trader(ideas: pd.DataFrame, intraday, now, check=None) -> pd.DataFrame:
    """As ftse_model_check_backtest.live_trader (only what is observable at each scan), with the fill check."""
    taken = []
    for _, day in ideas.groupby("day"):
        tickers, trades = set(), []
        for run_time, scan in day.groupby("run_time", sort=True):
            now_scan = out.market_stamp(run_time)
            still_open = 0
            for previous in trades:
                progress = simulate_checked(previous, intraday, now_scan, check)
                closed = (progress["status"] == "done" and progress["exit_time"]
                          and pd.Timestamp(progress["exit_time"]) <= now_scan)
                rejected = (progress["status"] == "skipped" and progress.get("observed_at")
                            and pd.Timestamp(progress["observed_at"]) <= now_scan)
                still_open += not (closed or rejected)
            slots = min(mc.MAX_OPEN - still_open, mc.MAX_PER_DAY - len(trades))
            for _, row in scan.sort_values("score", ascending=False).iterrows():
                if slots <= 0:
                    break
                if row["ticker"] in tickers:
                    continue
                result = {**row.to_dict(), **simulate_checked(row, intraday, now, check)}
                tickers.add(row["ticker"])
                trades.append(result)
                taken.append(result)
                slots -= 1
    return pd.DataFrame(taken)


def add_daily_trend(ideas: pd.DataFrame, closes: dict[str, pd.Series]) -> pd.DataFrame:
    flags = []
    for _, row in ideas.iterrows():
        series = closes.get(row["ticker"])
        sma = bt.sma_before(series, pd.Timestamp(row["day"]), 50) if series is not None else None
        flags.append(None if sma is None else (row["entry"] > sma if row["direction"] == "Long" else row["entry"] < sma))
    return ideas.assign(daily_trend_ok=flags)


def money(trades: pd.DataFrame) -> pd.DataFrame:
    done = trades[trades["status"] == "done"].copy() if not trades.empty else trades
    if done.empty:
        return done
    done["net_gbp"] = ((done["r_gross"] * done["fill_risk_pct"] - COST_PCT) * STAKE / 100).round(2)
    done["at_risk_gbp"] = done["fill_risk_pct"] * STAKE / 100
    return done


def summary(name: str, trades: pd.DataFrame, days: int, split: str) -> dict:
    done = money(trades)
    skipped = int((trades["outcome"] == "thin_stop").sum()) if not trades.empty else 0
    if done.empty:
        return {"Rules": name, "Trades": 0, "Skipped by fill check": skipped}
    per_risk = lambda part: part["net_gbp"].sum() / part["at_risk_gbp"].sum() if len(part) and part["at_risk_gbp"].sum() > 0 else None
    daily = done.groupby("day")["net_gbp"].sum()
    return {"Rules": name, "Trades": len(done), "Trades a day": len(done) / max(1, days),
            "Skipped by fill check": skipped, "Won after costs": (done["net_gbp"] > 0).mean(),
            "Target hit": (done["outcome"] == "target").mean(), "Stop hit": (done["outcome"] == "stop").mean(),
            "Avg stop at entry": done["fill_risk_pct"].mean() / 100,
            "Average per £100 trade (£)": done["net_gbp"].mean(), "Total (£)": done["net_gbp"].sum(),
            "Profit per £1 at risk (£)": per_risk(done),
            "Buys: per £1 at risk (£)": per_risk(done[done["direction"] == "Long"]),
            "Sells: per £1 at risk (£)": per_risk(done[done["direction"] == "Short"]),
            "First half: per £1 at risk (£)": per_risk(done[done["day"] < split]),
            "Second half: per £1 at risk (£)": per_risk(done[done["day"] >= split]),
            "Profitable days": (daily > 0).mean()}


def run(args) -> int:
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.kind != "trust" and c.index == "FTSE 100"]
    if args.limit:
        universe = universe[:args.limit]
    tickers = [c.yahoo_ticker for c in universe]
    print(f"Downloading 15-minute and daily candles for {len(tickers)} shares...")
    raw = sc.fetch_many(tickers, "15m", "60d")
    intraday = {t: sc.drop_untraded_bars(sc.completed_candles(f, "15m")) for t, f in raw.items()}
    intraday = {t: f for t, f in intraday.items() if len(f) > 100}
    daily_raw = sc.fetch_many(tickers, "1d", "1y")
    closes = {t: f["close"].astype(float) for t, f in daily_raw.items() if "close" in f}
    ideas = mc.replay_shares(universe, intraday, 15, args.warmup_days, args.workers)
    if ideas.empty:
        print("No ideas passed the live rules.")
        return 1
    ideas = add_daily_trend(ideas, closes)
    trend = ideas["daily_trend_ok"].fillna(False).astype(bool)
    days = sorted(ideas["day"].unique())
    split = days[len(days) // 2]
    now = pd.Timestamp.now(tz=sc.UK_TZ)
    if args.test == "min-stop":
        plans = [(MIN_STOP_VARIANTS[0], ideas, half_left)] + [
            (name, ideas, room_at_least(pct)) for name, pct in zip(MIN_STOP_VARIANTS[1:], (0.5, 0.6, 0.7, 0.8))]
    else:
        plans = [(VARIANTS[0], ideas, None), (VARIANTS[1], ideas, half_left), (VARIANTS[2], ideas, two_tenths_left),
                 (VARIANTS[3], ideas[trend], None), (VARIANTS[4], ideas[trend], half_left), (VARIANTS[5], ideas[trend], two_tenths_left)]
    rows, frames = [], []
    for name, chosen, check in plans:
        trades = live_trader(chosen, intraday, now, check) if not chosen.empty else pd.DataFrame()
        rows.append(summary(name, trades, len(days), split))
        if not trades.empty:
            frames.append(money(trades).assign(rules=name))
        print(f"  scored: {name}")
    table = pd.DataFrame(rows)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.cell(1, 1, "Fill check and daily trend filter: live trader replayed").font = bt.Font(name="Arial", bold=True, size=13)
    bt.note(ws, 2, f"Generated {datetime.now(sc.UK_TZ):%Y-%m-%d %H:%M} UK time. FTSE 100 shares, {days[0]} to {days[-1]} "
                   f"({len(days)} trading days). Every 15-minute scan 08:30-15:15 with the live rules (target cap, spread-bet "
                   "costs of 0.10%), up to 5 trades open at once and 10 a day, £100 a trade, closed by the end of the day.")
    if args.test == "min-stop":
        bt.note(ws, 3, "Minimum room test (27 Oct): all variants use the live fill check (skip if less than half the planned "
                       "room to the stop is left at the entry price); B-E also skip trades with less than 0.5% / 0.6% / 0.7% / "
                       "0.8% room to the stop at the entry price.")
    else:
        bt.note(ws, 3, "Fill check: at the actual entry price (the next candle's open) the trade is skipped if too little is left "
                   "to the stop: less than half the planned distance (B), or less than 0.2% (C). Daily trend: buys only "
                   "above the 50-day average close, sells only below (D).")
    bt.note(ws, 4, "Compare on 'Profit per £1 at risk'. About 48 trading days of one market period: treat differences as "
                   "hints, not proof.")
    money_cols = [c for c in table.columns if "(£)" in c]
    bt.write_table(ws, 6, table.round(4), money_cols,
                   ["Won after costs", "Target hit", "Stop hit", "Avg stop at entry", "Profitable days"])
    ws.column_dimensions["A"].width = 52
    for col in range(2, len(table.columns) + 1):
        ws.column_dimensions[bt.get_column_letter(col)].width = 13
    if frames:
        detail = pd.concat(frames)
        shown = detail[["rules", "day", "as_of", "company", "ticker", "setup", "direction", "entry", "fill", "stop", "target",
                        "stop_pct", "fill_risk_pct", "outcome", "exit_time", "net_gbp"]].copy()
        shown["setup"] = shown["setup"].replace({"Breakout": "Reclaim"})
        shown.columns = ["Rules", "Day", "Scan", "Company", "Ticker", "Setup", "Direction", "Signal price", "Entry",
                         "Stop", "Target", "Planned stop %", "Stop % at entry", "Result", "Exit", "After costs (£)"]
        sheet = wb.create_sheet("Trades")
        bt.write_table(sheet, 1, shown.round(3), ["After costs (£)"])
        sheet.freeze_panes = "A2"
        sheet.column_dimensions["A"].width = 44
    saved = sc.save_workbook(wb, Path(args.out))
    pd.set_option("display.width", 250)
    print(table[["Rules", "Trades", "Skipped by fill check", "Won after costs", "Average per £100 trade (£)",
                 "Profit per £1 at risk (£)"]].round(3).to_string(index=False))
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill check and daily trend filter backtest")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--warmup-days", type=int, default=12)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--test", choices=["fill", "min-stop"], default="fill",
                        help="fill: fill check and trend filter (27 Oct); min-stop: minimum room to the stop at entry")
    parser.add_argument("--out", default="results/ftse_entry_check.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
