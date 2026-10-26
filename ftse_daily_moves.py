"""Daily FTSE 100 moves and the morning picker's scorecard (from 26 Oct 2026).

Run by the 17:05 outcomes workflow. Writes scan_data/daily_moves/<UK date>.csv with every FTSE 100
share's move that day, and the morning picker's view of it with whether the call was right:

    day,ticker,company,prev_close,open,high,low,close,change_pct,open_to_close_pct,picker_view,picker_right

change_pct = close against the previous close (what news sites quote, e.g. "+13%").
open_to_close_pct = from the 08:00 open to the close (how the picker is scored: a Buy is right when the
share closed above its open, a Sell when below; Avoid and no view are not scored).

The morning picker reads the previous trading day's file to see the biggest movers and its own
scorecard. The scanner does not use it.

    python ftse_daily_moves.py            # today (UK)
    python ftse_daily_moves.py --day 2026-10-23
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

import ftse_trade_scanner as sc
from ftse_picker import load_views

COLUMNS = ["day", "ticker", "company", "prev_close", "open", "high", "low", "close", "change_pct",
           "open_to_close_pct", "picker_view", "picker_right"]


def day_moves(frames: dict[str, pd.DataFrame], names: dict[str, str], day: str,
              views: dict[str, tuple[str, str]] | None) -> pd.DataFrame:
    rows = []
    for ticker, frame in frames.items():
        if frame is None or frame.empty:
            continue
        frame = frame.sort_index()
        dates = pd.DatetimeIndex(frame.index)
        dates = dates.tz_convert(sc.UK_TZ) if dates.tz is not None else dates
        keys = [str(d.date()) for d in dates]
        if day not in keys:
            continue
        n = keys.index(day)
        bar = frame.iloc[n]
        o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)):
            continue
        prev = float(frame["close"].iloc[n - 1]) if n > 0 else float("nan")
        view = (views or {}).get(ticker.upper(), ("", ""))[0]
        move = c / o - 1
        right = "" if view not in ("Buy", "Sell") or c == o else ("Yes" if (move > 0) == (view == "Buy") else "No")
        rows.append({"day": day, "ticker": ticker, "company": names.get(ticker, ticker),
                     "prev_close": round(prev, 4) if math.isfinite(prev) else None, "open": round(o, 4),
                     "high": round(h, 4), "low": round(l, 4), "close": round(c, 4),
                     "change_pct": round((c / prev - 1) * 100, 2) if math.isfinite(prev) and prev > 0 else None,
                     "open_to_close_pct": round(move * 100, 2), "picker_view": view, "picker_right": right})
    table = pd.DataFrame(rows, columns=COLUMNS)
    return table.sort_values("change_pct", ascending=False, na_position="last").reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Save the day's FTSE 100 moves and the picker's scorecard")
    parser.add_argument("--day", default=None, help="UK date YYYY-MM-DD (default: today)")
    parser.add_argument("--data-dir", default=str(sc.DEFAULT_DATA_DIR))
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    args = parser.parse_args()
    day = args.day or datetime.now(sc.UK_TZ).strftime("%Y-%m-%d")
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.index == "FTSE 100" and c.kind != "trust"]
    names = {c.yahoo_ticker: c.company for c in universe}
    frames = sc.fetch_many(list(names), "1d", "10d")
    data_dir = Path(args.data_dir)
    table = day_moves(frames, names, day, load_views(data_dir, day))
    if table.empty:
        print(f"No daily prices for {day} (not a trading day, or Yahoo not updated yet). Nothing saved.")
        sys.exit(0)
    out = data_dir / "daily_moves" / f"{day}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(out, index=False)
    scored = table[table["picker_right"].isin(["Yes", "No"])]
    score = f"; picker {int((scored['picker_right'] == 'Yes').sum())} of {len(scored)} calls right" if len(scored) else ""
    top, bottom = table.iloc[0], table.dropna(subset=["change_pct"]).iloc[-1]
    print(f"{day}: {len(table)} FTSE 100 shares saved to {out}. Biggest riser {top['company']} {top['change_pct']:+.1f}%, "
          f"biggest faller {bottom['company']} {bottom['change_pct']:+.1f}%{score}.")


if __name__ == "__main__":
    main()
