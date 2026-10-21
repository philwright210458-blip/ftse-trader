"""MA-zone pullback with a trailing exit (ideas register 22, follow-up), daily candles, per £250 trade.

    python ftse_ma_trailing_backtest.py --index "FTSE 100" --out results/ftse_ma_zone_trailing_ftse100.xlsx

Same entries as ftse_ma_daily_backtest.py (dip into the 9 EMA / 20 SMA zone in
an uptrend, buy at the next open, protective stop below the dip). Two exits are
compared on exactly the same entries:

* Fixed target: sell at 2x the stop distance, or after 20 trading days.
* Trailing: no target. Hold while the trend lasts; sell at the next day's open
  after a day closes below the 20 SMA (the protective stop still applies, and
  anything still open after MAX_HOLD trading days is sold at the close).

Results are pounds per £250 trade under three cost cases (set from the brokers'
published charges, 21 Oct 2026):
* IG share dealing: £0 commission, 0.1% price gap (25p), 0.5% stamp duty
  (£1.25) on the buy = £1.50 a trade.
* No stamp duty (e.g. a UK-listed ETF or other stamp-free route): 25p a trade.
* IG spread bet: 0.10% spread (25p) plus overnight funding of about 6.5% a year
  (benchmark rate + 2.5%), about 4.5p per night on £250.

Caution: today's FTSE 100 list (companies that dropped out are missing), and
dividends are not added. Both flatter or understate buys slightly; neither
favours one exit over the other.
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font

import ftse_backtest as bt
import ftse_ma_daily_backtest as db
import ftse_ma_zone as mz
import ftse_trade_scanner as sc

STAKE = 250.0
SHARE_COSTS = 0.25 + 1.25                 # IG share dealing: price gap + stamp duty
NO_STAMP_COSTS = 0.25                     # e.g. UK-listed ETF
SPREAD_BET_SPREAD = 0.25
FUNDING_PER_NIGHT = STAKE * 0.065 / 365   # about 4.5p a night
FIXED_HOLD, MAX_HOLD = 20, 250


def trail(frame: pd.DataFrame, ind: pd.DataFrame, signals: pd.DataFrame, max_hold: int = MAX_HOLD) -> list[dict]:
    """Trailing-exit trades for one share, one open trade at a time."""
    trades, busy_until = [], -1
    position = {stamp: n for n, stamp in enumerate(frame.index)}
    o, l, c = (frame[k].to_numpy() for k in ("open", "low", "close"))
    sma = ind["sma20"].to_numpy()
    for stamp, row in signals.iterrows():
        n = position[stamp]
        if n <= busy_until or n + 1 >= len(frame):
            continue
        fill, stop = o[n + 1], float(row["stop"])
        if not (math.isfinite(fill) and fill > stop):
            continue
        outcome, exit_price, exit_n = None, None, None
        last = n + max_hold
        for k in range(n + 1, min(last, len(frame) - 1) + 1):
            if l[k] <= stop:
                outcome, exit_price, exit_n = "stop", min(o[k], stop), k
                break
            if c[k] < sma[k]:                                   # trend broken: sell at the next open
                if k + 1 >= len(frame):
                    break
                outcome, exit_price, exit_n = "closed below 20 SMA", o[k + 1], k + 1
                break
        if outcome is None:
            if last >= len(frame):
                continue                                        # still open at the end of the data
            outcome, exit_price, exit_n = "held to the limit", c[last], last
        trades.append(record(frame, stamp, n, fill, stop, exit_price, exit_n, outcome, row))
        busy_until = exit_n
    return trades


def fixed(frame: pd.DataFrame, signals: pd.DataFrame) -> list[dict]:
    """The earlier fixed-target exit, converted to the same record format."""
    rows = []
    position = {stamp: n for n, stamp in enumerate(frame.index)}
    for t in db.simulate(frame, signals, FIXED_HOLD):
        stamp = pd.Timestamp(t["signal"])
        n = position[stamp]
        exit_n = n + t["days"]
        rows.append(record(frame, stamp, n, t["fill"], t["stop"], t["exit"], exit_n, t["outcome"],
                           {"slope": t["slope"]}))
    return rows


def record(frame, stamp, n, fill, stop, exit_price, exit_n, outcome, row) -> dict:
    nights = int((frame.index[exit_n] - frame.index[n + 1]).days)
    gross = (exit_price - fill) / fill * STAKE
    return {"signal": stamp.date(), "entry_day": frame.index[n + 1].date(), "exit_day": frame.index[exit_n].date(),
            "days": exit_n - n, "nights": nights, "fill": round(float(fill), 4), "stop": round(float(stop), 4),
            "exit": round(float(exit_price), 4), "outcome": outcome, "slope": round(float(row["slope"]), 4),
            "gross": round(gross, 2), "net_shares": round(gross - SHARE_COSTS, 2),
            "net_no_stamp": round(gross - NO_STAMP_COSTS, 2),
            "net_spread_bet": round(gross - SPREAD_BET_SPREAD - FUNDING_PER_NIGHT * nights, 2)}


def summary(name: str, exit_name: str, trades: pd.DataFrame, split) -> dict:
    if trades.empty:
        return {"Strategy": name, "Exit": exit_name, "Trades": 0}
    entry = pd.to_datetime(trades["entry_day"])
    yearly = trades.groupby(entry.dt.year)["net_shares"].sum()
    best = trades["gross"].clip(lower=0)
    top = best.sort_values(ascending=False)
    share_top = top.head(max(1, len(top) // 10)).sum() / best.sum() if best.sum() > 0 else float("nan")
    return {"Strategy": name, "Exit": exit_name, "Trades": len(trades), "Avg days held": trades["days"].mean(),
            "Made money (IG shares)": (trades["net_shares"] > 0).mean(),
            "Before costs per £250 (£)": trades["gross"].mean(),
            "After costs: IG shares (£)": trades["net_shares"].mean(),
            "After costs: no stamp duty (£)": trades["net_no_stamp"].mean(),
            "After costs: IG spread bet (£)": trades["net_spread_bet"].mean(),
            "IG shares, first half (£)": trades.loc[entry < split, "net_shares"].mean(),
            "IG shares, second half (£)": trades.loc[entry >= split, "net_shares"].mean(),
            "Years in profit (IG shares)": f"{int((yearly > 0).sum())} of {len(yearly)}",
            "Share of gains from best 10% of trades": share_top}


def run(args) -> int:
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.kind != "trust"]
    if args.index:
        universe = [c for c in universe if c.index == args.index]
    if args.limit:
        universe = universe[:args.limit]
    names = {c.yahoo_ticker: c.company for c in universe}
    print(f"Downloading {args.period} of daily candles for {len(universe)} shares...")
    data = db.download([c.yahoo_ticker for c in universe], args.period)
    if not data:
        print("No data downloaded (is Yahoo reachable?).")
        return 1
    books = {("zone", "Fixed target (2x, 20 days)"): [], ("zone", "Trailing (close below 20 SMA)"): [],
             ("trend", "Fixed target (2x, 20 days)"): [], ("trend", "Trailing (close below 20 SMA)"): []}
    for ticker, frame in data.items():
        ind = mz.indicators(frame)
        for kind, mask in (("zone", mz.signal_mask(ind)), ("trend", mz.trend_mask(ind))):
            signals = mz.levels(ind, mask.fillna(False))
            for exit_name, trades in (("Fixed target (2x, 20 days)", fixed(frame, signals)),
                                      ("Trailing (close below 20 SMA)", trail(frame, ind, signals))):
                books[(kind, exit_name)].extend({**t, "ticker": ticker, "company": names.get(ticker, ticker)}
                                                for t in trades)
    frames = {key: pd.DataFrame(rows) for key, rows in books.items()}
    zone_all = pd.concat([frames[("zone", e)] for e in ("Fixed target (2x, 20 days)", "Trailing (close below 20 SMA)")])
    if zone_all.empty:
        print("No signals found.")
        return 1
    low, high = zone_all["slope"].quantile([1 / 3, 2 / 3])          # same cut points for both exits

    def group(slope):
        return "Flat" if slope <= low else "Medium (about 45 degrees)" if slope <= high else "Steep"

    days = sorted(pd.to_datetime(zone_all["entry_day"]))
    split = days[len(days) // 2]
    rows = []
    for exit_name in ("Fixed target (2x, 20 days)", "Trailing (close below 20 SMA)"):
        zone = frames[("zone", exit_name)]
        if not zone.empty:
            zone = zone.assign(group=zone["slope"].map(group))
            frames[("zone", exit_name)] = zone
        rows.append(summary("MA zone: every signal", exit_name, zone, split))
        for g in ("Flat", "Medium (about 45 degrees)", "Steep"):
            rows.append(summary(f"MA zone: {g} slope", exit_name, zone[zone["group"] == g] if not zone.empty else zone, split))
        rows.append(summary("Yardstick: any uptrend day", exit_name, frames[("trend", exit_name)], split))
    table = pd.DataFrame(rows)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.cell(1, 1, "MA-zone pullback with a trailing exit: per £250 trade").font = Font(name="Arial", bold=True, size=13)
    period = f"{min(f.index[0] for f in data.values()).date()} to {max(f.index[-1] for f in data.values()).date()}"
    bt.note(ws, 2, f"Generated {datetime.now(sc.UK_TZ):%Y-%m-%d %H:%M} UK time. {len(data)} {args.index or 'FTSE'} shares, daily "
                   f"candles {period}. Same entries for both exits: 9 EMA above a rising 20 SMA, a day dips to the 9 EMA or "
                   "into the zone down to the 20 SMA and closes up; buy at the next open; protective stop below the dip. "
                   "Fixed target = sell at 2x the stop distance or after 20 days. Trailing = no target; sell at the next open "
                   f"after a day closes below the 20 SMA (or after {MAX_HOLD} trading days).")
    bt.note(ws, 3, "Costs per £250 trade: IG share dealing £1.50 (25p price gap + £1.25 stamp duty); no stamp duty 25p "
                   "(e.g. a UK-listed ETF); IG spread bet 25p spread + about 4.5p per night held. Slope groups use the same "
                   f"cut points for both exits (20 SMA rise per day in typical daily ranges): flat up to {low:.3f}, medium "
                   f"{low:.3f} to {high:.3f}, steep above {high:.3f}.")
    bt.note(ws, 4, "Caution: today's FTSE 100 list (companies that dropped out are missing) and no dividends. 'Share of gains "
                   "from best 10%' shows how much the result depends on a few big winners.")
    money = [c for c in table.columns if "(£)" in c]
    bt.write_table(ws, 6, table.round(4), money, ["Made money (IG shares)", "Share of gains from best 10% of trades"])
    for col, width in zip("ABCDEFGHIJKLMN", [34, 26, 8, 9, 10, 12, 12, 12, 12, 12, 12, 12, 12]):
        ws.column_dimensions[col].width = width

    year_ws = wb.create_sheet("Each year")
    empty = pd.DataFrame(columns=["entry_day", "net_shares", "net_spread_bet", "company", "ticker", "group", "fill", "stop",
                                  "outcome", "exit_day", "days", "gross"])
    trail_zone = frames[("zone", "Trailing (close below 20 SMA)")]
    fixed_zone = frames[("zone", "Fixed target (2x, 20 days)")]
    trail_zone = trail_zone if not trail_zone.empty else empty
    fixed_zone = fixed_zone if not fixed_zone.empty else empty
    yearly = pd.DataFrame({
        "Trailing: trades": trail_zone.groupby(pd.to_datetime(trail_zone["entry_day"]).dt.year).size(),
        "Trailing: total, IG shares (£)": trail_zone.groupby(pd.to_datetime(trail_zone["entry_day"]).dt.year)["net_shares"].sum().round(2),
        "Trailing: total, spread bet (£)": trail_zone.groupby(pd.to_datetime(trail_zone["entry_day"]).dt.year)["net_spread_bet"].sum().round(2),
        "Fixed target: total, IG shares (£)": fixed_zone.groupby(pd.to_datetime(fixed_zone["entry_day"]).dt.year)["net_shares"].sum().round(2)})
    yearly.index.name = "Year"
    bt.write_table(year_ws, 1, yearly.reset_index(), [c for c in yearly.columns if "(£)" in c])
    for col in "ABCDE":
        year_ws.column_dimensions[col].width = 24

    trade_ws = wb.create_sheet("Trades (trailing)")
    shown = trail_zone.sort_values("entry_day")[["entry_day", "company", "ticker", "group", "fill", "stop", "outcome",
                                                 "exit_day", "days", "gross", "net_shares", "net_spread_bet"]]
    shown.columns = ["Entry day", "Company", "Ticker", "Slope group", "Entry", "Stop", "Exit reason", "Exit day", "Days held",
                     "Before costs (£)", "After costs: IG shares (£)", "After costs: spread bet (£)"]
    bt.write_table(trade_ws, 1, shown, [c for c in shown.columns if "(£)" in c])
    trade_ws.freeze_panes = "A2"
    saved = sc.save_workbook(wb, Path(args.out))
    pd.set_option("display.width", 300)
    print(table[["Strategy", "Exit", "Trades", "Avg days held", "Before costs per £250 (£)", "After costs: IG shares (£)",
                 "After costs: no stamp duty (£)", "After costs: IG spread bet (£)"]].round(2).to_string(index=False))
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="MA-zone pullback: fixed target vs trailing exit, per £250")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--index", default="FTSE 100")
    parser.add_argument("--period", default="10y")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default="results/ftse_ma_zone_trailing_ftse100.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
