import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import openpyxl
import pandas as pd

import ftse_daytrader as dt
import ftse_trade_scanner as sc
from ftse_sessions import calendar, session_open_close


def daily_uptrend(n=150, end="2026-09-30"):
    days = pd.bdate_range(end=end, periods=n)
    close = 100 + np.arange(n) * 0.3
    return pd.DataFrame({"open": close - 0.1, "high": close + 0.5, "low": close - 0.5, "close": close}, index=days)


def five_minute_day(day, path):
    opening, _ = session_open_close(pd.Timestamp(day))
    index = pd.date_range(opening, periods=len(path), freq="5min")
    rows = [(o, max(o, c) + 0.02, min(o, c) - 0.02, c) for o, c in path]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)


class DayTraderTests(unittest.TestCase):
    def test_daily_context_needs_trend_and_uses_only_earlier_days(self):
        d = daily_uptrend()
        ctx = dt.daily_context(d, pd.Timestamp("2026-10-01"), "A.L")
        self.assertEqual((ctx.trend, ctx.score >= 2), (1, True))
        flat = d.assign(close=100.0, open=100.0, high=100.5, low=99.5)
        self.assertIsNone(dt.daily_context(flat, pd.Timestamp("2026-10-01"), "A.L"))
        changed = d.copy()
        changed.loc["2026-10-01":] = 1.0                            # later prices can't change the morning's view
        self.assertEqual(dt.daily_context(changed, pd.Timestamp("2026-10-01"), "A.L"), ctx)

    def test_shortlist_takes_best_scores(self):
        ctxs = [dt.Context(f"T{i}", 1, i % 2 == 0, i % 3 == 0, float(i), 2 + (i % 2 == 0) + (i % 3 == 0)) for i in range(10)]
        picks = dt.shortlist(ctxs, 3)
        self.assertEqual([p.score for p in picks], [4, 4, 3])

    def test_watch_requires_move_in_trend_direction(self):
        up = five_minute_day("2026-10-01", [(100 + 0.1 * k, 100.1 + 0.1 * k) for k in range(12)])
        opening, _ = session_open_close(pd.Timestamp("2026-10-01"))
        self.assertTrue(dt.watch_ok(up, 1, opening))
        self.assertFalse(dt.watch_ok(up, -1, opening))

    def test_trigger_pullback_then_turn(self):
        rising = [(100 + 0.05 * k, 100.05 + 0.05 * k) for k in range(40)]
        top = rising[-1][1]
        dip = [(top, top - 0.3), (top - 0.3, top - 0.45), (top - 0.45, top - 0.4)]
        turn = [(top - 0.4, top + 0.05)]
        bars = five_minute_day("2026-10-01", rising + dip + turn)
        self.assertTrue(dt.trigger(bars, 1))
        self.assertFalse(dt.trigger(bars, -1))
        self.assertFalse(dt.trigger(bars.iloc[:-1], 1))

    def test_trade_target_is_at_least_one_percent_and_twice_the_stop(self):
        bars = five_minute_day("2026-10-01", [(100, 100)] * 10 + [(100, 101.5)] + [(101.5, 101.5)] * 5)
        recent = bars.iloc[:6]
        t = dt.run_trade(bars, 1, bars.index[6], recent)
        self.assertGreaterEqual(t["target_pct"], 1.0)
        self.assertAlmostEqual(t["target"] - t["fill"], max(0.01 * t["fill"], 2 * (t["fill"] - t["stop"])), places=3)
        self.assertEqual(t["outcome"], "target")
        self.assertAlmostEqual(t["net_spread_bet"], t["gross"] - 0.25)

    def test_sell_stop_and_end_of_day(self):
        bars = five_minute_day("2026-10-01", [(100, 100)] * 10 + [(100, 100.3)] + [(100.3, 99.9)] * 3)
        t = dt.run_trade(bars, -1, bars.index[6], bars.iloc[:6])
        self.assertEqual(t["outcome"], "stop")
        flat = five_minute_day("2026-10-01", [(100, 100)] * 14)
        self.assertEqual(dt.run_trade(flat, -1, flat.index[6], flat.iloc[:6])["outcome"], "closed at end of day")


if __name__ == "__main__":
    unittest.main()


class DayTraderRunTests(unittest.TestCase):
    def test_run_builds_workbook(self):
        sessions = calendar().sessions_in_range("2026-09-01", "2026-09-30")
        daily = daily_uptrend(200, end="2026-09-30")
        frames = []
        rng = np.random.default_rng(3)
        for day in sessions:
            opening, closing = session_open_close(day)
            index = pd.date_range(opening, closing, freq="5min", inclusive="left")
            base = daily.loc[day.normalize(), "open"] if day.normalize() in daily.index else 100.0
            steps = np.cumsum(rng.normal(0.01, 0.08, len(index)))
            close = base + steps
            open_ = np.r_[base, close[:-1]]
            frames.append(pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + 0.03,
                                        "low": np.minimum(open_, close) - 0.03, "close": close, "volume": 1000}, index=index))
        five = pd.concat(frames)
        daily_raw = daily.assign(volume=1000)

        def fake(tickers, interval, period, **_):
            return {"A.L": daily_raw.rename(columns=str.lower) if interval == "1d" else five}

        with tempfile.TemporaryDirectory() as folder:
            universe = Path(folder) / "u.csv"
            universe.write_text("company,epic,yahoo_ticker,index,type\nAlpha,A,A.L,FTSE 100,company\n")
            args = SimpleNamespace(universe=str(universe), limit=None, out=str(Path(folder) / "d.xlsx"))
            with mock.patch.object(dt.sc, "fetch_many", side_effect=fake):
                self.assertEqual(dt.run(args), 0)
            self.assertEqual(openpyxl.load_workbook(args.out).sheetnames, ["Summary", "Trades", "Shortlists"])
