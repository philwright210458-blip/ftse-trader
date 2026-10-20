"""Backtest for range model version 2 (ftse_range_v2.py) on FTSE shares and the FTSE 100 index.

    python ftse_range_backtest.py --exclude-trusts --out ftse_range_v2_backtest.xlsx

Shares: every £100 trade pays 10p dealing, plus 50p stamp duty on buys.
FTSE 100 index (^FTSE, as by spread bet or CFD): 2p spread per £100 and
2p overnight funding per £100 for each night held; no stamp duty.
Trades are held up to 3 trading days (--horizon), then closed at the last price.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

import ftse_backtest as bt
import ftse_range_v2 as v2
import ftse_trade_scanner as sc

INDEX_TICKER = "^FTSE"
INDEX_SPREAD_GBP, INDEX_NIGHT_GBP = 0.02, 0.02
INDEX = sc.Constituent("FTSE 100 index", "UKX", INDEX_TICKER, "FTSE 100", "index")
OLD_MODEL_NOTE = ("For comparison, the current model (version 1) lost 43p per £100 after costs on every idea "
                  "(3,513 ideas, 17 Aug to 19 Oct), and 5p before costs.")


def download(universe, interval: str, period: str, delay: float) -> dict[str, pd.DataFrame]:
    intraday = {}
    for number, company in enumerate(universe, 1):
        try:
            raw = sc.completed_candles(sc.fetch_data(company.yahoo_ticker, interval, period), interval)
            if company.kind == "index":
                raw["volume"] = 1          # an index has no volume; keep every candle
                intraday[company.yahoo_ticker] = raw
            else:
                intraday[company.yahoo_ticker] = sc.drop_untraded_bars(raw)
        except Exception as error:
            print(f"  could not fetch {company.yahoo_ticker}: {error}")
        if number % 25 == 0:
            print(f"  ...{number}/{len(universe)} downloaded")
        time.sleep(delay)
    return intraday


def index_costs(trades: pd.DataFrame) -> pd.DataFrame:
    """Replace share costs with spread-bet costs: spread plus funding for each night held."""
    result = trades.copy()
    done = result["status"] == "done"
    fills = pd.to_datetime(result["run_time"].str[:10])
    exits = pd.to_datetime(result["exit_time"].fillna("").str[:10], errors="coerce")
    nights = pd.Series([np.busday_count(f.date(), e.date()) if pd.notna(e) else 0 for f, e in zip(fills, exits)],
                       index=result.index)
    costs = (INDEX_SPREAD_GBP + INDEX_NIGHT_GBP * nights).where(done)
    result["costs_gbp"] = costs.round(2)
    result["net_gbp"] = (result["gross_gbp"] - costs).round(2)
    return result


def by_setup(market: str, trades: pd.DataFrame) -> list[dict]:
    rows = []
    for (setup, direction), group in trades.groupby(["setup", "direction"]):
        row = bt.summary_row(market, f"{setup}: {'buys' if direction == 'Long' else 'sells'}", group)
        rows.append(row)
    return rows


def build_workbook(summary, setups, daily, trades, period_text, generated) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.cell(1, 1, "Range model version 2 (Patrick Nill's method, rebuilt): backtest").font = Font(name="Arial", bold=True, size=13)
    bt.note(ws, 2, f"Generated {generated}. {period_text}. Each signal used only finished candles and was filled at the next "
                   "candle's open; stop counted first if stop and target fall in one candle.")
    bt.note(ws, 3, "Rules (counted in candles, so on hourly candles every pattern takes 4 times as long): a big move "
                   "(5 typical candles' worth within 8 candles), then a sideways box (at least 16 candles, 3 touches, no taller "
                   "than 60% of the move). Trades: Range (edge of the box back to the far edge), Breakout (close outside, then "
                   "a confirming close further out), Reclaim (close outside, then back inside within 3 candles). Target = next "
                   "fair-value level, taken only if at least 3x the stop distance. No new signals 13:15-14:45 or after 15:30.")
    bt.note(ws, 4, "Costs per £100: shares 10p dealing + 50p stamp duty on buys; FTSE 100 index (spread bet/CFD) 2p spread + 2p "
                   "per night held. Share sells are shown for interest only: a normal share account cannot sell short. "
                   + OLD_MODEL_NOTE)
    bt.note(ws, 5, "Caution: Yahoo keeps only 60 days of 15-minute candles, so this is one short market spell. The rules were set "
                   "once from the video, not tuned to these results.")
    money = [c for c in summary.columns if "(£)" in c]
    pct = ["Target hit", "Stop hit", "Closed at end", "Profitable days"]
    end = bt.write_table(ws, 7, summary, money, pct)
    column = summary.columns.get_loc("Avg after costs per £100 (£)") + 1 if "Avg after costs per £100 (£)" in summary else None
    for r in range(8, end):
        value = ws.cell(r, column).value if column else None
        if isinstance(value, (int, float)):
            ws.cell(r, column).fill = PatternFill("solid", fgColor=bt.GOOD if value > 0 else bt.BAD)
    for number, width in enumerate([26, 30, 11, 9, 9, 9, 10, 13, 13, 13, 10, 11, 11, 13, 13, 12, 12], 1):
        ws.column_dimensions[get_column_letter(number)].width = width
    ws.freeze_panes = "C8"

    setup_ws = wb.create_sheet("By setup")
    bt.note(setup_ws, 1, "Every signal split by trade type and direction. Use this to see which of Patrick's three trades "
                         "carries the results.")
    bt.write_table(setup_ws, 3, setups, [c for c in setups.columns if "(£)" in c], pct)
    for number in range(1, len(setups.columns) + 1):
        setup_ws.column_dimensions[get_column_letter(number)].width = 26 if number <= 2 else 12

    days_ws = wb.create_sheet("Daily")
    bt.note(days_ws, 1, "Profit or loss after costs per signal day (the day each trade was entered).", last_col=8)
    bt.write_table(days_ws, 3, daily, [c for c in daily.columns if "(£)" in c])
    for number in range(1, len(daily.columns) + 1):
        days_ws.column_dimensions[get_column_letter(number)].width = 14 if number > 1 else 12

    for title, frame in trades.items():
        trade_ws = wb.create_sheet(title)
        if frame.empty:
            trade_ws.cell(1, 1, "No trades.")
            continue
        columns = {"day": "Date", "as_of": "Signal candle closed", "company": "Company", "ticker": "Ticker",
                   "setup": "Setup", "direction": "Direction", "score": "Target ÷ stop distance", "fill": "Entry (filled)",
                   "stop": "Stop", "target": "Target", "outcome": "Result", "exit_time": "Exit time",
                   "gross_gbp": "Before costs (£)", "costs_gbp": "Costs (£)", "net_gbp": "After costs (£)"}
        shown = frame[[c for c in columns if c in frame.columns]].rename(columns=columns)
        bt.write_table(trade_ws, 1, shown, ["Before costs (£)", "Costs (£)", "After costs (£)"])
        trade_ws.freeze_panes = "A2"
    return wb


def run(args) -> int:
    universe = sc.load_universe(Path(args.universe))
    if args.exclude_trusts:
        universe = [c for c in universe if c.kind != "trust"]
    if args.limit:
        universe = universe[:args.limit]
    if getattr(args, "index_only", False):
        universe = []
    minutes = sc.interval_minutes(args.interval)
    # Hourly candles: enter at the open of the next candle, which starts the moment the signal candle closes.
    delay = v2.AVAILABLE_DELAY if minutes <= 15 else pd.Timedelta(0)
    print(f"Downloading {len(universe)} shares and the FTSE 100 index ({args.period} of {args.interval} candles)...")
    intraday = download(universe + [INDEX], args.interval, args.period, args.delay)
    shares = [c for c in universe if c.yahoo_ticker in intraday]
    if not shares and INDEX_TICKER not in intraday:
        print("No data downloaded (is Yahoo reachable?).")
        return 1
    all_days = sorted({str(s.date()) for d in intraday.values() for s in d.index})
    replay_days = set(all_days[args.warmup_days:])
    now = max(d.index[-1] for d in intraday.values()) + pd.Timedelta(minutes=minutes)

    print("Finding signals...")
    share_rows = [row for c in shares for row in v2.signals(c, intraday[c.yahoo_ticker], minutes, replay_days, delay=delay)]
    index_rows = v2.signals(INDEX, intraday.get(INDEX_TICKER), minutes, replay_days, delay=delay)
    share_sig = bt.with_keys(pd.DataFrame(share_rows)) if share_rows else pd.DataFrame()
    index_sig = bt.with_keys(pd.DataFrame(index_rows)) if index_rows else pd.DataFrame()
    print(f"  {len(share_sig)} share signals, {len(index_sig)} index signals")

    scenarios = []
    if not share_sig.empty:
        buys = share_sig[share_sig["direction"] == "Long"]
        scenarios += [("FTSE shares", "Every signal", share_sig, False),
                      ("FTSE shares", "Buys only (normal share account)", buys, False),
                      ("FTSE shares", "Buys only, first 3 per day", bt.live_picks(buys) if not buys.empty else buys, False)]
    if not index_sig.empty:
        scenarios += [("FTSE 100 index", "Every signal", index_sig, True),
                      ("FTSE 100 index", "Buys only", index_sig[index_sig["direction"] == "Long"], True)]

    summary, setup_rows, daily_cols, trade_sheets = [], [], {}, {}
    for market, scenario, chosen, is_index in scenarios:
        print(f"Scoring: {market}, {scenario}...")
        if chosen.empty:
            summary.append({"Variant": market, "Scenario": scenario, "Completed trades": 0})
            continue
        trades = bt.score(chosen, intraday, args.horizon, now, args.interval)
        if trades.empty:
            summary.append({"Variant": market, "Scenario": scenario, "Completed trades": 0})
            continue
        if is_index:
            trades = index_costs(trades)
        summary.append(bt.summary_row(market, scenario, trades))
        done = trades[trades["status"] == "done"]
        daily_cols[f"{market}: {scenario} (£)"] = done.groupby("day")["net_gbp"].sum()
        if scenario == "Every signal":
            setup_rows += by_setup(market, trades)
            trade_sheets[f"Trades ({'index' if is_index else 'shares'})"] = trades

    summary_frame = pd.DataFrame(summary).rename(columns={"Variant": "Market"})
    setup_frame = pd.DataFrame(setup_rows).rename(columns={"Variant": "Market", "Scenario": "Setup"})
    daily_frame = pd.DataFrame(daily_cols).sort_index().round(2)
    daily_frame.index.name = "Date"
    daily_frame = daily_frame.reset_index()
    days = sorted(replay_days)
    what = f"{len(shares)} shares plus the FTSE 100 index" if shares else "FTSE 100 index only"
    period_text = (f"History replayed: {days[0]} to {days[-1]} ({len(days)} trading days, {what}, {args.interval} candles); "
                   f"each trade held up to {args.horizon} trading day(s)") if days else "No days replayed"
    for frame in trade_sheets.values():
        frame.sort_values(["day", "run_time"], inplace=True)
    generated = datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M UK time")
    workbook = build_workbook(summary_frame, setup_frame, daily_frame,
                              {"Trades (shares)": trade_sheets.get("Trades (shares)", pd.DataFrame()),
                               "Trades (index)": trade_sheets.get("Trades (index)", pd.DataFrame())},
                              period_text, generated)
    saved = sc.save_workbook(workbook, Path(args.out))
    print(f"\n{period_text}")
    cols = [c for c in ["Market", "Scenario", "Completed trades", "Target hit", "Avg before costs per £100 (£)",
                        "Avg after costs per £100 (£)", "Total after costs (£)"] if c in summary_frame.columns]
    print(summary_frame[cols].to_string(index=False))
    if not setup_frame.empty:
        setup_cols = ["Market", "Setup", "Completed trades", "Target hit", "Avg before costs per £100 (£)",
                      "Avg after costs per £100 (£)"]
        print(setup_frame[[c for c in setup_cols if c in setup_frame.columns]].to_string(index=False))
    print(f"\nWorkbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest range model version 2 on FTSE shares and the FTSE 100 index")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--period", default="60d")
    parser.add_argument("--warmup-days", type=int, default=5,
                        help="Trading days at the start used only as history (default: 5)")
    parser.add_argument("--horizon", type=int, default=3, help="Longest hold in trading days (default: 3)")
    parser.add_argument("--exclude-trusts", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--index-only", action="store_true", help="Test only the FTSE 100 index")
    parser.add_argument("--delay", type=float, default=0.2)
    parser.add_argument("--out", default="ftse_range_v2_backtest.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
