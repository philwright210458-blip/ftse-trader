import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import openpyxl
import pandas as pd

import ftse_backtest as bt
import ftse_trade_scanner as sc
from ftse_sessions import calendar, session_open_close


def synthetic(seed: int, sessions):
    """15-minute candles oscillating inside a range, with occasional breaks, across real London sessions."""
    rng = np.random.default_rng(seed)
    rows, price = [], 100.0
    for day in sessions:
        opening, closing = session_open_close(day)
        for stamp in pd.date_range(opening, closing, freq="15min", inclusive="left"):
            price = 100 + 1.2 * math.sin(len(rows) / 7) + rng.normal(0, 0.15)
            high, low = price + abs(rng.normal(0, 0.2)), price - abs(rng.normal(0, 0.2))
            rows.append((stamp, price, high, low, price + rng.normal(0, 0.05), 1000))
    frame = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"]).set_index("datetime")
    frame["high"] = frame[["open", "high", "close"]].max(axis=1)
    frame["low"] = frame[["open", "low", "close"]].min(axis=1)
    return frame


class BacktestTests(unittest.TestCase):
    def test_widen_moves_stop_out_and_keeps_three_to_one(self):
        frame = pd.DataFrame({"entry": [100.0, 100.0], "stop": [99.5, 100.5], "target": [101.5, 98.5],
                              "direction": ["Long", "Short"]})
        wide = bt.widen(frame, 2.0)
        self.assertEqual(list(wide["stop"]), [98.0, 102.0])
        self.assertEqual(list(wide["target"]), [106.0, 94.0])
        unchanged = bt.widen(pd.DataFrame({"entry": [100.0], "stop": [97.0], "target": [109.0], "direction": ["Long"]}), 2.0)
        self.assertEqual(float(unchanged["stop"][0]), 97.0)

    def test_two_way_keys_count_conflict_rows(self):
        setups = pd.DataFrame({"run_time": ["r1", "r1", "r1", "r2"], "ticker": ["A", "A", "B", "A"],
                               "direction": ["Long", "Short", "Long", "Long"], "conflict": [False, True, False, False]})
        self.assertEqual(bt.two_way_keys(setups), {("r1", "A")})

    def _bars(self, last):
        index = pd.date_range("2026-09-01 08:00", periods=20, freq="15min", tz=sc.UK_TZ)
        rows = [(100.0, 100.5, 99.5, 100.0)] * 19 + [last]            # typical candle range 1.0
        return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)

    def _row(self, setup, direction, data):
        return pd.DataFrame({"ticker": ["A.L"], "data_time": [data.index[-1].isoformat()], "setup": [setup],
                             "direction": [direction], "zone_low": [99.0], "zone_high": [99.2]})

    def test_reclaim_needs_half_atr_beyond_edge(self):
        far = self._bars((99.0, 99.8, 98.9, 99.6))      # close 0.6 above zone low 99.0, ATR 1.0
        near = self._bars((99.0, 99.4, 98.9, 99.3))     # close only 0.3 above
        self.assertTrue(bt.confirmation_flags(self._row("Breakout", "Long", far), {"A.L": far})["reclaim_confirmed"].iloc[0])
        self.assertFalse(bt.confirmation_flags(self._row("Breakout", "Long", near), {"A.L": near})["reclaim_confirmed"].iloc[0])

    def test_range_needs_rejection_candle(self):
        pin = self._bars((99.6, 99.8, 98.9, 99.7))      # long lower wick into the zone, closes near the high
        weak = self._bars((99.7, 99.8, 99.0, 99.1))     # touches the zone but closes near the low
        small = self._bars((99.3, 99.4, 99.1, 99.35))   # right shape but only 0.3 ATR tall
        flags = [bt.confirmation_flags(self._row("Range", "Long", d), {"A.L": d})["range_confirmed"].iloc[0]
                 for d in (pin, weak, small)]
        self.assertEqual(flags, [True, False, False])

    def test_sma_uses_only_earlier_days(self):
        index = pd.date_range("2026-01-01", periods=60, freq="D")
        closes = pd.Series(range(60), index=index, dtype=float)
        self.assertEqual(bt.sma_before(closes, pd.Timestamp("2026-02-20"), 50), float(np.mean(range(0, 50))))
        self.assertIsNone(bt.sma_before(closes, pd.Timestamp("2026-01-10"), 50))

    def test_live_picks_three_a_day_one_per_share(self):
        frame = pd.DataFrame({"day": ["d"] * 5, "run_time": ["d 10:03", "d 10:03", "d 10:18", "d 10:18", "d 10:33"],
                              "ticker": ["A", "B", "A", "C", "D"], "score": [50, 60, 99, 40, 90]})
        chosen = bt.live_picks(frame)
        self.assertEqual(list(chosen["ticker"]), ["B", "A", "C"])

    def test_end_to_end_on_synthetic_history(self):
        sessions = calendar().sessions_in_range("2026-08-03", "2026-09-04")
        intraday = {"AAA.L": synthetic(1, sessions), "BBB.L": synthetic(2, sessions)}
        daily = {t: d["close"].groupby(d.index.date).last().rename(index=pd.Timestamp) for t, d in intraday.items()}
        universe = [sc.Constituent("Alpha plc", "AAA", "AAA.L", "FTSE 100", "company"),
                    sc.Constituent("Beta plc", "BBB", "BBB.L", "FTSE 250", "company")]
        out = Path(tempfile.mkdtemp()) / "bt.xlsx"
        args = SimpleNamespace(universe="x", interval="15m", period="60d", warmup_days=10, horizon=1, pick_time="10:30",
                               exclude_trusts=False, limit=None, delay=0, out=str(out))
        with mock.patch.object(bt.sc, "load_universe", return_value=universe), \
             mock.patch.object(bt, "download", return_value=(intraday, daily)):
            self.assertEqual(bt.run(args), 0)
        book = openpyxl.load_workbook(out)
        self.assertEqual(book.sheetnames, ["Summary", "Daily", "Trades (baseline)", "Trades (trend pullback)"])
        summary = pd.DataFrame(list(book["Summary"].iter_rows(min_row=7, values_only=True)))
        summary.columns = summary.iloc[0]
        summary = summary.iloc[1:]
        self.assertEqual(len(summary), 42)        # 13 variants x 3 scenarios + 3 trend-pullback rows
        trades = pd.DataFrame(list(book["Trades (baseline)"].iter_rows(values_only=True)))
        trades.columns = trades.iloc[0]
        done = trades.iloc[1:].dropna(subset=["After costs (£)"])
        self.assertGreater(len(done), 0)
        for _, row in done.iterrows():
            cost = 0.6 if row["Direction"] == "Long" else 0.1
            self.assertAlmostEqual(row["Costs (£)"], cost, places=2)
            self.assertAlmostEqual(row["Before costs (£)"] - row["Costs (£)"], row["After costs (£)"], delta=0.011)
            # entries are always after the scan that produced them
            self.assertGreaterEqual(pd.Timestamp(row["Exit time"]).tz_convert(sc.UK_TZ).tz_localize(None),
                                    pd.Timestamp(row["Signal time"]))



class FilterTests(unittest.TestCase):
    def test_slope_compares_with_ten_days_earlier_without_peeking(self):
        index = pd.date_range("2026-01-01", periods=80, freq="D")
        rising = pd.Series(range(80), index=index, dtype=float)
        now, then = bt.sma_slope_before(rising, pd.Timestamp("2026-03-21"))   # 79 closes before this day
        self.assertEqual(now, float(np.mean(range(29, 79))))
        self.assertEqual(then, float(np.mean(range(19, 69))))
        self.assertGreater(now, then)
        self.assertEqual(bt.sma_slope_before(rising, pd.Timestamp("2026-02-25")), (None, None))

    def test_ema_uses_only_candles_up_to_the_signal(self):
        index = pd.date_range("2026-08-03 08:00", periods=40, freq="15min", tz=sc.UK_TZ)
        data = pd.DataFrame({"close": list(range(30)) + [0] * 10}, index=index, dtype=float)
        price, now, then = bt.ema_momentum(data, index[29])
        self.assertEqual(price, 29.0)
        self.assertGreater(now, then)          # rising, and the later crash is not visible yet



class PullbackTests(unittest.TestCase):
    def setUp(self):
        import ftse_pullback as pb
        self.pb = pb
        self.company = sc.Constituent("Alpha plc", "AAA", "AAA.L", "FTSE 100", "company")
        days = pd.date_range("2026-01-01", periods=80, freq="B")
        self.rising_daily = pd.Series(np.linspace(80, 100, 80), index=days)

    def intraday(self, prices):
        index = pd.date_range("2026-04-21 08:00", periods=len(prices), freq="15min", tz=sc.UK_TZ)
        closes = pd.Series(prices, index=index, dtype=float)
        opens = closes.shift(1).fillna(closes.iloc[0])
        return pd.DataFrame({"open": opens, "close": closes, "high": np.maximum(opens, closes) + 0.05,
                             "low": np.minimum(opens, closes) - 0.05, "volume": 1000}, index=index)

    def test_daily_trend_needs_rising_average_and_price_above(self):
        self.assertEqual(self.pb.daily_trend(self.rising_daily, "2026-04-21"), 1)
        self.assertEqual(self.pb.daily_trend(-self.rising_daily, "2026-04-21"), -1)
        self.assertEqual(self.pb.daily_trend(self.rising_daily, "2026-02-01"), 0)       # not enough history yet

    def test_pullback_to_20_ema_then_turn_up_gives_one_buy_with_stop_below_dip(self):
        climb = list(np.linspace(100, 103, 24))
        dip = [102.4, 101.95, 102.1]         # eases back to the rising 20-period average
        turn = [102.6, 102.9, 103.2]         # closes back above the 9-period average
        rows = self.pb.signals(self.company, self.intraday(climb + dip + turn), self.rising_daily)
        self.assertEqual(len(rows), 1)                       # one trade per share per day
        row = rows[0]
        self.assertEqual(row["direction"], "Long")
        self.assertLess(row["stop"], min(dip))
        self.assertAlmostEqual(row["target"] - row["entry"], 2 * (row["entry"] - row["stop"]), places=3)
        self.assertGreater(pd.Timestamp(row["run_time"]), pd.Timestamp(row["data_time"]).tz_localize(None))

    def test_no_buy_when_daily_trend_is_down(self):
        climb = list(np.linspace(100, 103, 28)) + [102.6, 102.4, 102.5, 102.9, 103.1]
        self.assertEqual(self.pb.signals(self.company, self.intraday(climb), -self.rising_daily), [])


if __name__ == "__main__":
    unittest.main()
