"""Scan-limit backtest: don't fill all the slots at once (plan step 2a, 30 Oct 2026).

With realistic timing (Yahoo's prices ~15 minutes late) 52% of the live trader's trades fill at 08:45, straight
after the first usable scan, and 3+ trades in the same direction filled together averaged -24.9p a trade against
-8.9p for the rest. This replays the live trader (FTSE 100, live rules, fill check, up to 5 open and 10 a day,
£100 a trade, 10p costs) with the 15-minute data delay and compares variants set in advance:

  A. Current rules
  B. At most 2 new trades per scan
  C. At most 1 new trade per scan
  D. At most 2 new trades in the same direction per scan
  E. At most 3 trades open in the same direction at once

Holding back trades at one scan frees places for later scans, so only a full replay shows the effect. A variant
"helps" only if its average per trade beats A in both halves. Paper research only.

    python ftse_scan_limit_backtest.py --out results/ftse_scan_limits.xlsx
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook

import ftse_backtest as bt
import ftse_entry_check_backtest as ec
import ftse_feasibility_backtest as fb
import ftse_model_check_backtest as mc
import ftse_outcomes as out
import ftse_trade_scanner as sc

VARIANTS = {
    "A. Current rules": {},
    "B. At most 2 new trades per scan": {"per_scan": 2},
    "C. At most 1 new trade per scan": {"per_scan": 1},
    "D. At most 2 new trades in the same direction per scan": {"same_direction_per_scan": 2},
    "E. At most 3 trades open in the same direction at once": {"same_direction_open": 3},
}


def live_trader(ideas: pd.DataFrame, intraday, now, check=ec.half_left, per_scan: int | None = None,
                same_direction_per_scan: int | None = None, same_direction_open: int | None = None) -> pd.DataFrame:
    """ftse_entry_check_backtest.live_trader with the scan limits added. Only what is observable at each scan."""
    taken = []
    for _, day in ideas.groupby("day"):
        tickers, trades = set(), []
        for run_time, scan in day.groupby("run_time", sort=True):
            now_scan = out.market_stamp(run_time)
            open_trades = []
            for previous in trades:
                progress = ec.simulate_checked(previous, intraday, now_scan, check)
                closed = (progress["status"] == "done" and progress["exit_time"]
                          and pd.Timestamp(progress["exit_time"]) <= now_scan)
                rejected = (progress["status"] == "skipped" and progress.get("observed_at")
                            and pd.Timestamp(progress["observed_at"]) <= now_scan)
                if not (closed or rejected):
                    open_trades.append(previous)
            slots = min(mc.MAX_OPEN - len(open_trades), mc.MAX_PER_DAY - len(trades))
            if per_scan is not None:
                slots = min(slots, per_scan)
            new_by_direction: dict[str, int] = {}
            open_by_direction: dict[str, int] = {}
            for previous in open_trades:
                open_by_direction[previous["direction"]] = open_by_direction.get(previous["direction"], 0) + 1
            for _, row in scan.sort_values("score", ascending=False).iterrows():
                if slots <= 0:
                    break
                if row["ticker"] in tickers:
                    continue
                side = row["direction"]
                if same_direction_per_scan is not None and new_by_direction.get(side, 0) >= same_direction_per_scan:
                    continue
                if same_direction_open is not None and open_by_direction.get(side, 0) >= same_direction_open:
                    continue
                result = {**row.to_dict(), **ec.simulate_checked(row, intraday, now, check)}
                tickers.add(row["ticker"])
                trades.append(result)
                taken.append(result)
                new_by_direction[side] = new_by_direction.get(side, 0) + 1
                open_by_direction[side] = open_by_direction.get(side, 0) + 1
                slots -= 1
    return pd.DataFrame(taken)


def summary(name: str, trades: pd.DataFrame, split: str, days: int) -> dict:
    row = fb.rule_summary(name, trades, split, days)
    done = ec.money(trades) if not trades.empty else trades
    if done is not None and len(done):
        fills = done.apply(fb.fill_candle, axis=1)
        row["Filled at 08:45"] = float((fills.dt.strftime("%H:%M") == "08:45").mean())
        clock = fills.dt.strftime("%H:%M")
        row["Filled after 12:00"] = float((clock >= "12:00").mean())
    return row


def run(args) -> int:
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.kind != "trust" and c.index == "FTSE 100"]
    if args.limit:
        universe = universe[:args.limit]
    tickers = [c.yahoo_ticker for c in universe]
    print(f"Downloading 15-minute candles for {len(tickers)} FTSE 100 shares...")
    intraday = {t: sc.drop_untraded_bars(sc.completed_candles(f, "15m")) for t, f in sc.fetch_many(tickers, "15m", "60d").items()}
    intraday = {t: f for t, f in intraday.items() if len(f) > 100}
    ideas = fb.replay(universe, intraday, 15, args.warmup_days, args.workers, args.data_delay)
    if ideas.empty:
        print("No ideas passed the live rules.")
        return 1
    days = sorted(ideas["day"].unique())
    split = days[len(days) // 2]
    now = pd.Timestamp.now(tz=sc.UK_TZ)
    rows = []
    for name, limits in VARIANTS.items():
        trades = live_trader(ideas, intraday, now, **limits)
        rows.append(summary(name, trades, split, len(days)))
        print(f"  scored: {name}")
    table = pd.DataFrame(rows)
    verdict = [f"FTSE 100, {days[0]} to {days[-1]} ({len(days)} trading days), 15-minute data delay "
               f"{'on' if args.data_delay else 'off'}; split at {split}."]
    verdict += fb.rule_verdicts(table)
    wb = Workbook()
    ws = wb.active
    ws.title = "Verdict"
    ws.cell(1, 1, "Scan limits: don't fill all the slots at once").font = bt.Font(name="Arial", bold=True, size=13)
    for n, line in enumerate(verdict, 3):
        bt.note(ws, n, line)
    ws.column_dimensions["A"].width = 150
    fb.write_sheet(wb, "Variants", table, ["Each variant replayed through the live trader. 'Helps' only if better in both halves."],
                   pct=["Won after costs", "Stop hit", "Filled at 08:45", "Filled after 12:00"])
    saved = sc.save_workbook(wb, Path(args.out))
    Path(args.out).with_suffix(".json").write_text(json.dumps(
        {"generated": datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M"), "verdict": verdict,
         "variants": table.where(table.notna(), None).to_dict("records")}, indent=1, default=str))
    for line in verdict:
        print(line)
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan-limit backtest (plan step 2a)")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--warmup-days", type=int, default=12)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--data-delay", type=int, default=15)
    parser.add_argument("--out", default="results/ftse_scan_limits.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
