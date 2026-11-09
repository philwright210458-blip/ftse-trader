"""Trend and market-direction checks on the 15-minute scanner's trades (plan step 10; Phil agreed 9 Nov 2026).

Phil's early-2000s approach: focus on key companies and look at the share's recent trend, volume and the market
before trading. This tests whether any of those checks picks out the scanner's better trades, using trades that
already exist (no new scanner runs):

  * the 359 backtest trades (results/ftse_feasibility_delay15_trades.csv: FTSE 100, live rules, 15-minute delay)
  * the live paper trades (scan_data/trade_notes.csv, entered trades)

Variants set in advance (9 Nov 2026, before any results):

  A. All trades (the comparison)
  B. With the market: the FTSE 100 has moved the trade's way so far today (from yesterday's close to the last
     hourly price the scanner could have seen, i.e. ending at least 15 minutes before the signal; before the
     first hour ends, the opening price)
  C. With the sector: the average of the share's sector peers (config/sectors.csv, the share itself left out) has
     moved the trade's way, measured the same way
  D. With the short trend: 9 EMA above the 20 SMA on 15-minute candles for a buy, below for a sell
  E. With the bigger trend: daily and 4-hour trends both point the trade's way
  F. Volume: the signal candle's volume at least 1.5 times its recent average
  G. Key companies: the 20 largest FTSE 100 companies only
  H. Market and short trend together (B and D)

A check "helps" only if its trades beat A in BOTH halves (split at the middle day) AND random groups of the same
number of trades do as well under 5% of the time (5,000 draws). Found-after-looking results are not adopted
without a forward test. Paper research only; not financial advice.

    python ftse_trend_filters_test.py --out results/ftse_trend_filters.xlsx
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook

import ftse_backtest as bt
import ftse_feasibility_backtest as fb
import ftse_trade_scanner as sc

ROOT = Path(__file__).parent
BACKTEST = ROOT / "results" / "ftse_feasibility_delay15_trades.csv"
LIVE = ROOT / "scan_data" / "trade_notes.csv"
SECTORS = ROOT / "config" / "sectors.csv"
INDEX = "^FTSE"
DELAY = pd.Timedelta(minutes=15)
DRAWS = 5000
LUCK_LIMIT = 0.05
KEY_COMPANIES = ["AZN.L", "SHEL.L", "HSBA.L", "ULVR.L", "RIO.L", "REL.L", "BATS.L", "LSEG.L", "GSK.L", "BP.L",
                 "BA.L", "RR.L", "NG.L", "BARC.L", "LLOY.L", "DGE.L", "GLEN.L", "CPG.L", "EXPN.L", "AAL.L"]
VARIANTS = {
    "A": "A. All trades",
    "B": "B. With the market (FTSE 100 so far today)",
    "C": "C. With the sector so far today",
    "D": "D. With the short trend (9 EMA v 20 SMA, 15-min)",
    "E": "E. With the bigger trend (daily and 4-hour)",
    "F": "F. Volume at least 1.5x average on the signal candle",
    "G": "G. Key companies (20 largest)",
    "H": "H. Market and short trend together (B + D)",
}


def load_trades() -> pd.DataFrame:
    cols = ["day", "ticker", "company", "setup", "direction", "signal_time", "net_gbp", "ema_state",
            "daily_trend", "trend_4h", "signal_volume_ratio"]
    back = pd.read_csv(BACKTEST)
    back = back[back["entered"].astype(str).str.startswith(("Yes", "True"))] if "entered" in back else back
    live = pd.read_csv(LIVE)
    live = live[live["entered"] == "Yes"]
    out = []
    for source, frame in (("Backtest", back), ("Live", live)):
        frame = frame[[c for c in cols if c in frame.columns]].copy()
        frame["source"] = source
        out.append(frame)
    trades = pd.concat(out, ignore_index=True)
    trades["sign"] = np.where(trades["direction"] == "Long", 1, -1)
    return trades


def move_so_far(hourly: pd.DataFrame, day: str, signal_time: str) -> float | None:
    """% move from the previous day's close to the last hourly price the scanner could have seen."""
    if hourly is None or hourly.empty:
        return None
    stamp = pd.Timestamp(f"{day} {signal_time}", tz=sc.UK_TZ) - DELAY
    index = hourly.index
    today = hourly[index.normalize() == pd.Timestamp(day, tz=sc.UK_TZ)]
    before = hourly[index < pd.Timestamp(day, tz=sc.UK_TZ)]
    if today.empty or before.empty:
        return None
    previous_close = float(before["close"].iloc[-1])
    done = today[today.index + pd.Timedelta(hours=1) <= stamp]
    price = float(done["close"].iloc[-1]) if not done.empty else float(today["open"].iloc[0])
    return (price / previous_close - 1) * 100 if previous_close else None


def hourly_prices(tickers: list[str]) -> dict[str, pd.DataFrame]:
    print(f"Downloading hourly prices for {len(tickers)} tickers...")
    data = sc.fetch_many(tickers, "60m", "730d")
    out = {}
    for ticker, frame in data.items():
        frame = frame.copy()
        frame.index = pd.to_datetime(frame.index)
        frame.index = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index
        out[ticker] = frame.tz_convert(sc.UK_TZ)[["open", "close"]].dropna()
    return out


def add_market_and_sector(trades: pd.DataFrame, prices: dict[str, pd.DataFrame], sectors: pd.DataFrame) -> pd.DataFrame:
    trades = trades.copy()
    by_sector = sectors.groupby("sector")["ticker"].apply(list).to_dict()
    sector_of = dict(zip(sectors["ticker"], sectors["sector"]))
    cache: dict[tuple, float | None] = {}

    def move(ticker, day, time):
        key = (ticker, day, time)
        if key not in cache:
            cache[key] = move_so_far(prices.get(ticker), day, time)
        return cache[key]

    market, sector = [], []
    for _, t in trades.iterrows():
        market.append(move(INDEX, t["day"], t["signal_time"]))
        peers = [p for p in by_sector.get(sector_of.get(t["ticker"]), []) if p != t["ticker"]]
        moves = [m for m in (move(p, t["day"], t["signal_time"]) for p in peers) if m is not None]
        sector.append(float(np.mean(moves)) if moves else None)
    trades["market_move_pct"] = market
    trades["sector_move_pct"] = sector
    return trades


def masks(trades: pd.DataFrame) -> dict[str, pd.Series]:
    s = trades["sign"]
    ema = trades["ema_state"].map({"Up": 1, "Down": -1}).fillna(0)
    daily = trades["daily_trend"].map({"Up": 1, "Down": -1}).fillna(0)
    h4 = trades["trend_4h"].map({"Up": 1, "Down": -1}).fillna(0)
    market = np.sign(pd.to_numeric(trades.get("market_move_pct"), errors="coerce")).fillna(0)
    sector = np.sign(pd.to_numeric(trades.get("sector_move_pct"), errors="coerce")).fillna(0)
    volume = pd.to_numeric(trades["signal_volume_ratio"], errors="coerce").fillna(0)
    m = {
        "A": pd.Series(True, index=trades.index),
        "B": market == s,
        "C": sector == s,
        "D": ema == s,
        "E": (daily == s) & (h4 == s),
        "F": volume >= 1.5,
        "G": trades["ticker"].isin(KEY_COMPANIES),
        "H": (market == s) & (ema == s),
    }
    return {k: v.astype(bool) for k, v in m.items()}


def luck(all_net: np.ndarray, chosen_avg: float, n: int, rng: np.random.Generator) -> float | None:
    if n == 0 or n >= len(all_net):
        return None
    draws = np.array([rng.choice(all_net, n, replace=False).mean() for _ in range(DRAWS)])
    return float(np.mean(draws >= chosen_avg))


def evaluate(trades: pd.DataFrame, source: str, seed: int = 7) -> pd.DataFrame:
    part = trades[trades["source"] == source].reset_index(drop=True)
    if part.empty:
        return pd.DataFrame()
    days = sorted(part["day"].unique())
    split = days[len(days) // 2]
    first = part["day"] < split
    net = part["net_gbp"].astype(float)
    rng = np.random.default_rng(seed)
    base = {"first": net[first].mean(), "second": net[~first].mean()}
    rows = []
    for key, mask in masks(part).items():
        chosen = net[mask]
        row = {"Source": source, "Check": VARIANTS[key], "Trades": int(mask.sum()),
               "Share of trades": float(mask.mean()),
               "Average per £100 trade (£)": float(chosen.mean()) if len(chosen) else None,
               "First half (£)": float(net[mask & first].mean()) if (mask & first).any() else None,
               "Second half (£)": float(net[mask & ~first].mean()) if (mask & ~first).any() else None,
               "Won": float((chosen > 0).mean()) if len(chosen) else None,
               "Split at": split}
        row["Other trades: average (£)"] = float(net[~mask].mean()) if (~mask).any() else None
        if key != "A" and len(chosen):
            row["Random groups as good"] = luck(net.to_numpy(), float(chosen.mean()), int(mask.sum()), rng)
            row["Better than all trades in both halves"] = bool(
                row["First half (£)"] is not None and row["Second half (£)"] is not None
                and row["First half (£)"] > base["first"] and row["Second half (£)"] > base["second"])
            row["Helps"] = bool(row["Better than all trades in both halves"] and row["Random groups as good"] is not None
                                and row["Random groups as good"] < LUCK_LIMIT)
        rows.append(row)
    return pd.DataFrame(rows)


def verdict_lines(table: pd.DataFrame) -> list[str]:
    lines = []
    for source, part in table.groupby("Source", sort=False):
        base = part.iloc[0]
        lines.append(f"{source} ({int(base['Trades'])} trades, split at {base['Split at']}): all trades "
                     f"{base['Average per £100 trade (£)'] * 100:+.1f}p a trade.")
        for _, r in part.iloc[1:].iterrows():
            if not r["Trades"]:
                lines.append(f"  {r['Check']}: no trades.")
                continue
            verdict = "HELPS" if r.get("Helps") else "does not help"
            lines.append(
                f"  {r['Check']}: {verdict}. {int(r['Trades'])} trades ({r['Share of trades']:.0%}), "
                f"{r['Average per £100 trade (£)'] * 100:+.1f}p a trade (first half {r['First half (£)'] * 100:+.1f}p, "
                f"second {r['Second half (£)'] * 100:+.1f}p) v {r['Other trades: average (£)'] * 100:+.1f}p for the rest; "
                f"random groups as good {r['Random groups as good']:.0%} of the time.")
    return lines


def run(args) -> int:
    trades = load_trades()
    sectors = pd.read_csv(SECTORS)
    if not args.offline:
        prices = hourly_prices([INDEX] + sorted(set(sectors["ticker"])))
        trades = add_market_and_sector(trades, prices, sectors)
    else:
        trades["market_move_pct"] = None
        trades["sector_move_pct"] = None
    table = pd.concat([evaluate(trades, "Backtest"), evaluate(trades, "Live")], ignore_index=True)
    verdict = ["Checks on the 15-minute scanner's trades (Phil's focus-and-trend approach, plan step 10). A check helps "
               f"only if its trades beat all trades in both halves and random groups of the same size do as well under "
               f"{LUCK_LIMIT:.0%} of the time. The backtest (359 trades) is the main evidence; live trades are a check."]
    verdict += verdict_lines(table)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Verdict"
    ws.cell(1, 1, "Trend and market checks on the scanner's trades").font = bt.Font(name="Arial", bold=True, size=13)
    for n, line in enumerate(verdict, 3):
        bt.note(ws, n, line)
    ws.column_dimensions["A"].width = 150
    pct = ["Share of trades", "Won", "Random groups as good"]
    fb.write_sheet(wb, "Checks", table, ["Each check against all trades."], pct=pct)
    trades.to_csv(out.with_name(out.stem + "_trades.csv"), index=False)
    saved = sc.save_workbook(wb, out)
    out.with_suffix(".json").write_text(json.dumps(
        {"generated": datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M"), "verdict": verdict,
         "checks": table.astype(object).where(table.notna(), None).to_dict("records")}, indent=1, default=str))
    for line in verdict:
        print(line)
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Trend and market checks on the scanner's trades (plan step 10)")
    parser.add_argument("--out", default="results/ftse_trend_filters.xlsx")
    parser.add_argument("--offline", action="store_true", help="Skip the market and sector downloads")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
