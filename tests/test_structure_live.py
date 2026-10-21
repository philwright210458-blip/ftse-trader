import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import openpyxl
import pandas as pd

import ftse_structure as st
import ftse_structure_live as live
from tests.structure_fixture import textbook

FRAME = textbook()
SIGNAL = st.signals(FRAME)[0]
SIGNAL_DAY = FRAME.index[SIGNAL["t"]]
DATA = {"A.L": FRAME}
NAMES, INDEXES = {"A.L": "Alpha plc"}, {"A.L": "FTSE 100"}


def empty():
    return pd.DataFrame(columns=live.COLUMNS)


@mock.patch.object(live, "FIRST_SIGNAL_DAY", str(FRAME.index[0].date()))
class StructureLiveTests(unittest.TestCase):
    def test_nothing_before_the_signal(self):
        trades, _ = live.step(empty(), DATA, NAMES, INDEXES, None, SIGNAL_DAY - pd.Timedelta(days=1), "now")
        self.assertTrue(trades.empty)

    def test_signal_recorded_then_filled_next_morning(self):
        trades, checked = live.step(empty(), DATA, NAMES, INDEXES, None, SIGNAL_DAY, "now")
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades.iloc[0]["signal_day"], str(SIGNAL_DAY.date()))
        self.assertTrue(pd.isna(trades.iloc[0]["fill"]))
        next_day = FRAME.index[SIGNAL["t"] + 1]
        trades, _ = live.step(trades, DATA, NAMES, INDEXES, checked, next_day, "later")
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(float(trades.iloc[0]["fill"]), float(FRAME["open"].iloc[SIGNAL["t"] + 1]), places=3)

    def test_catch_up_resolves_both_targets(self):
        trades, _ = live.step(empty(), DATA, NAMES, INDEXES, None, FRAME.index[-1], "now")
        row = trades.iloc[0]
        self.assertEqual((row["result_measured"], row["result_2x"]), ("Target hit", "Target hit"))
        self.assertGreater(float(row["net_measured"]), float(row["net_2x"]))

    def test_rerun_does_not_duplicate(self):
        trades, checked = live.step(empty(), DATA, NAMES, INDEXES, None, FRAME.index[-1], "now")
        again, _ = live.step(trades, DATA, NAMES, INDEXES, checked, FRAME.index[-1], "now")
        self.assertEqual(len(again), 1)

    def test_signal_uses_only_prices_up_to_its_day(self):
        changed = FRAME.copy()
        changed.loc[changed.index > SIGNAL_DAY, ["open", "high", "low", "close"]] *= 0.5
        trades, _ = live.step(empty(), {"A.L": changed}, NAMES, INDEXES, None, SIGNAL_DAY, "now")
        self.assertEqual(trades.iloc[0]["signal_day"], str(SIGNAL_DAY.date()))

    def test_run_writes_files(self):
        with tempfile.TemporaryDirectory() as folder:
            universe = Path(folder) / "u.csv"
            universe.write_text("company,epic,yahoo_ticker,index,type\nAlpha plc,A,A.L,FTSE 100,company\n")
            args = SimpleNamespace(universe=str(universe), data_dir=str(Path(folder) / "live"), out=str(Path(folder) / "l.xlsx"),
                                   period="2y")
            now = pd.Timestamp(FRAME.index[-1]).tz_localize("Europe/London") + pd.Timedelta(hours=17, minutes=5)
            with mock.patch.object(live.db, "download", return_value=DATA):
                self.assertEqual(live.run(args, now=now), 0)
            self.assertEqual(openpyxl.load_workbook(args.out).sheetnames, ["Summary", "Trades"])
            self.assertEqual(len(pd.read_csv(Path(args.data_dir) / "trades.csv")), 1)


if __name__ == "__main__":
    unittest.main()
