import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import openpyxl
import pandas as pd

import ftse_ma_daily_backtest as db
import ftse_ma_zone as mz


def uptrend_with_dip(n=80, back=10):
    """Steady rise, then one day dipping into the 9 EMA / 20 SMA zone and closing up."""
    days = pd.bdate_range("2024-01-01", periods=n)
    close = 100 + np.arange(n) * 0.5
    frame = pd.DataFrame({"open": close - 0.2, "high": close + 0.4, "low": close - 0.4, "close": close}, index=days)
    i = n - back
    ind = mz.indicators(frame)
    low = (ind["ema9"].iloc[i - 1] + ind["sma20"].iloc[i - 1]) / 2       # between the lines
    frame.iloc[i] = [low + 0.3, close[i - 1] + 0.2, low, close[i - 1] + 0.1]
    return frame, days[i]


class MaZoneTests(unittest.TestCase):
    def test_dip_into_zone_in_uptrend_is_a_signal(self):
        frame, day = uptrend_with_dip()
        mask = mz.signal_mask(mz.indicators(frame))
        self.assertTrue(bool(mask.loc[day]))
        self.assertEqual(int(mask.sum()), 1)          # ordinary rising days don't dip to the 9 EMA

    def test_dip_far_below_20_sma_is_not_a_signal(self):
        frame, day = uptrend_with_dip()
        frame.loc[day, "low"] = frame.loc[day, "low"] - 20
        self.assertFalse(bool(mz.signal_mask(mz.indicators(frame)).loc[day]))

    def test_downtrend_gives_no_signals(self):
        frame, _ = uptrend_with_dip()
        flipped = frame.iloc[::-1].copy()
        flipped.index = frame.index
        self.assertEqual(int(mz.signal_mask(mz.indicators(flipped)).sum()), 0)

    def test_levels_stop_below_dip_and_target_twice(self):
        frame, day = uptrend_with_dip()
        ind = mz.indicators(frame)
        row = mz.levels(ind, mz.signal_mask(ind)).loc[day]
        self.assertLess(row["stop"], min(row["low"], row["sma20"]))
        self.assertAlmostEqual(row["target"] - row["close"], 2 * (row["close"] - row["stop"]))

    def test_slope_groups_are_equal_thirds(self):
        groups = mz.slope_groups(pd.Series(np.arange(9, dtype=float)))
        self.assertEqual(list(groups.value_counts().sort_index()), [3, 3, 3])


class DailySimulationTests(unittest.TestCase):
    def frame(self, rows):
        return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.bdate_range("2025-01-01", periods=len(rows)))

    def signals(self, frame, stop):
        return pd.DataFrame({"stop": [stop], "slope": [0.1]}, index=[frame.index[0]])

    def test_target_reset_from_fill_and_hit(self):
        f = self.frame([(100, 101, 99, 100), (100, 100.5, 99.5, 100), (100, 104.5, 100, 104)])
        t = db.simulate(f, self.signals(f, 98.0))[0]
        self.assertEqual((t["outcome"], t["target"], t["exit"]), ("target", 104.0, 104.0))
        self.assertAlmostEqual(t["net_gbp"], 4.0 - 0.6)

    def test_stop_first_and_gap(self):
        f = self.frame([(100, 101, 99, 100), (100, 100.5, 99.5, 100), (97, 105, 96, 104)])
        t = db.simulate(f, self.signals(f, 98.0))[0]
        self.assertEqual((t["outcome"], t["exit"]), ("stop", 97.0))

    def test_timeout_after_hold_days(self):
        rows = [(100, 100.5, 99.5, 100)] * 25
        f = self.frame(rows)
        t = db.simulate(f, self.signals(f, 98.0), hold=20)[0]
        self.assertEqual((t["outcome"], t["days"]), ("timeout", 20))

    def test_run_writes_workbook(self):
        frame, _ = uptrend_with_dip(400, back=60)
        with tempfile.TemporaryDirectory() as folder:
            universe = Path(folder) / "u.csv"
            universe.write_text("company,epic,yahoo_ticker,index,type\nAlpha,A,A.L,FTSE 100,company\n")
            args = SimpleNamespace(universe=str(universe), index="FTSE 100", period="10y", limit=None, out=str(Path(folder) / "d.xlsx"))
            with mock.patch.object(db, "download", return_value={"A.L": frame}):
                self.assertEqual(db.run(args), 0)
            book = openpyxl.load_workbook(args.out)
            self.assertEqual(book.sheetnames, ["Summary", "Each year", "Trades (MA zone)"])


if __name__ == "__main__":
    unittest.main()
