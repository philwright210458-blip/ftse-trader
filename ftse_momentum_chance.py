"""Chance test for Claude's monthly momentum model (plan step 6, idea 18; agreed by Phil 3 Nov 2026).

The question: does ranking shares by momentum beat picking shares AT RANDOM under exactly the same rules? Each
random portfolio uses the same share list, the same 200-day-average rule, the same market filter, the same "hold
10, keep while in the top 20" rule, the same monthly timing and the same costs. The only difference: each month the
eligible shares are put in a random order instead of momentum order. If random portfolios do as well, the momentum
ranking adds nothing (and the backtest's growth came from the share list and the rules around it).

FTSE 350 shares, no investment trusts (as the momentum backtest), prices with dividends. Variants set in advance
(3 Nov, before results):

  A. As now (with the market filter), 2011 to today: the backtest that showed 18.7% a year
  B. Without the market filter, 2011 to today
  C. As now, longer history: from 2006 (includes the 2008 crash)

Pass mark (for each variant): random portfolios as good under 5% of the time in BOTH halves (growth a year,
split at the middle date), AND the model beats holding every share on the list equally in both halves.
Paper research only; not financial advice.

    python ftse_momentum_chance.py --draws 500 --out results/ftse_momentum_chance.xlsx
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook

import ftse_backtest as bt
import ftse_feasibility_backtest as fb
import ftse_momentum as mm
import ftse_trade_scanner as sc

DRAWS = 500
LUCK_LIMIT = 0.05
VARIANTS = {
    "A": ("A. As now (market filter), from 2011", {"start": "2011-01-01", "market_filter": True}),
    "B": ("B. No market filter, from 2011", {"start": "2011-01-01", "market_filter": False}),
    "C": ("C. As now, longer history from 2006", {"start": "2006-01-01", "market_filter": True}),
}


def random_ranker(rng: np.random.Generator):
    """Same eligible shares as the real ranking, in a random order (a fresh order every month)."""
    def rank(close_ff, sma, counts, seen, position):
        ranked = mm.ranking(close_ff, sma, counts, seen, position)
        if ranked.empty:
            return ranked
        ranked = ranked.copy()
        ranked["score"] = rng.random(len(ranked))
        return ranked.sort_values("score", ascending=False)
    return rank


def growth(values: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """Average yearly growth between two dates (values forward-filled)."""
    v = values.dropna()
    v = v[(v.index >= start) & (v.index <= end)]
    if len(v) < 2:
        return float("nan")
    years = (v.index[-1] - v.index[0]).days / 365.25
    return float((v.iloc[-1] / v.iloc[0]) ** (1 / years) - 1) if years > 0 else float("nan")


def three_growths(values: pd.Series, split: pd.Timestamp) -> tuple[float, float, float]:
    start, end = values.index[0], values.index[-1]
    return growth(values, start, end), growth(values, start, split), growth(values, split, end)


def luck(actual: float, randoms: np.ndarray) -> float | None:
    randoms = randoms[np.isfinite(randoms)]
    if not np.isfinite(actual) or len(randoms) == 0:
        return None
    return float(np.mean(randoms >= actual))


def run_variant(name: str, rules: dict, open_, close, market, draws: int, seed: int) -> dict:
    settings = mm.Settings(start=rules["start"], market_filter=rules["market_filter"])
    model = mm.run_model(open_, close, market, settings)
    values = model.values
    split = values.index[len(values) // 2]
    whole, first, second = three_growths(values, split)
    everything = mm.equal_weight_everything(close, values.index[0], values.index[-1], 1000.0)
    e_whole, e_first, e_second = three_growths(everything, split)
    rng = np.random.default_rng(seed)
    rand = np.empty((draws, 3))
    began = time.time()
    for d in range(draws):
        r = mm.run_model(open_, close, market, settings, rank_fn=random_ranker(rng))
        rand[d] = three_growths(r.values, split)
        if (d + 1) % 50 == 0:
            print(f"  {name}: {d + 1}/{draws} random portfolios ({time.time() - began:.0f}s)")
    return {
        "Variant": name, "Start": values.index[0].date().isoformat(), "End": values.index[-1].date().isoformat(),
        "Split": split.date().isoformat(),
        "Model: growth a year": whole, "Model: first half": first, "Model: second half": second,
        "Every share equally: growth a year": e_whole, "Every share equally: first half": e_first,
        "Every share equally: second half": e_second,
        "Random portfolios: average growth a year": float(np.nanmean(rand[:, 0])),
        "Random portfolios: first half": float(np.nanmean(rand[:, 1])),
        "Random portfolios: second half": float(np.nanmean(rand[:, 2])),
        "Random portfolios: best 5% did at least": float(np.nanpercentile(rand[:, 0], 95)),
        "Random as good: whole": luck(whole, rand[:, 0]),
        "Random as good: first half": luck(first, rand[:, 1]),
        "Random as good: second half": luck(second, rand[:, 2]),
        "Months in cash": model.months_in_cash, "Costs paid per £1,000 (£)": round(model.costs, 2),
        "Random draws": draws,
    }


def passes(row: dict) -> bool:
    halves = ("first half", "second half")
    lucky = all(row.get(f"Random as good: {h}") is not None and row[f"Random as good: {h}"] < LUCK_LIMIT for h in halves)
    beats_list = all(row[f"Model: {h}"] > row[f"Every share equally: {h}"] for h in halves)
    return lucky and beats_list


def verdict_lines(rows: list[dict]) -> list[str]:
    lines = []
    for row in rows:
        head = "PASSES" if passes(row) else "does not pass"
        lines.append(
            f"{row['Variant']} ({row['Start']} to {row['End']}): {head}. Model {row['Model: growth a year']:.1%} a year "
            f"(first half {row['Model: first half']:.1%}, second {row['Model: second half']:.1%}); random portfolios "
            f"{row['Random portfolios: average growth a year']:.1%} on average (best 5% at least "
            f"{row['Random portfolios: best 5% did at least']:.1%}); every share equally {row['Every share equally: growth a year']:.1%}. "
            f"Random as good {row['Random as good: whole']:.0%} of the time (first half {row['Random as good: first half']:.0%}, "
            f"second {row['Random as good: second half']:.0%}).")
    return lines


def load_prices(universe_path: str, limit: int | None, earliest: str):
    universe = [c for c in sc.load_universe(Path(universe_path)) if c.kind != "trust"]
    if limit:
        universe = universe[:limit]
    tickers = [c.yahoo_ticker for c in universe]
    print(f"Downloading daily prices for {len(tickers)} shares and the FTSE 100 from {earliest}...")
    open_, close, dividends = mm.download(tickers + [mm.MARKET_TICKER], earliest)
    if close.empty or mm.MARKET_TICKER not in close.columns:
        return None
    market = close[mm.MARKET_TICKER]
    shares = [t for t in tickers if t in close.columns and close[t].notna().sum() > 0]
    open_, close = mm.clean(open_[shares], close[shares])
    open_, close, _ = mm.with_dividends(open_, close, dividends)
    close = close.loc[market.dropna().index.min():]
    return open_.reindex(close.index), close, market


def run(args) -> int:
    earliest = min(pd.Timestamp(r["start"]) for _, r in VARIANTS.values()) - pd.DateOffset(months=15)
    loaded = load_prices(args.universe, args.limit, earliest.strftime("%Y-%m-%d"))
    if loaded is None:
        print("No data downloaded (is Yahoo reachable?).")
        return 1
    open_, close, market = loaded
    keys = args.variants.split(",") if args.variants else list(VARIANTS)
    rows = [run_variant(VARIANTS[k][0], VARIANTS[k][1], open_, close, market, args.draws, seed=20 + n)
            for n, k in enumerate(keys)]
    verdict = [f"Claude's monthly momentum model, {close.shape[1]} FTSE 350 shares (no investment trusts), prices with "
               f"dividends. Random portfolios = the same rules and costs with the eligible shares in a random order each "
               f"month, {args.draws} per variant. Pass: random as good under {LUCK_LIMIT:.0%} of the time in both halves "
               "AND better than every share on the list equally in both halves."]
    verdict += verdict_lines(rows)
    table = pd.DataFrame(rows)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Verdict"
    ws.cell(1, 1, "Momentum model: chance test (plan step 6)").font = bt.Font(name="Arial", bold=True, size=13)
    for n, line in enumerate(verdict, 3):
        bt.note(ws, n, line)
    bt.note(ws, len(verdict) + 4, "Caution: today's share list, so companies that failed or left the index are missing. "
                                  "Random portfolios share that bias, so the comparison is fair on it. Paper research only.")
    ws.column_dimensions["A"].width = 150
    pct = [c for c in table.columns if "growth" in c or "half" in c or "best 5%" in c or "as good" in c]
    fb.write_sheet(wb, "Variants", table, ["The model against random portfolios under the same rules."], pct=pct)
    saved = sc.save_workbook(wb, out)
    out.with_suffix(".json").write_text(json.dumps(
        {"generated": datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M"), "verdict": verdict,
         "variants": table.astype(object).where(table.notna(), None).to_dict("records")}, indent=1, default=str))
    for line in verdict:
        print(line)
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Momentum model chance test (plan step 6)")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--draws", type=int, default=DRAWS)
    parser.add_argument("--variants", default=None, help="e.g. A,C (default: all)")
    parser.add_argument("--out", default="results/ftse_momentum_chance.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
