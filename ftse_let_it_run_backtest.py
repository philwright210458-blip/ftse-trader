"""Let it run: trailing exits, small targets and a first-scan cap (plan step 12; Phil agreed 13 Nov 2026)

Phil, 13 Nov: once a trade is past half its target, let it keep running and close it only when it starts to
retreat. He would use IG spread bets, whose trailing stops follow live prices. This replays the same 60 trading
days and live rules as step 11 (FTSE 100, £100 a trade, 10p costs, up to 5 open and 10 a day, the fill check, stop
at most 0.9% away, target left at least the stop, 15-minute data delay). The baseline is B, half the target, which
is live from 13 Nov. Variants set in advance (13 Nov 2026, before any results):

Exits (the same trades; only how they end changes; no fixed target; anything still open closes at 16:30):
  G. Let it run (Phil's idea, scanner version): once the price reaches half target, the stop moves up to the
     half-target price from the next candle. The trade closes when a 15-minute candle closes back below the
     previous candle's low (buys; above its high for sells), acted on at the open of the next candle after the
     15-minute delay.
  H1. IG-style trailing stop once halfway: from the next candle the stop sits at the half-target price or the best
      price so far less the trade's own stop distance, whichever is better.
  H2. As H1 with half the stop distance.
  I. IG-style trailing stop from the fill: the stop trails the best price by the trade's stop distance from the
     start; no target.
  The IG trailing step is treated as continuous; stops act on candle highs and lows as if the broker held them.

Entries (all with exit B):
  J. Skip a trade whose full target at the fill is under half the share's typical daily move.
  K. At most 2 new trades from scans before 08:45 (the first, 08:32, scan).

A variant "helps" only if its day totals beat B in BOTH halves (split at the middle day) AND a random sign-flip
test of the day-by-day differences says luck would do as well under 5% of the time. A (full target) and C (step 11
trailing) are shown for reference. Paper research only; live trading is unchanged.

    python ftse_let_it_run_backtest.py --out results/ftse_let_it_run.xlsx
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook

import ftse_backtest as bt
import ftse_entry_check_backtest as ec
import ftse_exit_lock_backtest as el
import ftse_feasibility_backtest as fb
import ftse_trade_scanner as sc

DELAY, CANDLE = el.DELAY, el.CANDLE
LUCK_LIMIT = el.LUCK_LIMIT
FIRST_SCAN_BEFORE = "08:45"
FIRST_SCAN_CAP = 2
SMALL_TARGET = 0.5
EXIT_NAMES = {
    "A": "A. Full target or stop (before 13 Nov; reference)",
    "B": "B. Half the target (live from 13 Nov; baseline)",
    "C": "C. Step 11 trailing stop once halfway (reference)",
    "G": "G. Let it run: stop to half target, close when a candle closes back past the previous one",
    "H1": "H1. IG-style trailing once halfway: half target or best less the stop distance",
    "H2": "H2. IG-style trailing once halfway: half target or best less half the stop distance",
    "I": "I. IG-style trailing stop from the fill (stop distance), no target",
}
ENTRY_NAMES = {
    "J": "J. Skip targets under half a typical day's move (exit B)",
    "K": f"K. At most {FIRST_SCAN_CAP} new trades from the first scan (exit B)",
}
COMPARED = ["A", "C", "G", "H1", "H2", "I", "J", "K"]


# ---------------------------------------------------------------- exits

def _beyond(long: bool, price: float, level: float) -> bool:
    return price >= level if long else price <= level


def _better(long: bool, a: float, b: float) -> float:
    return max(a, b) if long else min(a, b)


def run_exit(bars: pd.DataFrame, side: str, fill: float, stop: float, target: float, mode: str
             ) -> tuple[pd.Timestamp | None, float, str]:
    """(exit candle start, exit price, reason) for one trade under exit G, H1, H2 or I."""
    long = side == "Long"
    risk = abs(fill - stop)
    half = fill + (target - fill) / 2
    distance = {"H1": risk, "H2": risk / 2, "I": risk}.get(mode)
    current, best, armed = stop, fill, mode == "I"
    close_from = None                                   # G: close at the first candle starting at or after this
    previous = None
    for stamp, bar in bars.iterrows():
        o, h, l, c = (float(bar[k]) for k in ("open", "high", "low", "close"))
        if close_from is not None and stamp >= close_from:
            return stamp, o, "retreat"
        if armed:                                       # moves use earlier candles only
            if mode == "G":
                current = _better(long, current, half)
            elif mode == "I":
                current = _better(long, current, best - distance if long else best + distance)
            else:
                trail = best - distance if long else best + distance
                current = _better(long, current, _better(long, half, trail))
        if (l <= current) if long else (h >= current):
            return stamp, (min(o, current) if long else max(o, current)), "stop"
        best = max(best, h) if long else min(best, l)
        if mode == "G" and armed and close_from is None and previous is not None:
            if (c < previous[1]) if long else (c > previous[0]):
                close_from = stamp + CANDLE + DELAY
        if not armed and _beyond(long, best, half):
            armed = True
        previous = (h, l)
    if len(bars):
        return bars.index[-1], float(bars["close"].iloc[-1]), "16:30"
    return None, fill, "no candles"


def exit_rows(trades: pd.DataFrame, intraday: dict) -> list[dict]:
    """Each trade's result under every exit (A, B and C from step 11; G, H1, H2 and I here)."""
    out = []
    for _, t in trades.iterrows():
        row = t.to_dict()
        start = fb.fill_candle(row)
        bars = fb.day_bars(intraday[row["ticker"]], start)
        side, fill = row["direction"], float(row["fill"])
        stop, target = float(row["stop"]), float(row["target"])
        item = {"day": row["day"], "ticker": row["ticker"], "side": side, "fill": fill,
                "fill_candle": start.strftime("%H:%M")}
        for mode in "ABC":
            _, price, why = el.path_exit(bars, side, fill, stop, target, mode)
            item[f"{mode}_net"], item[f"{mode}_why"] = el.net(side, fill, price), why
        for mode in ("G", "H1", "H2", "I"):
            _, price, why = run_exit(bars, side, fill, stop, target, mode)
            item[f"{mode}_net"], item[f"{mode}_why"] = el.net(side, fill, price), why
        out.append(item)
    return out


# ---------------------------------------------------------------- entries

def small_target_check(intraday: dict):
    """J: the live entry rules, plus skip a full target under half the share's typical daily move."""
    cache: dict[tuple[str, str], float | None] = {}

    def check(row, result) -> bool:
        if not el.live_check(row, result):
            return False
        key = (row["ticker"], str(row["day"]))
        if key not in cache:
            frame = intraday[row["ticker"]]
            day_end = pd.Timestamp(row["day"]).tz_localize(frame.index.tz) + pd.Timedelta(days=1)
            cache[key] = sc.typical_daily_move_pct(frame[frame.index < day_end])
        typical = cache[key]
        if not typical:
            return True
        fill = float(result["fill"])
        return abs(float(row["target"]) - fill) / fill * 100 >= SMALL_TARGET * typical
    return check


def capped_trader(ideas: pd.DataFrame, intraday, now, check) -> pd.DataFrame:
    """K: ec.live_trader with at most FIRST_SCAN_CAP new trades from scans before FIRST_SCAN_BEFORE."""
    taken = []
    for _, day in ideas.groupby("day"):
        tickers, trades = set(), []
        for run_time, scan in day.groupby("run_time", sort=True):
            now_scan = ec.out.market_stamp(run_time)
            still_open = 0
            for previous in trades:
                progress = ec.simulate_checked(previous, intraday, now_scan, check)
                closed = (progress["status"] == "done" and progress["exit_time"]
                          and pd.Timestamp(progress["exit_time"]) <= now_scan)
                rejected = (progress["status"] == "skipped" and progress.get("observed_at")
                            and pd.Timestamp(progress["observed_at"]) <= now_scan)
                still_open += not (closed or rejected)
            slots = min(ec.mc.MAX_OPEN - still_open, ec.mc.MAX_PER_DAY - len(trades))
            if now_scan.strftime("%H:%M") < FIRST_SCAN_BEFORE:
                slots = min(slots, FIRST_SCAN_CAP)
            for _, row in scan.sort_values("score", ascending=False).iterrows():
                if slots <= 0:
                    break
                if row["ticker"] in tickers:
                    continue
                result = {**row.to_dict(), **ec.simulate_checked(row, intraday, now, check)}
                tickers.add(row["ticker"])
                trades.append(result)
                taken.append(result)
                slots -= 1
    return pd.DataFrame(taken)


def half_target_days(trades: pd.DataFrame, intraday: dict) -> tuple[pd.Series, int]:
    """Day totals (£) and trade count under exit B for a set of trades."""
    done = trades[trades["status"] == "done"] if not trades.empty else trades
    rows = exit_rows(done, intraday)
    if not rows:
        return pd.Series(dtype=float), 0
    frame = pd.DataFrame(rows)
    return frame.groupby("day")["B_net"].sum(), len(frame)


# ---------------------------------------------------------------- comparison

def compare(days: pd.DataFrame, counts: dict[str, int]) -> pd.DataFrame:
    """Every variant's day totals against B."""
    split = days["day"].iloc[len(days) // 2]
    first = days["day"] < split
    rng = np.random.default_rng(13)
    names = {**EXIT_NAMES, **ENTRY_NAMES}
    rows = []
    for key in ["B"] + COMPARED:
        total = days[key]
        taken = counts[key]
        row = {"Variant": names[key], "Days": len(days), "Trades": int(taken), "Total (£)": float(total.sum()),
               "Per trade (£)": float(total.sum() / max(1, taken)),
               "First half (£)": float(total[first].sum()), "Second half (£)": float(total[~first].sum()),
               "Winning days": float((total > 0).mean()), "Worst day (£)": float(total.min()),
               "Best day (£)": float(total.max()), "Split at": split}
        if key != "B":
            diff = (total - days["B"]).to_numpy()
            row["Better than B: first half"] = bool(total[first].sum() > days["B"][first].sum())
            row["Better than B: second half"] = bool(total[~first].sum() > days["B"][~first].sum())
            row["Luck as good"] = el.sign_flip_luck(diff, rng)
            row["Helps"] = bool(row["Better than B: first half"] and row["Better than B: second half"]
                                and row["Luck as good"] is not None and row["Luck as good"] < LUCK_LIMIT)
        rows.append(row)
    return pd.DataFrame(rows)


def verdict_lines(table: pd.DataFrame) -> list[str]:
    base = table.iloc[0]
    lines = [f"{base['Variant']}: {int(base['Trades'])} trades over {int(base['Days'])} days, "
             f"£{base['Total (£)']:+.2f} ({base['Per trade (£)'] * 100:+.1f}p a trade; first half "
             f"£{base['First half (£)']:+.2f}, second £{base['Second half (£)']:+.2f}); "
             f"{base['Winning days']:.0%} winning days."]
    for _, r in table.iloc[1:].iterrows():
        head = "HELPS" if r.get("Helps") else "does not help"
        luck = f"{r['Luck as good']:.0%}" if pd.notna(r.get("Luck as good")) else "n/a"
        lines.append(f"{r['Variant']}: {head}. {int(r['Trades'])} trades, £{r['Total (£)']:+.2f} "
                     f"({r['Per trade (£)'] * 100:+.1f}p a trade; first half £{r['First half (£)']:+.2f}, second "
                     f"£{r['Second half (£)']:+.2f}); {r['Winning days']:.0%} winning days; luck as good {luck}.")
    return lines


def run(args) -> int:
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.kind != "trust" and c.index == "FTSE 100"]
    if args.limit:
        universe = universe[:args.limit]
    tickers = [c.yahoo_ticker for c in universe]
    print(f"Downloading 15-minute candles for {len(tickers)} FTSE 100 shares...")
    intraday = {t: sc.drop_untraded_bars(sc.completed_candles(f, "15m"))
                for t, f in sc.fetch_many(tickers, "15m", "60d").items()}
    intraday = {t: f for t, f in intraday.items() if len(f) > 100}
    ideas = fb.replay(universe, intraday, 15, args.warmup_days, args.workers, args.data_delay)
    if ideas.empty:
        print("No ideas.")
        return 1
    now = pd.Timestamp.now(tz=sc.UK_TZ)

    trades = ec.live_trader(ideas, intraday, now, el.live_check)
    done = trades[trades["status"] == "done"] if not trades.empty else trades
    items = exit_rows(done, intraday)
    per_trade = pd.DataFrame(items)
    exits = ["A", "B", "C", "G", "H1", "H2", "I"]
    days = per_trade.groupby("day")[[f"{k}_net" for k in exits]].sum()
    days.columns = exits
    counts = {k: len(per_trade) for k in exits}

    j_days, counts["J"] = half_target_days(ec.live_trader(ideas, intraday, now, small_target_check(intraday)), intraday)
    k_days, counts["K"] = half_target_days(capped_trader(ideas, intraday, now, el.live_check), intraday)
    all_days = sorted(set(days.index) | set(j_days.index) | set(k_days.index))
    days = days.reindex(all_days, fill_value=0.0)
    days["J"] = j_days.reindex(all_days, fill_value=0.0)
    days["K"] = k_days.reindex(all_days, fill_value=0.0)
    days = days.reset_index().rename(columns={"index": "day"})
    if "day" not in days.columns:
        days = days.rename(columns={days.columns[0]: "day"})

    table = compare(days, counts)
    why = per_trade[[f"{k}_why" for k in ("G", "H1", "H2", "I")]].apply(lambda s: s.value_counts()).fillna(0)
    verdict = [f"FTSE 100, {days['day'].iloc[0]} to {days['day'].iloc[-1]} ({len(days)} trading days with trades), "
               "today's live rules (fill check, stop at most 0.9%, target left at least the stop), 15-minute data "
               "delay. Baseline B = half target (live from 13 Nov). A variant helps only if its day totals beat B in "
               f"both halves and a sign-flip test says luck would do as well under {LUCK_LIMIT:.0%} of the time."]
    verdict += verdict_lines(table)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Verdict"
    ws.cell(1, 1, "Let it run: trailing exits, small targets, first-scan cap").font = bt.Font(name="Arial", bold=True, size=13)
    for n, line in enumerate(verdict, 3):
        bt.note(ws, n, line)
    ws.column_dimensions["A"].width = 150
    fb.write_sheet(wb, "Variants", table, ["Each variant's day totals against B (half target)."],
                   pct=["Winning days", "Luck as good"])
    fb.write_sheet(wb, "Days", days, ["Day totals (£) under each variant."])
    fb.write_sheet(wb, "How exits ended", why.reset_index().rename(columns={"index": "Reason"}),
                   ["How trades ended under the new exits (count of trades)."])
    per_trade.round(2).to_csv(out.with_name(out.stem + "_trades.csv"), index=False)
    saved = sc.save_workbook(wb, out)
    out.with_suffix(".json").write_text(json.dumps(
        {"generated": datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M"), "verdict": verdict,
         "variants": table.astype(object).where(table.notna(), None).to_dict("records"),
         "exit_reasons": why.to_dict()}, indent=1, default=str))
    for line in verdict:
        print(line)
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Let it run backtest (plan step 12)")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--warmup-days", type=int, default=12)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--data-delay", type=int, default=15)
    parser.add_argument("--out", default="results/ftse_let_it_run.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
