"""Exit and day-lock backtest: can the scanner keep more of its earlier profits? (plan step 11; Phil agreed 12 Nov 2026)

Phil's observation (6 and 12 Nov): the day's total is often up by midday (about +£1.50 to +£4) and gives it back by
the close. This replays the last 60 trading days under today's live rules (FTSE 100, £100 a trade, 10p costs, up to
5 open and 10 a day, the fill check, and the 4 Nov fill rules: stop at most 0.9% away and target left at least the
stop) and compares exits set in advance (12 Nov 2026, before any results):

  A. Current: full target or stop (closed at 16:30 if neither)
  B. Half the target: take profit halfway to the target
  C. Trailing stop: once a trade is halfway to its target, its stop follows the best price, keeping half the best
     gain (moves from the next candle), still aiming for the full target
  D. Day lock at +£1: once the day's total (closed trades plus open trades at the latest price) reaches +£1,
     close every open trade and take no more trades that day
  E. Day lock at +£1.50: the same at +£1.50
  F. Give-back lock: once the day has been up £1.50 or more, close everything (and stop for the day) if it falls
     back to half of its best

Locks D-F act as the live scanner could: the day's total is known 15 minutes after a candle ends (Yahoo's delay),
and open trades are closed at the open of the next candle after that. The trades themselves (which ideas, when)
are the same for every exit; only how they end changes, except that the locks take no new trades after locking.

An exit "helps" only if its day totals beat A in BOTH halves (split at the middle day) AND a random sign-flip test
of the day-by-day differences says luck would do as well under 5% of the time. Paper research only.

    python ftse_exit_lock_backtest.py --out results/ftse_exit_locks.xlsx
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook

import ftse_backtest as bt
import ftse_entry_check_backtest as ec
import ftse_feasibility_backtest as fb
import ftse_trade_scanner as sc

STAKE, COST_PCT = 100.0, 0.10
MAX_STOP_PCT, MIN_TARGET_VS_STOP = 0.9, 1.0
DELAY = pd.Timedelta(minutes=15)
CANDLE = pd.Timedelta(minutes=15)
DRAWS = 10000
LUCK_LIMIT = 0.05
EXITS = {
    "A": "A. Current: full target or stop",
    "B": "B. Half the target",
    "C": "C. Trailing stop once halfway (keeps half the best gain)",
    "D": "D. Day lock at +£1",
    "E": "E. Day lock at +£1.50",
    "F": "F. Give-back lock (up £1.50+, close at half the best)",
}
LOCKS = {"D": ("level", 1.0), "E": ("level", 1.5), "F": ("giveback", 1.5)}


def live_check(row, result) -> bool:
    """Today's live entry rules: the fill check plus the 4 Nov fill rules."""
    if not ec.half_left(row, result):
        return False
    fill, stop, target = float(result["fill"]), float(row["stop"]), float(row["target"])
    risk = abs(fill - stop)
    if risk / fill * 100 > MAX_STOP_PCT:
        return False
    return abs(target - fill) >= MIN_TARGET_VS_STOP * risk


def net(side: str, fill: float, price: float) -> float:
    move = (price - fill) / fill if side == "Long" else (fill - price) / fill
    return move * STAKE - STAKE * COST_PCT / 100


def path_exit(bars: pd.DataFrame, side: str, fill: float, stop: float, target: float, mode: str
              ) -> tuple[pd.Timestamp | None, float, str]:
    """(exit candle start, exit price, reason) for one trade under exit A, B or C."""
    long = side == "Long"
    half = fill + (target - fill) / 2
    aim = half if mode == "B" else target
    current, best, trailing = stop, fill, False
    for stamp, bar in bars.iterrows():
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        if mode == "C" and trailing:                          # the trail uses the best price of earlier candles
            keep = fill + (best - fill) / 2
            current = max(current, keep) if long else min(current, keep)
        if (l <= current) if long else (h >= current):
            return stamp, (min(o, current) if long else max(o, current)), "stop"
        if (h >= aim) if long else (l <= aim):
            return stamp, (max(o, aim) if long else min(o, aim)), "target"
        if mode == "C":
            best = max(best, h) if long else min(best, l)
            if not trailing and ((best >= half) if long else (best <= half)):
                trailing = True
    if len(bars):
        return bars.index[-1], float(bars["close"].iloc[-1]), "16:30"
    return None, fill, "no candles"


def trade_paths(trades: pd.DataFrame, intraday: dict) -> list[dict]:
    """Each trade's candles from its fill, plus its exits under A, B and C."""
    out = []
    for _, t in trades.iterrows():
        row = t.to_dict()
        start = fb.fill_candle(row)
        bars = fb.day_bars(intraday[row["ticker"]], start)
        fill, stop, target = float(row["fill"]), float(row["stop"]), float(row["target"])
        item = {"day": row["day"], "ticker": row["ticker"], "side": row["direction"], "fill": fill,
                "start": start, "bars": bars}
        for mode in "ABC":
            when, price, why = path_exit(bars, row["direction"], fill, stop, target, mode)
            item[mode] = {"end": when, "price": price, "why": why, "net": net(row["direction"], fill, price)}
        out.append(item)
    return out


def day_with_lock(items: list[dict], kind: str, level: float) -> tuple[float, int, str | None]:
    """Day total under a lock applied to exit A; returns (total £, trades taken, lock time or None)."""
    if not items:
        return 0.0, 0, None
    times = sorted({s for it in items for s in it["bars"].index})
    best, locked_at = -math.inf, None
    for stamp in times:
        total = 0.0
        for it in items:
            if it["start"] > stamp:
                continue                                       # not filled yet
            a = it["A"]
            if a["end"] is not None and a["end"] <= stamp:
                total += a["net"]
            elif stamp in it["bars"].index:
                total += net(it["side"], it["fill"], float(it["bars"].loc[stamp, "close"]))
        best = max(best, total)
        hit = total >= level if kind == "level" else (best >= level and total <= best / 2)
        if hit:
            locked_at = stamp
            break
    if locked_at is None:
        return sum(it["A"]["net"] for it in items), len(items), None
    act = locked_at + CANDLE + DELAY                           # known 15 minutes after the candle ends
    total, taken = 0.0, 0
    for it in items:
        if it["start"] >= act:
            continue                                           # no new trades after the lock
        taken += 1
        a = it["A"]
        if a["end"] is not None and a["end"] < act:
            total += a["net"]
            continue
        later = it["bars"][it["bars"].index >= act]
        price = float(later["open"].iloc[0]) if len(later) else a["price"]
        total += net(it["side"], it["fill"], price)
    return total, taken, act.strftime("%H:%M")


def day_totals(items: list[dict]) -> pd.DataFrame:
    rows = []
    for day in sorted({it["day"] for it in items}):
        todays = [it for it in items if it["day"] == day]
        row = {"day": day, "trades": len(todays)}
        for mode in "ABC":
            row[mode] = sum(it[mode]["net"] for it in todays)
        for key, (kind, level) in LOCKS.items():
            total, taken, when = day_with_lock(todays, kind, level)
            row[key], row[f"{key}_trades"], row[f"{key}_locked_at"] = total, taken, when
        rows.append(row)
    return pd.DataFrame(rows)


def sign_flip_luck(diff: np.ndarray, rng: np.random.Generator) -> float | None:
    """Share of random sign flips of the day differences whose average is at least the real one."""
    diff = diff[np.isfinite(diff)]
    if len(diff) == 0 or np.allclose(diff, 0):
        return None
    flips = rng.choice([-1.0, 1.0], size=(DRAWS, len(diff)))
    return float(np.mean((flips * diff).mean(axis=1) >= diff.mean()))


def summarise(days: pd.DataFrame, trade_count: int) -> pd.DataFrame:
    split = days["day"].iloc[len(days) // 2]
    first = days["day"] < split
    rng = np.random.default_rng(12)
    rows = []
    for key, name in EXITS.items():
        total = days[key]
        taken = days[f"{key}_trades"].sum() if f"{key}_trades" in days else trade_count
        row = {"Exit": name, "Days": len(days), "Trades": int(taken), "Total (£)": float(total.sum()),
               "Per day (£)": float(total.mean()), "Per trade (£)": float(total.sum() / max(1, taken)),
               "First half (£)": float(total[first].sum()), "Second half (£)": float(total[~first].sum()),
               "Winning days": float((total > 0).mean()), "Worst day (£)": float(total.min()),
               "Best day (£)": float(total.max()), "Split at": split}
        if key in LOCKS:
            row["Days locked"] = int(days[f"{key}_locked_at"].notna().sum())
        if key != "A":
            diff = (total - days["A"]).to_numpy()
            row["Better than A: first half"] = bool(total[first].sum() > days["A"][first].sum())
            row["Better than A: second half"] = bool(total[~first].sum() > days["A"][~first].sum())
            row["Luck as good"] = sign_flip_luck(diff, rng)
            row["Helps"] = bool(row["Better than A: first half"] and row["Better than A: second half"]
                                and row["Luck as good"] is not None and row["Luck as good"] < LUCK_LIMIT)
        rows.append(row)
    return pd.DataFrame(rows)


def verdict_lines(table: pd.DataFrame) -> list[str]:
    base = table.iloc[0]
    lines = [f"{base['Exit']}: {int(base['Trades'])} trades over {int(base['Days'])} days, £{base['Total (£)']:+.2f} "
             f"(first half £{base['First half (£)']:+.2f}, second £{base['Second half (£)']:+.2f}); "
             f"{base['Winning days']:.0%} winning days."]
    for _, r in table.iloc[1:].iterrows():
        head = "HELPS" if r.get("Helps") else "does not help"
        extra = f"; locked on {int(r['Days locked'])} days" if pd.notna(r.get("Days locked")) else ""
        luck = f"{r['Luck as good']:.0%}" if pd.notna(r.get("Luck as good")) else "n/a"
        lines.append(f"{r['Exit']}: {head}. £{r['Total (£)']:+.2f} (first half £{r['First half (£)']:+.2f}, second "
                     f"£{r['Second half (£)']:+.2f}); {r['Winning days']:.0%} winning days; luck as good {luck}{extra}.")
    return lines


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
        print("No ideas.")
        return 1
    now = pd.Timestamp.now(tz=sc.UK_TZ)
    trades = ec.live_trader(ideas, intraday, now, live_check)
    done = trades[trades["status"] == "done"] if not trades.empty else trades
    items = trade_paths(done, intraday)
    days = day_totals(items)
    table = summarise(days, len(items))
    verdict = [f"FTSE 100, {days['day'].iloc[0]} to {days['day'].iloc[-1]} ({len(days)} trading days with trades), "
               "today's live rules (fill check, stop at most 0.9%, target left at least the stop), 15-minute data delay. "
               "An exit helps only if its day totals beat A in both halves and a sign-flip test says luck would do as "
               f"well under {LUCK_LIMIT:.0%} of the time."]
    verdict += verdict_lines(table)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Verdict"
    ws.cell(1, 1, "Exits and day locks: keeping earlier profits").font = bt.Font(name="Arial", bold=True, size=13)
    for n, line in enumerate(verdict, 3):
        bt.note(ws, n, line)
    ws.column_dimensions["A"].width = 150
    pct = ["Winning days", "Luck as good"]
    fb.write_sheet(wb, "Exits", table, ["Each exit's day totals against A."], pct=pct)
    fb.write_sheet(wb, "Days", days, ["Day totals (£) under each exit; locks show when they locked."])
    trade_rows = pd.DataFrame([{"day": it["day"], "ticker": it["ticker"], "side": it["side"], "fill": it["fill"],
                                "fill_candle": it["start"].strftime("%H:%M"),
                                **{f"{m}_net": round(it[m]["net"], 2) for m in "ABC"},
                                **{f"{m}_why": it[m]["why"] for m in "ABC"}} for it in items])
    trade_rows.to_csv(out.with_name(out.stem + "_trades.csv"), index=False)
    saved = sc.save_workbook(wb, out)
    out.with_suffix(".json").write_text(json.dumps(
        {"generated": datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M"), "verdict": verdict,
         "exits": table.astype(object).where(table.notna(), None).to_dict("records")}, indent=1, default=str))
    for line in verdict:
        print(line)
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Exit and day-lock backtest (plan step 11)")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--warmup-days", type=int, default=12)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--data-delay", type=int, default=15)
    parser.add_argument("--out", default="results/ftse_exit_locks.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
