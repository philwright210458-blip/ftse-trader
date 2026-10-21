"""Data check: which 15-minute candles is Yahoo missing for given shares and days?

Writes results/data_check.txt. Used to explain trades the outcome tracker cannot
resolve ("Not resolved yet" / "Missing data"): the tracker refuses to guess what
happened during a missing candle.

    python ftse_data_check.py DOCS.L BRBY.L --days 2026-10-15 2026-10-19
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import ftse_trade_scanner as sc
from ftse_sessions import calendar, session_open_close


def check(ticker: str, days: list[str], interval: str = "15m") -> list[str]:
    lines = [f"{ticker}"]
    try:
        raw = sc.completed_candles(sc.fetch_data(ticker, interval, "60d"), interval)
    except Exception as error:
        return lines + [f"  download failed: {error}"]
    traded = sc.drop_untraded_bars(raw)
    step = pd.Timedelta(minutes=sc.interval_minutes(interval))
    for day in days:
        opening, closing = session_open_close(pd.Timestamp(day))
        slots = pd.date_range(opening, closing, freq=step, inclusive="left")
        absent = [s for s in slots if s not in raw.index]
        no_trades = [s for s in slots if s in raw.index and s not in traded.index]
        lines.append(f"  {day}: {len(slots)} candles expected, {len(slots) - len(absent)} returned, "
                     f"{len(no_trades)} with zero volume (treated as missing)")
        if absent:
            lines.append("    not returned: " + ", ".join(s.strftime("%H:%M") for s in absent))
        if no_trades:
            lines.append("    zero volume: " + ", ".join(s.strftime("%H:%M") for s in no_trades))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="List missing 15-minute candles for shares and days")
    parser.add_argument("tickers", nargs="+")
    parser.add_argument("--days", nargs="*", default=None, help="Trading days (default: the last 5)")
    parser.add_argument("--out", default="results/data_check.txt")
    args = parser.parse_args()
    days = args.days
    if not days:
        today = pd.Timestamp.now(tz=sc.UK_TZ).tz_localize(None).normalize()
        sessions = calendar().sessions_in_range(today - pd.Timedelta(days=10), today)
        days = [str(d.date()) for d in sessions[-5:]]
    report = [f"Yahoo 15-minute data check, {pd.Timestamp.now(tz=sc.UK_TZ):%Y-%m-%d %H:%M} UK", ""]
    for ticker in args.tickers:
        report += check(ticker, days) + [""]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))


if __name__ == "__main__":
    main()
