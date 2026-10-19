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
        self.assertEqual(book.sheetnames, ["Summary", "Daily", "Trades (baseline)"])
        summary = pd.DataFrame(list(book["Summary"].iter_rows(min_row=6, values_only=True)))
        summary.columns = summary.iloc[0]
        summary = summary.iloc[1:]
        self.assertEqual(len(summary), 12)        # 4 variants x 3 scenarios
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


if __name__ == "__main__":
    unittest.main()
