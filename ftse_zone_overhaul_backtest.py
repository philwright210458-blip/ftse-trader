"""Zone overhaul backtest: fewer, stronger zones (plan step 2, idea 25; 2 Nov 2026).

The 15-minute scanner finds about 50 zones per share, so there is nearly always a "level" near the price and its
trades do no better than chance. This replays the live trader (FTSE 100, live rules, fill check, up to 5 open and
10 a day, £100 a trade, 10p costs, 15-minute data delay) with zones built in different ways, set in advance:

  A. Current zones
  B. Recent only: swing points from the last 10 trading days
  C. Merge close neighbours: zones closer than one typical 15-minute candle range become one
  D. Clear move away: a re-test only counts after a move away of at least 2 typical candle ranges
  E. All three together (B + C + D)

Each variant gets the chance test (the same trades in a random direction, and at random times that day). A variant
is worth putting in the live scanner (behind a setting, Phil's decision) only if it beats chance in BOTH halves
and is better than A in both halves. Paper research only.

The replays are slow (about an hour each), so the workflow runs one variant per job in parallel:

    python ftse_zone_overhaul_backtest.py --variant A --out-dir results/zone_overhaul
    python ftse_zone_overhaul_backtest.py --combine --out-dir results/zone_overhaul --out results/ftse_zone_overhaul.xlsx
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
import ftse_feasibility_backtest as fb
import ftse_trade_scanner as sc

VARIANTS = {
    "A": ("A. Current zones", {}),
    "B": ("B. Recent only (last 10 trading days)", {"recent_days": 10}),
    "C": ("C. Merge zones closer than 1 typical candle", {"merge_atr": 1.0}),
    "D": ("D. Re-test only after a move of 2 typical candles", {"move_atr": 2.0}),
    "E": ("E. All three (fewer, stronger zones)", {"recent_days": 10, "merge_atr": 1.0, "move_atr": 2.0}),
}
LUCK_LIMIT = 0.05


def run_variant(args) -> int:
    name, rules = VARIANTS[args.variant]
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.kind != "trust" and c.index == "FTSE 100"]
    if args.limit:
        universe = universe[:args.limit]
    tickers = [c.yahoo_ticker for c in universe]
    print(f"{name}: downloading 15-minute candles for {len(tickers)} FTSE 100 shares...")
    intraday = {t: sc.drop_untraded_bars(sc.completed_candles(f, "15m")) for t, f in sc.fetch_many(tickers, "15m", "60d").items()}
    intraday = {t: f for t, f in intraday.items() if len(f) > 100}
    ideas = fb.replay(universe, intraday, 15, args.warmup_days, args.workers, args.data_delay, zone_rules=rules or None)
    result = {"variant": args.variant, "name": name, "rules": rules, "ideas": len(ideas),
              "days": sorted(ideas["day"].unique().tolist()) if not ideas.empty else [], "trades": [], "skipped": 0,
              "zones_per_share": float(pd.to_numeric(ideas.get("zones_at_scan"), errors="coerce").mean()) if not ideas.empty else None,
              "zones_near_per_idea": float(pd.to_numeric(ideas.get("zones_near_at_scan"), errors="coerce").mean()) if not ideas.empty else None}
    if not ideas.empty:
        now = pd.Timestamp.now(tz=sc.UK_TZ)
        trades = ec.live_trader(ideas, intraday, now, ec.half_left)
        result["skipped"] = int((trades["status"] == "skipped").sum()) if not trades.empty else 0
        done = ec.money(trades) if not trades.empty else trades
        for _, t in (done.iterrows() if len(done) else []):
            trade = t.to_dict()
            frame = intraday[trade["ticker"]]
            exits = fb.exit_results(trade, frame)
            chance = fb.chance_inputs(trade, frame)
            result["trades"].append({
                "day": trade["day"], "ticker": trade["ticker"], "setup": trade["setup"], "direction": trade["direction"],
                "net_gbp": float(trade["net_gbp"]), "outcome": trade["outcome"],
                "actual": float(exits[fb.EXITS[0]]), "opposite": float(chance["opposite"]),
                "times": [float(x) for x in chance["random_times"]],
                "fill_time": fb.fill_candle(trade).strftime("%H:%M"),
                "zones_at_scan": trade.get("zones_at_scan"), "zones_near_at_scan": trade.get("zones_near_at_scan")})
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"variant_{args.variant}.json"
    path.write_text(json.dumps(result, default=str))
    print(f"{name}: {len(result['trades'])} trades from {result['ideas']} ideas; saved {path}")
    return 0


def summarise(result: dict, split: str) -> dict:
    trades = pd.DataFrame(result["trades"])
    row = {"Zones": result["name"], "Zones per share at a scan": result.get("zones_per_share"),
           "Zones within 1.2% of a trade idea": result.get("zones_near_per_idea"), "Ideas": result["ideas"]}
    if trades.empty:
        return {**row, "Trades": 0}
    first, second = trades[trades["day"] < split], trades[trades["day"] >= split]
    row.update({"Trades": len(trades), "Trades a day": len(trades) / max(1, len(result["days"])),
                "Skipped at the fill": result["skipped"], "Stop hit": float((trades["outcome"] == "stop").mean()),
                "Won after costs": float((trades["net_gbp"] > 0).mean()),
                "Average per £100 trade (£)": float(trades["net_gbp"].mean()), "Total (£)": float(trades["net_gbp"].sum()),
                "First half: average (£)": float(first["net_gbp"].mean()) if len(first) else None,
                "Second half: average (£)": float(second["net_gbp"].mean()) if len(second) else None})
    for label, part in (("whole", trades), ("first half", first), ("second half", second)):
        if part.empty:
            continue
        c = fb.chance_test(part["actual"].to_numpy(), part["opposite"].to_numpy(), part["times"].tolist())
        row[f"Random direction as good: {label}"] = c["random_direction_as_good"]
        row[f"Random time as good: {label}"] = c["random_time_as_good"]
    return row


def beats_chance(row: dict, half: str) -> bool:
    d, t = row.get(f"Random direction as good: {half}"), row.get(f"Random time as good: {half}")
    return d is not None and t is not None and d < LUCK_LIMIT and t < LUCK_LIMIT


def verdict_lines(table: pd.DataFrame) -> list[str]:
    base = table.iloc[0].to_dict()
    lines = []
    for _, r in table.iterrows():
        row = r.to_dict()
        if not row.get("Trades"):
            lines.append(f"{row['Zones']}: no trades.")
            continue
        chance = "beat chance in both halves" if all(beats_chance(row, h) for h in ("first half", "second half")) else \
            f"did not beat chance (random direction as good {row.get('Random direction as good: whole', float('nan')):.0%}, " \
            f"random time {row.get('Random time as good: whole', float('nan')):.0%})"
        text = (f"{row['Zones']}: {chance}; {int(row['Trades'])} trades, {row['Average per £100 trade (£)'] * 100:+.1f}p a trade "
                f"(first half {row['First half: average (£)'] * 100:+.1f}p, second {row['Second half: average (£)'] * 100:+.1f}p); "
                f"{row['Zones per share at a scan']:.0f} zones per share.")
        if row["Zones"] != base["Zones"] and base.get("Trades"):
            better = all(row[f"{h}: average (£)"] is not None and row[f"{h}: average (£)"] > base[f"{h}: average (£)"]
                         for h in ("First half", "Second half"))
            text += " Better than A in both halves." if better else " Not better than A in both halves."
        lines.append(text)
    return lines


def combine(args) -> int:
    results = {}
    for key in VARIANTS:
        path = Path(args.out_dir) / f"variant_{key}.json"
        if path.exists():
            results[key] = json.loads(path.read_text())
        else:
            print(f"Missing {path}")
    if "A" not in results or not results["A"]["days"]:
        print("No results for A (current zones).")
        return 1
    days = results["A"]["days"]
    split = days[len(days) // 2]
    table = pd.DataFrame([summarise(results[k], split) for k in VARIANTS if k in results])
    verdict = [f"FTSE 100, {days[0]} to {days[-1]} ({len(days)} trading days), 15-minute data delay, live rules with the "
               f"fill check; split at {split}. A variant is worth adopting only if it beats chance in both halves "
               f"(random direction and random time each as good under {LUCK_LIMIT:.0%} of the time) and beats A in both halves."]
    verdict += verdict_lines(table)
    wb = Workbook()
    ws = wb.active
    ws.title = "Verdict"
    ws.cell(1, 1, "Zone overhaul: fewer, stronger zones").font = bt.Font(name="Arial", bold=True, size=13)
    for n, line in enumerate(verdict, 3):
        bt.note(ws, n, line)
    ws.column_dimensions["A"].width = 150
    pct = [c for c in table.columns if "as good" in c] + ["Stop hit", "Won after costs"]
    fb.write_sheet(wb, "Variants", table, ["Each zone rule replayed through the live trader."], pct=pct)
    trades = pd.concat([pd.DataFrame(r["trades"]).assign(zones=r["name"]) for r in results.values() if r["trades"]],
                       ignore_index=True)
    if not trades.empty:
        trades.drop(columns=["times"]).to_csv(Path(args.out).with_name(Path(args.out).stem + "_trades.csv"), index=False)
    saved = sc.save_workbook(wb, Path(args.out))
    Path(args.out).with_suffix(".json").write_text(json.dumps(
        {"generated": datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M"), "verdict": verdict,
         "variants": table.astype(object).where(table.notna(), None).to_dict("records")}, indent=1, default=str))
    for line in verdict:
        print(line)
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Zone overhaul backtest (plan step 2)")
    parser.add_argument("--variant", choices=list(VARIANTS))
    parser.add_argument("--combine", action="store_true")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--warmup-days", type=int, default=12)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--data-delay", type=int, default=15)
    parser.add_argument("--out-dir", default="results/zone_overhaul")
    parser.add_argument("--out", default="results/ftse_zone_overhaul.xlsx")
    args = parser.parse_args()
    if not args.combine and not args.variant:
        parser.error("give --variant or --combine")
    sys.exit(combine(args) if args.combine else run_variant(args))


if __name__ == "__main__":
    main()
