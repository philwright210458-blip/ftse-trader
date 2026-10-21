import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import openpyxl
import pandas as pd

import ftse_structure as st
import ftse_structure_backtest as sb
from tests.structure_fixture import candles, textbook


class StructureSignalTests(unittest.TestCase):
    def test_textbook_impulse_pullback_breakout(self):
        frame = textbook()
        found = st.signals(frame)
        self.assertEqual(len(found), 1)
        s = found[0]
        self.assertTrue(st.FIB_LOW <= s["depth"] <= st.FIB_HIGH)
        self.assertAlmostEqual(s["target"], s["P"] + s["H"] - s["L"])
        self.assertLess(s["stop"], s["P"])

    def test_uses_only_past_prices(self):
        frame = textbook()
        s = st.signals(frame)[0]
        cut = st.signals(frame.iloc[: s["t"] + 1])
        self.assertEqual([x["t"] for x in cut], [s["t"]])

    def test_too_deep_pullback_cancels(self):
        frame = textbook()
        s = st.signals(frame)[0]
        deep = frame.copy()
        deep.iloc[s["t"] - 1, deep.columns.get_loc("low")] = s["L"] + 0.5      # pullback nearly to the impulse start
        self.assertEqual([x for x in st.signals(deep) if x["t"] == s["t"]], [])

    def test_trade_measured_move_and_fixed_comparison(self):
        measured, fixed = sb.run_share(textbook())
        self.assertEqual(measured[0]["outcome"], "target")
        self.assertGreater(measured[0]["target"], fixed[0]["target"])
        self.assertAlmostEqual(measured[0]["net_shares"], measured[0]["gross"] - 1.50)

    def test_stop_counted_first_and_gap(self):
        frame = candles([(100, 100)] * 3 + [(100, 100), (96, 99)])
        t = sb.trade(frame, 2, 97.0, lambda fill: 104.0)
        self.assertEqual((t["outcome"], t["exit"]), ("stop", 96.0))

    def test_drift_yardstick(self):
        frame = candles([(100 + k, 100 + k) for k in range(30)])
        self.assertAlmostEqual(sb.drift(frame, 1), pd.Series([(101 + k) / (101 + k) - 1 for k in range(28)]).mean() * 250 + 0.0, 1)

    def test_run_writes_workbook(self):
        with tempfile.TemporaryDirectory() as folder:
            universe = Path(folder) / "u.csv"
            universe.write_text("company,epic,yahoo_ticker,index,type\nAlpha,A,A.L,FTSE 100,company\n")
            args = SimpleNamespace(universe=str(universe), index="FTSE 100", period="10y", limit=None, out=str(Path(folder) / "s.xlsx"))
            with mock.patch.object(sb.db, "download", return_value={"A.L": textbook()}):
                self.assertEqual(sb.run(args), 0)
            self.assertEqual(openpyxl.load_workbook(args.out).sheetnames, ["Summary", "Each year", "Trades"])


if __name__ == "__main__":
    unittest.main()
