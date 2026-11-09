import unittest

import numpy as np
import pandas as pd

import ftse_trend_filters_test as tf

UK = "Europe/London"


def hourly():
    idx = list(pd.date_range("2026-11-05 08:00", periods=9, freq="60min", tz=UK)) + \
          list(pd.date_range("2026-11-06 08:00", periods=9, freq="60min", tz=UK))
    close = [100] * 9 + [101, 102, 103, 104, 105, 106, 107, 108, 109]
    return pd.DataFrame({"open": [c - 0.5 for c in close], "close": close}, index=pd.DatetimeIndex(idx))


class TrendFilterTests(unittest.TestCase):
    def test_move_so_far_uses_only_what_the_scanner_could_see(self):
        h = hourly()
        self.assertAlmostEqual(tf.move_so_far(h, "2026-11-06", "08:32"), 0.5)     # no hour finished: the open
        self.assertAlmostEqual(tf.move_so_far(h, "2026-11-06", "09:14"), 0.5)     # 09:00 close not seen until 09:15
        self.assertAlmostEqual(tf.move_so_far(h, "2026-11-06", "09:15"), 1.0)
        self.assertIsNone(tf.move_so_far(h, "2026-11-05", "10:00"))              # no previous day

    def test_masks(self):
        trades = pd.DataFrame({
            "ticker": ["AZN.L", "X.L"], "sign": [1, -1], "ema_state": ["Up", "Up"], "daily_trend": ["Up", "Down"],
            "trend_4h": ["Up", "Down"], "signal_volume_ratio": [2.0, 1.0],
            "market_move_pct": [0.3, 0.3], "sector_move_pct": [-0.1, -0.2]})
        m = tf.masks(trades)
        self.assertEqual(list(m["B"]), [True, False])
        self.assertEqual(list(m["C"]), [False, True])
        self.assertEqual(list(m["D"]), [True, False])
        self.assertEqual(list(m["E"]), [True, True])
        self.assertEqual(list(m["F"]), [True, False])
        self.assertEqual(list(m["G"]), [True, False])
        self.assertEqual(list(m["H"]), [True, False])

    def test_evaluate(self):
        rng = np.random.default_rng(2)
        days = [f"2026-09-{d:02d}" for d in range(1, 21)]
        trades = pd.DataFrame({"source": "Backtest", "day": [days[i % 20] for i in range(80)],
                               "ticker": "X.L", "sign": 1, "ema_state": "Up", "daily_trend": "Up", "trend_4h": "Up",
                               "signal_volume_ratio": 1.0, "market_move_pct": 0.1, "sector_move_pct": 0.1,
                               "net_gbp": rng.normal(0, 0.5, 80)})
        table = tf.evaluate(trades, "Backtest")
        self.assertEqual(len(table), 8)
        self.assertEqual(len(tf.verdict_lines(table)), 8)


if __name__ == "__main__":
    unittest.main()
