"""Chance test for the daily structure pullback (plan step 4a, idea 24; agreed by Phil 2 Nov 2026).

The question: do the structure pullback's ENTRIES beat buying the same share on a random day? Everything else is
kept the same for the random trades: the same share, the same year, the same stop distance (as % of the entry),
the same 2x-the-stop target and the same 60-day limit. If random days do as well, the model's pattern adds nothing.

FTSE 100 (no investment trusts), daily candles from 21 Oct 2006 (as the 20-year backtest), one open trade per share,
£250 a trade, IG share dealing costs £1.50. Variants set in advance (2 Nov, before results):

  A. As now (2x-the-stop target, no dividends)
  B. Dividends added (any ex-dividend date after the entry day, up to and including the exit day)
  C. No chasing: skip the trade if the next open is more than 1 typical daily range above the signal day's close

Random trades get the same treatment as the real ones in each variant (dividends in B; the same gap rule in C).
Pass mark: random days as good under 5% of the time in BOTH halves (trades split at the middle entry date), and
positive after costs in both halves. Paper research only; not financial advice.

    python ftse_structure_chance_backtest.py --out results/ftse_structure_chance.xlsx
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook

import ftse_backtest as bt
import ftse_dip_backtest as dm
import ftse_feasibility_backtest as fb
import ftse_structure as st
import ftse_trade_scanner as sc

STAKE, COSTS = 250.0, 1.50
DRAWS = 2000
LUCK_LIMIT = 0.05
CHASE_ATR = 1.0
VARIANTS = {
    "A": ("A. As now (2x-the-stop target)", {}),
    "B": ("B. Dividends added", {"dividends": True}),
    "C": ("C. No chasing (skip a gap of more than 1 typical daily range)", {"chase_atr": CHASE_ATR}),
}


def simulate(o, h, l, c, div, n: int, risk: float, hold: int, dividends: bool) -> dict | None:
    """Buy at the open of day n+1 with the stop `risk` (fraction) below and the target 2x that above.

    Same exit rules as ftse_structure_backtest.trade: stop counted first, gaps fill at the open, sold at the close
    after `hold` trading days. None if the trade would still be open at the end of the data."""
    if n + 1 >= len(o) or not math.isfinite(o[n + 1]) or risk <= 0:
        return None
    fill = o[n + 1]
    stop, target = fill * (1 - risk), fill * (1 + 2 * risk)
    last = n + hold
    if last >= len(o):
        return None
    exit_k, price, outcome = last, c[last], "60 days"
    for k in range(n + 1, last + 1):
        if l[k] <= stop:
            exit_k, price, outcome = k, min(o[k], stop), "stop"
            break
        if h[k] >= target:
            exit_k, price, outcome = k, max(o[k], target), "target"
            break
    dividend = float(div[n + 2:exit_k + 1].sum()) if dividends else 0.0   # ex-dates after the entry day
    gross = (price - fill + dividend) / fill * STAKE
    return {"k": exit_k, "fill": fill, "exit": price, "outcome": outcome, "dividend": dividend, "gross": gross}


def chased(o, c, atr, n: int, limit: float | None) -> bool:
    return limit is not None and n + 1 < len(o) and o[n + 1] > c[n] + limit * atr[n]


def real_trades(frame: pd.DataFrame, rules: dict) -> list[dict]:
    o, h, l, c = (frame[k].to_numpy() for k in ("open", "high", "low", "close"))
    div = frame["dividend"].to_numpy() if "dividend" in frame else np.zeros(len(frame))
    atr = st.atr(frame)
    out, busy = [], -1
    for s in st.signals(frame):
        n = s["t"]
        if n <= busy or n + 1 >= len(frame):
            continue
        if chased(o, c, atr, n, rules.get("chase_atr")):
            continue
        fill = o[n + 1]
        if not (math.isfinite(fill) and s["stop"] < fill):
            continue
        risk = (fill - s["stop"]) / fill
        t = simulate(o, h, l, c, div, n, risk, st.MAX_HOLD, rules.get("dividends", False))
        if t is None:
            continue
        busy = t["k"]
        out.append({"n": n, "entry_day": frame.index[n + 1].date().isoformat(), "exit_day": frame.index[t["k"]].date().isoformat(),
                    "days": t["k"] - n, "risk_pct": round(risk * 100, 2), "target_pct": round(risk * 200, 2),
                    "fill": round(float(fill), 4), "exit": round(float(t["exit"]), 4), "outcome": t["outcome"],
                    "dividend": round(t["dividend"], 4), "gross": round(t["gross"], 2), "net": round(t["gross"] - COSTS, 2)})
    return out


def random_results(frame: pd.DataFrame, trade: dict, rules: dict, draws: int, rng: np.random.Generator) -> np.ndarray:
    """Before-cost £ results of the same trade started on `draws` random days in the same year (same share)."""
    o, h, l, c = (frame[k].to_numpy() for k in ("open", "high", "low", "close"))
    div = frame["dividend"].to_numpy() if "dividend" in frame else np.zeros(len(frame))
    atr = st.atr(frame)
    year = pd.Timestamp(trade["entry_day"]).year
    days = np.flatnonzero((frame.index.year == year) & (np.arange(len(frame)) + st.MAX_HOLD + 1 < len(frame)))
    days = days[days >= 1]
    if rules.get("chase_atr") is not None:
        days = np.array([n for n in days if not chased(o, c, atr, n, rules["chase_atr"])], dtype=int)
    if len(days) == 0:
        return np.full(draws, np.nan)
    risk = trade["risk_pct"] / 100
    cache: dict[int, float] = {}
    picks = rng.choice(days, size=draws)
    out = np.empty(draws)
    for i, n in enumerate(picks):
        n = int(n)
        if n not in cache:
            t = simulate(o, h, l, c, div, n, risk, st.MAX_HOLD, rules.get("dividends", False))
            cache[n] = t["gross"] if t else np.nan
        out[i] = cache[n]
    return out


def luck(actual: np.ndarray, randoms: np.ndarray) -> float | None:
    """Share of draws where random days' average (before costs) was at least the real trades' average."""
    if len(actual) == 0:
        return None
    means = np.nanmean(randoms, axis=0)                 # randoms: trades x draws
    return float(np.mean(means >= np.mean(actual)))


def summarise(name: str, trades: pd.DataFrame, randoms: np.ndarray, split: str) -> dict:
    row = {"Variant": name, "Trades": len(trades)}
    if trades.empty:
        return row
    first = (trades["entry_day"] < split).to_numpy()
    years = pd.to_datetime(trades["entry_day"]).dt.year
    yearly = trades.groupby(years)["net"].sum()
    row.update({
        "Trades a year": len(trades) / max(1, years.nunique()),
        "Target hit": float((trades["outcome"] == "target").mean()),
        "Stop hit": float((trades["outcome"] == "stop").mean()),
        "Avg stop distance": float(trades["risk_pct"].mean() / 100),
        "Avg days held": float(trades["days"].mean()),
        "Before costs per £250 (£)": float(trades["gross"].mean()),
        "Random days, before costs (£)": float(np.nanmean(randoms)),
        "After costs per £250 (£)": float(trades["net"].mean()),
        "After costs: first half (£)": float(trades.loc[first, "net"].mean()) if first.any() else None,
        "After costs: second half (£)": float(trades.loc[~first, "net"].mean()) if (~first).any() else None,
        "Random as good: whole": luck(trades["gross"].to_numpy(), randoms),
        "Random as good: first half": luck(trades.loc[first, "gross"].to_numpy(), randoms[first]),
        "Random as good: second half": luck(trades.loc[~first, "gross"].to_numpy(), randoms[~first]),
        "Years in profit": f"{int((yearly > 0).sum())} of {len(yearly)}",
    })
    return row


def passes(row: dict) -> bool:
    halves = ("first half", "second half")
    return (row.get("Trades", 0) > 0
            and all(row.get(f"Random as good: {h}") is not None and row[f"Random as good: {h}"] < LUCK_LIMIT for h in halves)
            and all((row.get(f"After costs: {h} (£)") or 0) > 0 for h in halves))


def verdict_lines(table: pd.DataFrame) -> list[str]:
    lines = []
    for _, r in table.iterrows():
        row = r.to_dict()
        if not row.get("Trades"):
            lines.append(f"{row['Variant']}: no trades.")
            continue
        head = "PASSES: beats random days in both halves and makes money after costs in both" if passes(row) else \
            "does not pass"
        lines.append(
            f"{row['Variant']}: {head}. {int(row['Trades'])} trades; before costs £{row['Before costs per £250 (£)']:+.2f} "
            f"per £250 v £{row['Random days, before costs (£)']:+.2f} for random days; after costs "
            f"£{row['After costs per £250 (£)']:+.2f} (first half £{row['After costs: first half (£)']:+.2f}, second "
            f"£{row['After costs: second half (£)']:+.2f}); random days as good {row['Random as good: whole']:.0%} of the time "
            f"(first half {row['Random as good: first half']:.0%}, second {row['Random as good: second half']:.0%}).")
    return lines


def run_all(data: dict[str, pd.DataFrame], draws: int = DRAWS, seed: int = 11) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    rows, all_trades, split = [], [], None
    for key, (name, rules) in VARIANTS.items():
        rng = np.random.default_rng(seed)
        trades, randoms = [], []
        for ticker in sorted(data):
            frame = data[ticker]
            for t in real_trades(frame, rules):
                trades.append({**t, "ticker": ticker})
                randoms.append(random_results(frame, t, rules, draws, rng))
        tf = pd.DataFrame(trades)
        if tf.empty:
            rows.append({"Variant": name, "Trades": 0})
            continue
        order = np.argsort(tf["entry_day"].to_numpy(), kind="stable")
        tf, rand = tf.iloc[order].reset_index(drop=True), np.vstack(randoms)[order]
        if split is None:                                   # split at A's middle entry date, used for every variant
            split = tf["entry_day"].iloc[len(tf) // 2]
        rows.append(summarise(name, tf, rand, split))
        tf["random_avg"] = np.nanmean(rand, axis=1).round(2)
        all_trades.append(tf.assign(variant=key))
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    return pd.DataFrame(rows), trades, split or ""


def run(args) -> int:
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.kind != "trust" and c.index == "FTSE 100"]
    if args.limit:
        universe = universe[:args.limit]
    print(f"Downloading daily candles (with dividends) for {len(universe)} FTSE 100 shares from {args.start}...")
    data = dm.download([c.yahoo_ticker for c in universe], args.start)
    if not data:
        print("No data downloaded (is Yahoo reachable?).")
        return 1
    table, trades, split = run_all(data, args.draws)
    period = f"{min(f.index[0] for f in data.values()).date()} to {max(f.index[-1] for f in data.values()).date()}"
    verdict = [f"Daily structure pullback, {len(data)} FTSE 100 shares, {period}, £250 a trade, IG share dealing costs "
               f"£{COSTS:.2f}; split at {split}. Random days = the same share, same year, same stop distance, 2x target "
               f"and 60-day limit, {args.draws} draws. Pass: random days as good under {LUCK_LIMIT:.0%} of the time in both "
               "halves AND positive after costs in both halves."]
    verdict += verdict_lines(table)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Verdict"
    ws.cell(1, 1, "Daily structure pullback: chance test (plan step 4a)").font = bt.Font(name="Arial", bold=True, size=13)
    for n, line in enumerate(verdict, 3):
        bt.note(ws, n, line)
    bt.note(ws, len(verdict) + 4, "Caution: today's FTSE 100 list (companies that dropped out are missing). Paper research "
                                  "only; not financial advice.")
    ws.column_dimensions["A"].width = 150
    pct = [c for c in table.columns if "as good" in c] + ["Target hit", "Stop hit", "Avg stop distance"]
    fb.write_sheet(wb, "Variants", table, ["Each variant's real trades against the same trades on random days."], pct=pct)
    if not trades.empty:
        trades.drop(columns=["n"]).to_csv(out.with_name(out.stem + "_trades.csv"), index=False)
    saved = sc.save_workbook(wb, out)
    out.with_suffix(".json").write_text(json.dumps(
        {"generated": datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M"), "verdict": verdict,
         "variants": table.astype(object).where(table.notna(), None).to_dict("records")}, indent=1, default=str))
    for line in verdict:
        print(line)
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily structure pullback chance test (plan step 4a)")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--start", default="2006-10-21")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--draws", type=int, default=DRAWS)
    parser.add_argument("--out", default="results/ftse_structure_chance.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
