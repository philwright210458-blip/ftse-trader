import unittest
from unittest import mock

import numpy as np
import pandas as pd

import ftse_structure_chance_backtest as sk


def frame(closes, dividends=None, start="2020-01-01"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start, periods=len(closes))
    f = pd.DataFrame({"open": closes, "high": closes * 1.001, "low": closes * 0.999, "close": closes}, index=idx)
    f["dividend"] = 0.0 if dividends is None else dividends
    return f


def arrays(f):
    return [f[k].to_numpy() for k in ("open", "high", "low", "close", "dividend")]


class SimulateTests(unittest.TestCase):
    def test_target_is_twice_the_stop(self):
        f = frame([100.0] * 5 + [105.0] * 70)
        o, h, l, c, d = arrays(f)
        t = sk.simulate(o, h, l, c, d, 3, 0.02, 60, False)        # buy 100 at day 4's open; target 104
        self.assertEqual(t["outcome"], "target")
        self.assertAlmostEqual(t["exit"], 105.0)                   # gap past the target fills at the open
        self.assertAlmostEqual(t["gross"], 5 / 100 * 250)

    def test_stop_and_time_limit(self):
        o, h, l, c, d = arrays(frame([100.0] * 5 + [97.0] * 70))
        self.assertEqual(sk.simulate(o, h, l, c, d, 3, 0.02, 60, False)["outcome"], "stop")
        o, h, l, c, d = arrays(frame([100.0] * 80))
        t = sk.simulate(o, h, l, c, d, 3, 0.02, 60, False)
        self.assertEqual((t["outcome"], t["k"]), ("60 days", 63))
        o, h, l, c, d = arrays(frame([100.0] * 30))
        self.assertIsNone(sk.simulate(o, h, l, c, d, 3, 0.02, 60, False))   # would still be open at the end

    def test_dividends_only_after_the_entry_day(self):
        div = np.zeros(80)
        div[4] = 3.0                                               # ex-date on the entry day itself: not entitled
        div[10] = 2.0                                              # held over this one
        o, h, l, c, d = arrays(frame([100.0] * 80, div))
        self.assertAlmostEqual(sk.simulate(o, h, l, c, d, 3, 0.02, 60, True)["dividend"], 2.0)
        self.assertAlmostEqual(sk.simulate(o, h, l, c, d, 3, 0.02, 60, False)["dividend"], 0.0)

    def test_chased(self):
        o = np.array([100.0, 103.0])
        c = np.array([100.0, 103.0])
        atr = np.array([2.0, 2.0])
        self.assertTrue(sk.chased(o, c, atr, 0, 1.0))              # opened 3 above, more than 1 x 2
        self.assertFalse(sk.chased(o, c, atr, 0, 2.0))
        self.assertFalse(sk.chased(o, c, atr, 0, None))


class ChanceTests(unittest.TestCase):
    def test_luck(self):
        randoms = np.array([[0.0, 1.0, 2.0, 3.0]])
        self.assertAlmostEqual(sk.luck(np.array([2.0]), randoms), 0.5)
        self.assertIsNone(sk.luck(np.array([]), randoms))

    def test_run_all_with_fake_signals(self):
        rng = np.random.default_rng(4)
        closes = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, 1500)))
        data = {"X.L": frame(closes, start="2015-01-01"), "Y.L": frame(closes[::-1].copy(), start="2015-01-01")}

        def fake_signals(f):
            c = f["close"].to_numpy()
            return [{"t": t, "stop": c[t] * 0.97} for t in range(50, len(f) - 100, 120)]

        with mock.patch.object(sk.st, "signals", fake_signals):
            table, trades, split = sk.run_all(data, draws=50)
        self.assertEqual(list(table["Variant"]), [v[0] for v in sk.VARIANTS.values()])
        self.assertTrue((table["Trades"] > 0).all())
        self.assertTrue(set(trades["variant"]) == {"A", "B", "C"})
        for col in ("Random as good: first half", "Random as good: second half"):
            self.assertTrue(table[col].between(0, 1).all())
        lines = sk.verdict_lines(table)
        self.assertEqual(len(lines), 3)
        self.assertTrue(split)


if __name__ == "__main__":
    unittest.main()
