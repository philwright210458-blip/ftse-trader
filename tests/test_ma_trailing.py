import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import openpyxl
import pandas as pd

import ftse_ma_trailing_backtest as tb
import ftse_ma_zone as mz
from tests.test_ma_zone import uptrend_with_dip


def frame_of(closes, lows=None):
    days = pd.bdate_range("2025-01-01", periods=len(closes))
    closes = np.asarray(closes, dtype=float)
    lows = closes - 0.3 if lows is None else np.asarray(lows, dtype=float)
    return pd.DataFrame({"open": closes, "high": closes + 0.3, "low": lows, "close": closes}, index=days)


class TrailingTests(unittest.TestCase):
    def signal(self, frame, stop):
        return pd.DataFrame({"stop": [stop], "slope": [0.1]}, index=[frame.index[0]])

    def test_holds_while_above_20_sma_and_sells_next_open_after_close_below(self):
        closes = list(np.linspace(100, 130, 40)) + [120.0, 119.0]
        f = frame_of(closes)
        ind = pd.DataFrame({"sma20": [90.0] * 40 + [125.0, 125.0]}, index=f.index)
        t = tb.trail(f, ind, self.signal(f, 95.0))[0]
        self.assertEqual(t["outcome"], "closed below 20 SMA")
        self.assertEqual(t["exit"], 119.0)                       # next day's open
        self.assertAlmostEqual(t["gross"], round((119.0 - f["open"].iloc[1]) / f["open"].iloc[1] * 250, 2))

    def test_protective_stop_still_applies(self):
        f = frame_of([100, 100, 99, 98], lows=[99.7, 99.7, 96.0, 97.7])
        ind = pd.DataFrame({"sma20": [90.0] * 4}, index=f.index)
        t = tb.trail(f, ind, self.signal(f, 97.0))[0]
        self.assertEqual((t["outcome"], t["exit"]), ("stop", 97.0))

    def test_costs_per_250(self):
        f = frame_of([100, 100, 101, 102, 103, 104])
        rec = tb.record(f, f.index[0], 0, 100.0, 98.0, 102.0, 5, "x", {"slope": 0.1})
        self.assertEqual(rec["gross"], 5.0)
        self.assertAlmostEqual(rec["net_shares"], 3.5)
        self.assertAlmostEqual(rec["net_no_stamp"], 4.75)
        nights = rec["nights"]
        self.assertAlmostEqual(rec["net_spread_bet"], round(5.0 - 0.25 - tb.FUNDING_PER_NIGHT * nights, 2))

    def test_run_writes_workbook(self):
        frame, _ = uptrend_with_dip(400, back=60)
        fall = frame.iloc[-1]["close"] - np.arange(1, 31) * 1.0          # the trend ends, so trades close
        tail = pd.DataFrame({"open": fall + 0.2, "high": fall + 0.4, "low": fall - 0.4, "close": fall},
                            index=pd.bdate_range(frame.index[-1] + pd.Timedelta(days=1), periods=30))
        frame = pd.concat([frame, tail])
        with tempfile.TemporaryDirectory() as folder:
            universe = Path(folder) / "u.csv"
            universe.write_text("company,epic,yahoo_ticker,index,type\nAlpha,A,A.L,FTSE 100,company\n")
            args = SimpleNamespace(universe=str(universe), index="FTSE 100", period="10y", limit=None, out=str(Path(folder) / "t.xlsx"))
            with mock.patch.object(tb.db, "download", return_value={"A.L": frame}):
                self.assertEqual(tb.run(args), 0)
            self.assertEqual(openpyxl.load_workbook(args.out).sheetnames, ["Summary", "Each year", "Trades (trailing)"])


if __name__ == "__main__":
    unittest.main()
