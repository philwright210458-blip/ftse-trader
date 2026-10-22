"""Daily-context day trader (Phil's process, 22 Oct 2026): backtest on 5-minute candles, FTSE 100 shares.

    python ftse_daytrader.py --out results/ftse_daytrader.xlsx

Phil's process, written as rules a computer can check (set once, before testing):

1. Shortlist at 08:00, using daily candles up to yesterday only:
   * Daily trend (needed): up if yesterday's close > 20-day average > 50-day average and the
     20-day average is higher than 5 days earlier; down is the mirror. No trend = not considered.
   * Near a key daily level (+1): within 1 typical daily range of a price where at least 3 daily
     swing highs/lows (last 120 days) sit within half a typical daily range of each other.
   * Yesterday's big mover (+1): yesterday moved at least 1.5 typical daily ranges (close to close),
     or opened with a gap of at least 1%.
   Score = 2 (trend) + level + mover; the 5 best scores (ties: biggest move) make the shortlist.
2. Watch 08:00-08:30: the share must move in its daily trend's direction over those 30 minutes
   (08:30 price above the 08:00 open for an uptrend, below for a downtrend), or it is dropped
   for the day.
3. Entries 08:30-15:30, checked every 15 minutes on the three new 5-minute candles: only in the
   daily trend's direction. Trigger (buys; sells mirror): the 5-minute 20-period average is rising,
   price dipped to it (a low within 0.1%) in the last 4 candles, and a candle closes up and above
   the previous candle's high.
4. Stop just below the lowest low of the last 30 minutes (0.05% buffer). Target: the larger of a
   1% move and 2x the stop distance. Entry at the open of the first 5-minute candle after the
   check finishes (2 minutes after the check time). Closed at the last price of the day if
   neither is hit. Stop counted first if both fall in one candle.
5. At most 3 trades a day, one per share, at most one new trade per check. £250 a trade.

Costs per £250: spread bet (closed the same day, so no overnight charge) 25p (0.10% price gap).
Buys through IG share dealing would cost £1.50 (stamp duty), shown for comparison.

Yardsticks (same costs, same exits):
* No shortlist: the same watch and trigger rules on every FTSE 100 share with a daily trend.
* No trigger: the shortlist, entered at the first check (08:45) in the trend's direction.

Limits: Yahoo keeps only 60 days of 5-minute candles, prices can be up to 15 minutes late, and
missing candles are skipped (a stop or target inside a missing candle is not seen).
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font

import ftse_backtest as bt
import ftse_trade_scanner as sc
from ftse_sessions import calendar, session_open_close

STAKE = 250.0
SPREAD_BET_COST, SHARE_BUY_COST = 0.25, 1.50
SHORTLIST, MAX_TRADES = 5, 3
WATCH_END, FIRST_ENTRY, LAST_ENTRY = "08:30", "08:30", "15:30"
CHECK_EVERY = pd.Timedelta(minutes=15)
CHECK_DELAY = pd.Timedelta(minutes=2)
STEP = pd.Timedelta(minutes=5)
EMA_SPAN, EMA_SLOPE_BARS, DIP_BARS, DIP_TOL = 20, 6, 4, 0.001
STOP_LOOKBACK, STOP_BUFFER = 6, 0.0005
MIN_TARGET_PCT, MIN_TARGET_R = 0.01, 2.0
MOVER_ATR, GAP_PCT = 1.5, 0.01
LEVEL_LOOKBACK, LEVEL_TOUCHES, LEVEL_BAND_ATR, LEVEL_NEAR_ATR = 120, 3, 0.5, 1.0


# ----------------------------------------------------------------------
# Step 1: the morning shortlist (daily candles before the day only)
# ----------------------------------------------------------------------

@dataclass
class Context:
    ticker: str
    trend: int          # +1 up, -1 down
    level: bool
    mover: bool
    move_size: float
    score: int


def daily_context(daily: pd.DataFrame, day: pd.Timestamp, ticker: str) -> Context | None:
    prior = daily[daily.index < day.normalize()]
    if len(prior) < 60:
        return None
    c, h, l, o = (prior[k].to_numpy() for k in ("close", "high", "low", "open"))
    sma20 = pd.Series(c).rolling(20).mean().to_numpy()
    sma50 = pd.Series(c).rolling(50).mean().to_numpy()
    prev = np.r_[np.nan, c[:-1]]
    tr = np.nanmax(np.vstack([h - l, np.abs(h - prev), np.abs(l - prev)]), axis=0)
    atr = float(np.nanmedian(tr[-14:]))
    if not (atr > 0 and np.isfinite(sma50[-1]) and np.isfinite(sma20[-6])):
        return None
    if c[-1] > sma20[-1] > sma50[-1] and sma20[-1] > sma20[-6]:
        trend = 1
    elif c[-1] < sma20[-1] < sma50[-1] and sma20[-1] < sma20[-6]:
        trend = -1
    else:
        return None
    window_h, window_l = h[-LEVEL_LOOKBACK:], l[-LEVEL_LOOKBACK:]
    swings = []
    for i in range(3, len(window_h) - 3):
        if window_h[i] == window_h[i - 3:i + 4].max():
            swings.append(window_h[i])
        if window_l[i] == window_l[i - 3:i + 4].min():
            swings.append(window_l[i])
    swings = np.array(swings)
    level = bool(len(swings) and any(abs(p - c[-1]) <= LEVEL_NEAR_ATR * atr
                                     and (np.abs(swings - p) <= LEVEL_BAND_ATR * atr).sum() >= LEVEL_TOUCHES
                                     for p in swings))
    move = abs(c[-1] - c[-2]) / atr if len(c) > 1 else 0.0
    gap = abs(o[-1] / c[-2] - 1) if len(c) > 1 and c[-2] > 0 else 0.0
    mover = move >= MOVER_ATR or gap >= GAP_PCT
    return Context(ticker, trend, level, mover, move, 2 + int(level) + int(mover))


def shortlist(contexts: list[Context], size: int = SHORTLIST) -> list[Context]:
    return sorted(contexts, key=lambda x: (x.score, x.move_size), reverse=True)[:size]


# ----------------------------------------------------------------------
# Steps 2-5: watching, triggers and trades (5-minute candles of one day)
# ----------------------------------------------------------------------

def watch_ok(day_bars: pd.DataFrame, trend: int, opening: pd.Timestamp) -> bool:
    end = pd.Timestamp(f"{opening.date()} {WATCH_END}", tz=sc.UK_TZ)
    seen = day_bars[day_bars.index + STEP <= end]
    if len(seen) < 3:
        return False
    move = float(seen["close"].iloc[-1]) - float(seen["open"].iloc[0])
    return move > 0 if trend > 0 else move < 0


def trigger(bars: pd.DataFrame, trend: int) -> bool:
    """Pullback to the 5-minute 20-period average, then a turn in the trend's direction, on the last candle."""
    if len(bars) < EMA_SPAN + EMA_SLOPE_BARS + 2:
        return False
    close = bars["close"].astype(float)
    ema = close.ewm(span=EMA_SPAN, adjust=False).mean()
    last, prev = bars.iloc[-1], bars.iloc[-2]
    recent = bars.iloc[-DIP_BARS:]
    if trend > 0:
        return (ema.iloc[-1] > ema.iloc[-1 - EMA_SLOPE_BARS]
                and (recent["low"] <= ema.iloc[-DIP_BARS:] * (1 + DIP_TOL)).any()
                and last["close"] > last["open"] and last["close"] > prev["high"])
    return (ema.iloc[-1] < ema.iloc[-1 - EMA_SLOPE_BARS]
            and (recent["high"] >= ema.iloc[-DIP_BARS:] * (1 - DIP_TOL)).any()
            and last["close"] < last["open"] and last["close"] < prev["low"])


def run_trade(day_bars: pd.DataFrame, trend: int, fill_at: pd.Timestamp, recent: pd.DataFrame) -> dict | None:
    after = day_bars[day_bars.index >= fill_at]
    if after.empty:
        return None
    fill = float(after["open"].iloc[0])
    if trend > 0:
        stop = float(recent["low"].iloc[-STOP_LOOKBACK:].min()) * (1 - STOP_BUFFER)
        risk = fill - stop
    else:
        stop = float(recent["high"].iloc[-STOP_LOOKBACK:].max()) * (1 + STOP_BUFFER)
        risk = stop - fill
    if not (risk > 0 and math.isfinite(fill)):
        return None
    distance = max(MIN_TARGET_PCT * fill, MIN_TARGET_R * risk)
    target = fill + distance if trend > 0 else fill - distance
    outcome, price, when = "closed at end of day", float(after["close"].iloc[-1]), after.index[-1]
    for stamp, bar in after.iterrows():
        o, h, l = float(bar["open"]), float(bar["high"]), float(bar["low"])
        hit_stop = l <= stop if trend > 0 else h >= stop
        hit_target = h >= target if trend > 0 else l <= target
        if hit_stop:
            outcome, price, when = "stop", (min(o, stop) if trend > 0 else max(o, stop)), stamp
            break
        if hit_target:
            outcome, price, when = "target", (max(o, target) if trend > 0 else min(o, target)), stamp
            break
    gross = (price - fill) / fill * STAKE * trend
    return {"fill_time": after.index[0], "fill": round(fill, 4), "stop": round(stop, 4), "target": round(target, 4),
            "risk_pct": round(risk / fill * 100, 3), "target_pct": round(distance / fill * 100, 3),
            "outcome": outcome, "exit": round(price, 4), "exit_time": when, "gross": round(gross, 2),
            "net_spread_bet": round(gross - SPREAD_BET_COST, 2),
            "net_shares": round(gross - SHARE_BUY_COST, 2) if trend > 0 else None}


def trade_day(day: pd.Timestamp, picks: list[Context], intraday: dict[str, pd.DataFrame], mode: str) -> list[dict]:
    """One day's trades. mode: 'rules' (watch + trigger), 'no_trigger' (enter at the first check)."""
    opening, closing = session_open_close(day)
    bars_of = {}
    for ctx in picks:
        data = intraday.get(ctx.ticker)
        if data is None:
            continue
        day_bars = data[(data.index >= opening) & (data.index < closing)]
        if len(day_bars) < 20 or not watch_ok(day_bars, ctx.trend, opening):
            continue
        bars_of[ctx.ticker] = (ctx, data[data.index < closing], day_bars)
    trades, taken = [], set()
    check = pd.Timestamp(f"{day.date()} {FIRST_ENTRY}", tz=sc.UK_TZ) + CHECK_EVERY
    last = pd.Timestamp(f"{day.date()} {LAST_ENTRY}", tz=sc.UK_TZ)
    while check <= last and len(trades) < MAX_TRADES:
        fill_at = check + CHECK_DELAY
        fill_at = opening + math.ceil((fill_at - opening) / STEP) * STEP
        for ticker, (ctx, history, day_bars) in sorted(bars_of.items(), key=lambda kv: -kv[1][0].score):
            if ticker in taken:
                continue
            known = history[history.index + STEP <= check].iloc[-200:]
            fired = False
            if mode == "no_trigger":
                fired = True
            else:
                for k in range(3):                              # the three new candles since the last check
                    if trigger(known.iloc[:len(known) - k] if k else known, ctx.trend):
                        fired = True
                        break
            if not fired:
                continue
            result = run_trade(day_bars, ctx.trend, fill_at, known.iloc[-STOP_LOOKBACK:])
            if result:
                taken.add(ticker)
                trades.append({"day": str(day.date()), "check": check.strftime("%H:%M"), "ticker": ticker,
                               "direction": "Buy" if ctx.trend > 0 else "Sell", "score": ctx.score,
                               "level": ctx.level, "mover": ctx.mover, **result})
                break                                           # at most one new trade per check
        if mode == "no_trigger":
            break                                               # the yardstick enters once, at the first check
        check += CHECK_EVERY
    return trades


# ----------------------------------------------------------------------

def summary(name: str, trades: pd.DataFrame, split: str) -> dict:
    if trades.empty:
        return {"Strategy": name, "Trades": 0}
    days = trades["day"].nunique()
    buys = trades[trades["direction"] == "Buy"]
    return {"Strategy": name, "Trades": len(trades), "Days with a trade": days,
            "Target hit": (trades["outcome"] == "target").mean(), "Stop hit": (trades["outcome"] == "stop").mean(),
            "Made money (spread bet)": (trades["net_spread_bet"] > 0).mean(),
            "Before costs per £250 (£)": trades["gross"].mean(),
            "After costs: spread bet (£)": trades["net_spread_bet"].mean(),
            "Total after costs: spread bet (£)": trades["net_spread_bet"].sum(),
            "Buys: spread bet (£)": buys["net_spread_bet"].mean() if len(buys) else None,
            "Sells: spread bet (£)": trades.loc[trades["direction"] == "Sell", "net_spread_bet"].mean(),
            "Buys via IG shares instead (£)": buys["net_shares"].mean() if len(buys) else None,
            "First half (£)": trades.loc[trades["day"] < split, "net_spread_bet"].mean(),
            "Second half (£)": trades.loc[trades["day"] >= split, "net_spread_bet"].mean()}


def run(args) -> int:
    universe = [c for c in sc.load_universe(Path(args.universe)) if c.kind != "trust" and c.index == "FTSE 100"]
    if args.limit:
        universe = universe[:args.limit]
    names = {c.yahoo_ticker: c.company for c in universe}
    tickers = [c.yahoo_ticker for c in universe]
    print(f"Downloading daily and 5-minute candles for {len(tickers)} FTSE 100 shares...")
    daily_raw = sc.fetch_many(tickers, "1d", "2y")
    daily = {}
    for t, f in daily_raw.items():
        f = f[["open", "high", "low", "close"]].astype(float).dropna()
        idx = pd.DatetimeIndex(f.index)
        f.index = (idx.tz_localize(None) if idx.tz is not None else idx).normalize()
        daily[t] = f[~f.index.duplicated(keep="last")]
    intraday = {}
    for t, f in sc.fetch_many(tickers, "5m", "60d").items():
        f = sc.drop_untraded_bars(sc.completed_candles(f, "5m"))
        intraday[t] = f[["open", "high", "low", "close"]].astype(float).dropna()
    if not intraday or not daily:
        print("No data downloaded (is Yahoo reachable?).")
        return 1
    days = sorted({s.normalize().tz_localize(None) for f in intraday.values() for s in f.index})
    days = [d for d in days if calendar().is_session(d)][2:]   # the first days only warm up the averages
    results = {"rules": [], "all_shares": [], "no_trigger": []}
    shortlist_log = []
    for number, day in enumerate(days, 1):
        contexts = [ctx for t in tickers if t in daily and t in intraday
                    for ctx in [daily_context(daily[t], day, t)] if ctx]
        picks = shortlist(contexts)
        shortlist_log += [{"day": str(day.date()), "ticker": p.ticker, "company": names.get(p.ticker, p.ticker),
                           "trend": "Up" if p.trend > 0 else "Down", "near level": p.level, "big mover": p.mover,
                           "score": p.score} for p in picks]
        results["rules"] += trade_day(day, picks, intraday, "rules")
        results["no_trigger"] += trade_day(day, picks, intraday, "no_trigger")
        results["all_shares"] += trade_day(day, contexts, intraday, "rules")
        if number % 10 == 0:
            print(f"  ...{number}/{len(days)} days")
    frames = {k: pd.DataFrame(v) for k, v in results.items()}
    split = str(days[len(days) // 2].date())
    rules = frames["rules"]
    rows = [summary("Phil's process: shortlist + watch + trigger", rules, split)]
    if not rules.empty:
        for label, mask in (("  shortlist reason: trend only", ~rules["level"] & ~rules["mover"]),
                            ("  shortlist reason: trend + key level", rules["level"] & ~rules["mover"]),
                            ("  shortlist reason: trend + big mover", ~rules["level"] & rules["mover"]),
                            ("  shortlist reason: all three", rules["level"] & rules["mover"])):
            rows.append(summary(label, rules[mask], split))
    rows += [summary("Yardstick: same rules on every trending FTSE 100 share (no shortlist)", frames["all_shares"], split),
             summary("Yardstick: shortlist, entered at 08:45 with no trigger", frames["no_trigger"], split)]
    table = pd.DataFrame(rows)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.cell(1, 1, "Daily-context day trader (Phil's process): backtest").font = Font(name="Arial", bold=True, size=13)
    bt.note(ws, 2, f"Generated {datetime.now(sc.UK_TZ):%Y-%m-%d %H:%M} UK time. {len(intraday)} FTSE 100 shares, "
                   f"{days[0].date()} to {days[-1].date()} ({len(days)} trading days) of 5-minute candles. Shortlist of 5 at "
                   "08:00 from daily candles (trend needed; +1 near a key daily level; +1 yesterday's big mover). Watch "
                   "08:00-08:30: must move in the trend's direction. Entries 08:30-15:30, checked every 15 minutes: pullback to "
                   "the 5-minute 20-period average and a turn in the trend's direction. Stop below the last 30 minutes' low "
                   "(above the high for sells); target the larger of 1% and 2x the stop distance; closed at the end of the day.")
    bt.note(ws, 3, "£250 a trade. Spread bet costs 25p (0.10% price gap), no overnight charge as trades close the same day. "
                   "'Buys via IG shares' shows the same buys with £1.50 costs (stamp duty). At most 3 trades a day, one per "
                   "share, one new trade per check. First / second half = the days split in two.")
    bt.note(ws, 4, "Caution: only 60 days of 5-minute data exist on Yahoo; prices can be up to 15 minutes late; missing "
                   "candles are skipped. Treat this as a first look, then judge on live paper trades.")
    money = [c for c in table.columns if "(£)" in c]
    bt.write_table(ws, 6, table.round(4), money, ["Target hit", "Stop hit", "Made money (spread bet)"])
    for col, width in zip("ABCDEFGHIJKLMNO", [58, 8, 9, 8, 8, 10, 11, 11, 12, 11, 11, 12, 10, 10]):
        ws.column_dimensions[col].width = width

    for title, frame in (("Trades", rules), ("Shortlists", pd.DataFrame(shortlist_log))):
        sheet = wb.create_sheet(title)
        if frame.empty:
            sheet.cell(1, 1, "None.")
            continue
        shown = frame.copy()
        if title == "Trades":
            shown["company"] = shown["ticker"].map(names)
            shown = shown[["day", "check", "company", "ticker", "direction", "score", "level", "mover", "fill", "stop",
                           "target", "target_pct", "outcome", "exit", "gross", "net_spread_bet", "net_shares"]]
            shown.columns = ["Day", "Check", "Company", "Ticker", "Direction", "Score", "Near level", "Big mover", "Entry",
                             "Stop", "Target", "Target move (%)", "Result", "Exit", "Before costs (£)",
                             "After costs: spread bet (£)", "Buys via IG shares (£)"]
        bt.write_table(sheet, 1, shown, [c for c in shown.columns if "(£)" in c])
        sheet.freeze_panes = "A2"
    saved = sc.save_workbook(wb, Path(args.out))
    pd.set_option("display.width", 300)
    print(table[["Strategy", "Trades", "Target hit", "Before costs per £250 (£)", "After costs: spread bet (£)",
                 "First half (£)", "Second half (£)"]].round(2).to_string(index=False))
    print(f"Workbook saved: {saved}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest Phil's daily-context day trader on 5-minute candles")
    parser.add_argument("--universe", default=str(sc.UNIVERSE_PATH))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", default="results/ftse_daytrader.xlsx")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
