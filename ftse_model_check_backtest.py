"""Model check (24 Oct 2026; ideas register 29): is our live model set up the right way?

Phil's three-candle strategy (claude/three-candle-strategy.md in the project) trades a whole market,
once a day at the open, in the direction of the 4-hour 200 SMA. Our model trades single FTSE 100
shares all day from older zones, with no higher-timeframe trend filter. This replays the last ~60 days
of 15-minute candles and compares:

  A. Our current model: FTSE 100 shares, every 15-minute scan 08:30-15:15, the live scanner's rules
     (entry rules, worth-it check, target capped at the smaller of 2% and the share's typical daily
     move) and the live picker (up to 5 trades open at once, a closed trade frees its slot, 10 a day,
     one per share, no limit per scan); £100 a trade with share-dealing costs (0.10% + 0.5% stamp duty
     on buys). The morning picker is left out (no history of its views).
  B. A + the 4-hour 200 SMA trend filter: buys only when the price is above the average of the last
     200 four-hour closes (completed bars only), sells only below.
  C. A, trading only in the first two hours (scans with candles completed by 08:30 ... 10:00).
  D. B and C together.
  E. A, B, C, D again with spread-bet costs (0.10% of the amount, no stamp duty).
  F. The three-candle strategy on indices (benchmark):
       Candle 1 = first 15-minute candle after the open sets the range; Candle 2 trades beyond one end
       and closes back inside; Candle 3 breaks Candle 2 in the direction of the 4-hour 200 SMA (above =
       buy after a dip below Candle 1, below = sell after a poke above). Stop 1 point beyond Candle 2's
       far end, target the opposite end of Candle 1, closed at the end of the session. At most one
       trade a day. Spread-bet cost about 1 point (FTSE 100) / 0.5 point (S&P 500) per trade.
       - FTSE 100 index at the London open (08:00 UK)
       - FTSE 100 index at the New York open (14:30 UK)
       - S&P 500 index at the New York open (the original)

Compared on "profit per £1 at risk" (net profit / what the stops put at risk), which is fair across
£100 share trades and index trades of any size, plus win rate and both halves of the period.

4-hour bars: each session is split into blocks of 4 hours from its open (London 08:00-12:00, 12:00-16:00,
16:00-16:30; New York 09:30-13:30, 13:30-16:00), built from Yahoo's hourly candles (about 1 year).

    python ftse_model_check_backtest.py --out results/ftse_model_check.xlsx
"""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook

import ftse_backtest as bt
import ftse_outcomes as out
import ftse_risk_backtest as rb
import ftse_trade_scanner as sc
from ftse_sessions import is_session

MAX_OPEN, MAX_PER_DAY = 5, 10
STAKE = 100.0
SHARE_COSTS = {"Long": 0.60, "Short": 0.10}         # % of the amount: dealing 0.10% + stamp duty 0.5% on buys
SPREAD_COSTS = {"Long": 0.10, "Short": 0.10}
SMA_BARS = 200
FIRST_TWO_HOURS = "10:00"

INDICES = [  # name, Yahoo ticker, timezone, session open, session close, open used, spread-bet cost in points
    ("FTSE 100 index, London open (08:00 UK)", "^FTSE", "Europe/London", "08:00", "16:30", "08:00", 1.0),
    ("FTSE 100 index, New York open (14:30 UK)", "^FTSE", "Europe/London", "08:00", "16:30", "14:30", 1.0),
    ("S&P 500 index, New York open (the original)", "^GSPC", "America/New_York", "09:30", "16:00", "09:30", 0.5),
]
STOP_BUFFER_POINTS = 1.0


# ----------------------------------------------------------------------
# 4-hour 200 SMA
# ----------------------------------------------------------------------

def four_hour_closes(hourly: pd.DataFrame, session_open: str) -> pd.Series:
    """Close of each 4-hour block, indexed by the time the block ENDS (so it is only used once complete)."""
    if hourly.empty:
        return pd.Series(dtype=float)
    opening = pd.Timedelta(hours=int(session_open[:2]), minutes=int(session_open[3:]))
    since_open = hourly.index - hourly.index.normalize() - opening
    block = np.floor(since_open / pd.Timedelta(hours=4)).astype(int)
    rows = []
    for (day, number), part in hourly.groupby([hourly.index.date, np.asarray(block)]):
        start = pd.Timestamp(day).tz_localize(hourly.index.tz) + opening + pd.Timedelta(hours=4 * number)
        last_bar_end = part.index[-1] + pd.Timedelta(hours=1)
        rows.append((min(start + pd.Timedelta(hours=4), last_bar_end), float(part["close"].iloc[-1])))
    series = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([t for t, _ in rows])).sort_index()
    return series


def sma_at(closes: pd.Series, when: pd.Timestamp, bars: int = SMA_BARS) -> float | None:
    """Average of the last `bars` 4-hour closes of blocks that had ended by `when`."""
    done = closes[closes.index <= when]
    return float(done.tail(bars).mean()) if len(done) >= bars else None


# ----------------------------------------------------------------------
# A-E: our model on shares
# ----------------------------------------------------------------------

def share_setups(job) -> list[dict]:
    """Like ftse_risk_backtest.share_setups, with the live target cap applied before the entry rules."""
    company, data, cuts, interval_min = job
    step = pd.Timedelta(minutes=interval_min)
    ends = data.index + step
    found = []
    for cut in cuts:
        visible = data[ends <= cut]
        cfg = sc.INDEX_RULES[company.index]
        if len(visible) <= cfg.swing_lookback * 2 or visible.index[-1].date() != cut.date():
            continue
        zones = sc.qualify_zones(sc.cluster_zones(sc.find_swing_points(visible, cfg.swing_lookback), cfg.zone_tolerance_pct),
                                 visible, cfg)
        setups = sc.build_setups(company.company, company.yahoo_ticker, company.index, visible, zones, cfg)
        for setup in setups:
            setup.kind, setup.last_trade = company.kind, visible.index[-1] + step
            sc.cap_target(setup, visible)
            setup.filter_note = sc.entry_rule_note(setup, visible)
            found.append({"cut": cut, "setup": setup})
    return found


def replay_shares(universe, intraday, interval_min: int, warmup_days: int, workers: int) -> pd.DataFrame:
    all_days = sorted({stamp.normalize() for data in intraday.values() for stamp in data.index})
    sessions = [day for day in all_days if is_session(day)][warmup_days:]
    cuts = [cut for day in sessions for cut in rb.scan_cuts(day)]
    jobs = [(c, intraday[c.yahoo_ticker], cuts, interval_min) for c in universe if c.yahoo_ticker in intraday]
    print(f"Replaying {len(cuts)} scans over {len(sessions)} days for {len(jobs)} shares ({workers} workers)...")
    raw = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for number, rows in enumerate(pool.map(share_setups, jobs, chunksize=1), 1):
            raw.extend(rows)
            if number % 10 == 0:
                print(f"  ...{number}/{len(jobs)} shares replayed")
    by_cut: dict = {}
    for row in raw:
        by_cut.setdefault(row["cut"], []).append(row["setup"])
    rows = []
    for cut in sorted(by_cut):
        setups = by_cut[cut]
        newest = max(s.last_trade for s in setups)
        sc.mark_stale(setups, newest, 30)
        fresh = [s for s in setups if not s.stale]
        sc.annotate_conflicts(fresh)
        sc.apply_entry_rules(fresh)
        for s in sc.dedupe_setups(fresh):
            if s.conflict:
                continue
            rows.append({"day": str(cut.date()), "as_of": cut.strftime("%H:%M"),
                         "data_time": (newest - pd.Timedelta(minutes=interval_min)).isoformat(),
                         "run_time": (cut + rb.RUN_DELAY).strftime("%Y-%m-%d %H:%M:%S"), "mode": "watch",
                         "company": s.company, "ticker": s.ticker, "setup": s.setup_type, "direction": s.direction,
                         "score": s.quality_score, "entry": s.entry, "stop": s.stop, "target": s.target,
                         "stop_pct": abs(s.entry - s.stop) / s.entry * 100})
    return pd.DataFrame(rows)


def add_trend(ideas: pd.DataFrame, hourly: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Column 'with_trend': the idea's direction agrees with the share's 4-hour 200 SMA at the signal."""
    closes = {t: four_hour_closes(h, "08:00") for t, h in hourly.items()}
    flags = []
    for _, row in ideas.iterrows():
        series = closes.get(row["ticker"])
        signal = out.market_stamp(row["data_time"]) + pd.Timedelta(minutes=15)
        sma = sma_at(series, signal) if series is not None else None
        if sma is None:
            flags.append(None)
        else:
            flags.append(row["entry"] > sma if row["direction"] == "Long" else row["entry"] < sma)
    return ideas.assign(with_trend=flags)


def simulate_one(row, intraday, now) -> dict:
    data = intraday[row["ticker"]]
    signal_close = out.market_stamp(row["data_time"]) + pd.Timedelta(minutes=15)
    available = max(signal_close, out.market_stamp(row["run_time"]))
    sim = out.simulate(row["direction"], float(row["stop"]), float(row["target"]), available, data, 1, now, "15m")
    return {"status": sim.status, "outcome": sim.outcome, "fill": sim.fill, "exit_time": sim.exit_time,
            "r_gross": sim.r_gross, "fill_risk_pct": sim.fill_risk_pct}


def live_trader(ideas: pd.DataFrame, intraday, now) -> pd.DataFrame:
    """Live picker since 26 Oct 2026: up to 5 open at once (closed trades free their slot), 10 a day, one per share."""
    taken = []
    for _, day in ideas.groupby("day"):
        tickers, trades = set(), []
        for run_time, scan in day.groupby("run_time", sort=True):
            now_scan = out.market_stamp(run_time)
            still_open = sum(1 for t in trades if t["status"] != "done" or not t["exit_time"]
                             or pd.Timestamp(t["exit_time"]) > now_scan)
            slots = min(MAX_OPEN - still_open, MAX_PER_DAY - len(trades))
            for _, row in scan.sort_values("score", ascending=False).iterrows():
                if slots <= 0:
                    break
                if row["ticker"] in tickers:
                    continue
                result = {**row.to_dict(), **simulate_one(row, intraday, now)}
                if result["status"] == "skipped":          # never entered: frees the slot straight away
                    tickers.add(row["ticker"])
                    taken.append(result)
                    continue
                tickers.add(row["ticker"])
                trades.append(result)
                taken.append(result)
                slots -= 1
    return pd.DataFrame(taken)


def share_money(trades: pd.DataFrame, costs: dict[str, float]) -> pd.DataFrame:
    done = trades[trades["status"] == "done"].copy()
    if done.empty:
        return done
    move_pct = done["r_gross"] * done["fill_risk_pct"]
    cost_pct = done["direction"].map(costs)
    done["net_gbp"] = ((move_pct - cost_pct) * STAKE / 100).round(2)
    done["at_risk_gbp"] = done["fill_risk_pct"] * STAKE / 100
    return done


# ----------------------------------------------------------------------
# F: three-candle strategy on indices
# ----------------------------------------------------------------------

def three_candle(data: pd.DataFrame, closes: pd.Series, open_clock: str, close_clock: str, cost_points: float) -> pd.DataFrame:
    """One possible trade a day; returns completed trades with profit in points and the risk in points."""
    rows = []
    for day, session in data.groupby(data.index.date):
        start = pd.Timestamp(f"{day} {open_clock}").tz_localize(data.index.tz)
        end = pd.Timestamp(f"{day} {close_clock}").tz_localize(data.index.tz)
        bars = session[(session.index >= start) & (session.index < end)]
        if len(bars) < 4 or bars.index[0] != start or bars.index[1] != start + pd.Timedelta(minutes=15) \
                or bars.index[2] != start + pd.Timedelta(minutes=30):
            continue
        c1, c2, c3 = bars.iloc[0], bars.iloc[1], bars.iloc[2]
        sma = sma_at(closes, start + pd.Timedelta(minutes=30))
        if sma is None:
            continue
        price = float(c2["close"])
        inside = c1["low"] <= c2["close"] <= c1["high"]
        if price > sma and inside and c2["low"] < c1["low"] and c3["high"] > c2["high"] and float(c1["high"]) > float(c2["high"]):
            side, entry = "Buy", max(float(c3["open"]), float(c2["high"]))
            stop, target = float(c2["low"]) - STOP_BUFFER_POINTS, float(c1["high"])
        elif price < sma and inside and c2["high"] > c1["high"] and c3["low"] < c2["low"] and float(c1["low"]) < float(c2["low"]):
            side, entry = "Sell", min(float(c3["open"]), float(c2["low"]))
            stop, target = float(c2["high"]) + STOP_BUFFER_POINTS, float(c1["low"])
        else:
            rows.append({"day": str(day), "taken": False})
            continue
        long = side == "Buy"
        if (long and entry >= target) or (not long and entry <= target):
            rows.append({"day": str(day), "taken": False})
            continue
        outcome, exit_price, exit_time = None, None, None
        for stamp, bar in bars.iloc[2:].iterrows():
            hit_stop = bar["low"] <= stop if long else bar["high"] >= stop          # stop counted first (cautious)
            hit_target = bar["high"] >= target if long else bar["low"] <= target
            if hit_stop:
                outcome, exit_time, exit_price = "Stopped out", stamp, stop
                if stamp != bars.index[2]:                                   # a gap through the stop fills at the open
                    exit_price = min(float(bar["open"]), stop) if long else max(float(bar["open"]), stop)
                break
            if hit_target:
                outcome, exit_price, exit_time = "Target hit", target, stamp
                break
        if outcome is None:
            outcome, exit_price, exit_time = "Closed at end of day", float(bars["close"].iloc[-1]), bars.index[-1]
        points = (exit_price - entry) if long else (entry - exit_price)
        risk = abs(entry - stop)
        rows.append({"day": str(day), "taken": True, "direction": side, "entry": round(entry, 2), "stop": round(stop, 2),
                     "target": round(target, 2), "result": outcome, "exit": round(exit_price, 2),
                     "exit_time": exit_time.strftime("%H:%M"), "risk_points": round(risk, 2),
                     "points": round(points, 2), "net_points": round(points - cost_points, 2)})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# Summary and workbook
# ----------------------------------------------------------------------

def summary(name: str, net: pd.Series, risk: pd.Series, outcome: pd.Series, days: pd.Series, possible_days: int,
            split: str, money_label: str) -> dict:
    if net.empty:
        return {"Model": name, "Trades": 0, "Days in test": possible_days}
    per_risk = lambda mask: net[mask].sum() / risk[mask].sum() if mask.any() and risk[mask].sum() > 0 else None
    first, second = days < split, days >= split
    daily = net.groupby(days).sum()
    return {"Model": name, "Trades": len(net), "Days in test": possible_days, "Trades a day": len(net) / max(1, possible_days),
            "Won after costs": (net > 0).mean(), "Target hit": outcome.isin(["target", "Target hit"]).mean(),
            "Stop hit": outcome.isin(["stop", "Stopped out"]).mean(),
            "Profit per £1 at risk (£)": per_risk(net.notna()),
            "First half: per £1 at risk (£)": per_risk(first), "Second half: per £1 at risk (£)": per_risk(second),
            "Profitable days": (daily > 0).mean(), money_label: net.mean()}


def build_workbook(table: pd.DataFrame, share_trades: pd.DataFrame, index_trades: pd.DataFrame, period: str,
                   generated: str) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.cell(1, 1, "Model check: our model v the three-candle strategy").font = bt.Font(name="Arial", bold=True, size=13)
    bt.note(ws, 2, f"Generated {generated}. {period}. A-D: our live model on FTSE 100 shares, every 15-minute scan "
                   "08:30-15:15 with the live rules (including the target cap), up to 5 trades open at once and 10 a day, "
                   "£100 a trade. E: the same trades with spread-bet costs (0.10%, no stamp duty).")
    bt.note(ws, 3, "Trend filter: buys only above the 4-hour 200 SMA, sells only below. First two hours: scans with candles "
                   "completed by 10:00. F: the three-candle strategy on indices, one trade a day at most, stop 1 point beyond "
                   "Candle 2, target the far end of Candle 1, closed at the end of the session; spread-bet cost 1 point "
                   "(FTSE 100) or 0.5 point (S&P 500).")
    bt.note(ws, 4, "Compare on 'Profit per £1 at risk' (net profit / what the stops put at risk). About 40-60 trading days "
                   "of one market period: treat differences as hints, not proof. Indices: few trades, so very uncertain.")
    money = [c for c in table.columns if "(£)" in c]
    bt.write_table(ws, 6, table.round(4), money, ["Won after costs", "Target hit", "Stop hit", "Profitable days"])
    ws.column_dimensions["A"].width = 60
    for col in range(2, len(table.columns) + 1):
        ws.column_dimensions[bt.get_column_letter(col)].width = 13
    if not index_trades.empty:
        sheet = wb.create_sheet("Index trades")
        bt.write_table(sheet, 1, index_trades)
        sheet.column_dimensions["A"].width = 44
    if not share_trades.empty:
        sheet = wb.create_sheet("Share trades")
        cols = ["model", "day", "as_of", "company", "ticker", "setup", "direction", "entry", "stop", "target", "outcome",
                "exit_time", "net_gbp", "at_risk_gbp"]
        shown = share_trades[[c for c in cols if c in share_trades.columns]].copy()
        shown["setup"] = shown["setup"].replace({"Breakout": "Reclaim"})
        bt.write_table(sheet, 1, shown.round(3), ["net_gbp", "at_risk_gbp"])
        sheet.freeze_panes = "A2"
        sheet.column_dimensions["A"].width = 44
    return wb


def run(args) -> int:
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.kind != "trust" and c.index == "FTSE 100"]
    if args.limit:
        universe = universe[:args.limit]
    tickers = [c.yahoo_ticker for c in universe]
    print(f"Downloading 15-minute and hourly candles for {len(tickers)} shares and 2 indices...")
    raw = sc.fetch_many(tickers, "15m", "60d")
    intraday = {t: sc.drop_untraded_bars(sc.completed_candles(f, "15m")) for t, f in raw.items()}
    intraday = {t: f for t, f in intraday.items() if len(f) > 100}
    hourly_raw = sc.fetch_many(tickers, "1h", "1y")
    hourly = {t: sc.drop_untraded_bars(sc.completed_candles(f, "1h")) for t, f in hourly_raw.items()}
    rows, share_frames, index_frames = [], [], []

    # F first (quick), so a problem with the shares still leaves the benchmark
    for name, ticker, tz, s_open, s_close, open_clock, cost in INDICES:
        try:
            m15 = sc.fetch_data(ticker, "15m", "60d").tz_convert(tz)
            h1 = sc.fetch_data(ticker, "1h", "1y").tz_convert(tz)
        except Exception as error:
            print(f"  could not fetch {ticker}: {error}")
            rows.append({"Model": "F. " + name, "Trades": 0})
            continue
        closes = four_hour_closes(h1, s_open)
        trades = three_candle(m15, closes, open_clock, s_close, cost)
        possible = int(trades["day"].nunique()) if not trades.empty else 0
        done = trades[trades["taken"]] if not trades.empty else trades
        days_all = sorted(trades["day"].unique()) if not trades.empty else []
        split = days_all[len(days_all) // 2] if days_all else ""
        if done.empty:
            rows.append({"Model": "F. " + name, "Trades": 0, "Days in test": possible})
        else:
            rows.append(summary("F. " + name, done["net_points"], done["risk_points"], done["result"], done["day"],
                                possible, split, "Average points per trade after costs"))
            index_frames.append(done.assign(model=name)[["model"] + [c for c in done.columns if c not in ("model", "taken")]])
        print(f"  three-candle: {name}: {len(done)} trade(s) in {possible} day(s)")

    ideas = replay_shares(universe, intraday, 15, args.warmup_days, args.workers)
    if not ideas.empty:
        ideas = add_trend(ideas, hourly)
        days = sorted(ideas["day"].unique())
        split = days[len(days) // 2]
        now = pd.Timestamp.now(tz=sc.UK_TZ)
        early = ideas["as_of"] <= FIRST_TWO_HOURS
        trend = ideas["with_trend"].fillna(False).astype(bool)
        variants = [("A. Current model", ideas), ("B. + 4-hour 200 SMA trend filter", ideas[trend]),
                    ("C. First two hours only", ideas[early]), ("D. Trend filter + first two hours", ideas[trend & early])]
        for name, chosen in variants:
            trades = live_trader(chosen, intraday, now) if not chosen.empty else pd.DataFrame()
            for label, costs in (("share dealing", SHARE_COSTS), ("spread bet", SPREAD_COSTS)):
                prefix = name if label == "share dealing" else "E" + name[1:]
                full = f"{prefix} ({label})"
                money = share_money(trades, costs) if not trades.empty else pd.DataFrame()
                if money.empty:
                    rows.append({"Model": full, "Trades": 0, "Days in test": len(days)})
                    continue
                rows.append(summary(full, money["net_gbp"], money["at_risk_gbp"], money["outcome"], money["day"],
                                    len(days), split, "Average £ per £100 trade after costs"))
                if label == "share dealing":
                    share_frames.append(money.assign(model=name))
            print(f"  scored: {name}")
        period = f"Shares: {days[0]} to {days[-1]} ({len(days)} trading days after a {args.warmup_days}-day warm-up)"
    else:
        period = "Shares: no ideas passed the rules"
    table = pd.DataFrame(rows)
    order = table["Model"].str[0].map({"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5}).fillna(9)
    table = table.assign(_o=order).sort_values(["_o"], kind="stable").drop(columns="_o")
    generated = datetime.now(sc.UK_TZ).strftime("%Y-%m-%d %H:%M UK time")
    shares = pd.concat(share_frames) if share_frames else pd.DataFrame()
    indices = pd.concat(index_frames) if index_frames else pd.DataFrame()
    saved = sc.save_workbook(build_workbook(table, shares, indices, period, generated), Path(args.out))
    pd.set_option("display.width", 250)
    shown = [c for c in ["Model", "Trades", "Won after costs", "Profit per £1 at risk (£)", "First half: per £1 at risk (£)",
                         "Second half: per £1 at risk (£)"] if c in table.columns]
    print(table[shown].round(3).to_string(index=False))
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Model check: our model v the three-candle strategy")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--warmup-days", type=int, default=12)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--out", default="results/ftse_model_check.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
