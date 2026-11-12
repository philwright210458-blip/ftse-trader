import unittest

import numpy as np
import pandas as pd

import ftse_exit_lock_backtest as xl

UK = "Europe/London"
DAY = "2026-11-12"


def bars(rows, start="09:00"):
    idx = pd.date_range(f"{DAY} {start}", periods=len(rows), freq="15min", tz=UK)
    return pd.DataFrame([dict(zip(("open", "high", "low", "close"), r)) for r in rows], index=idx)


def item(rows, side="Long", fill=100.0, stop=99.0, target=102.0, start="09:00"):
    b = bars(rows, start)
    it = {"day": DAY, "ticker": "X.L", "side": side, "fill": fill, "start": b.index[0], "bars": b}
    for mode in "ABC":
        when, price, why = xl.path_exit(b, side, fill, stop, target, mode)
        it[mode] = {"end": when, "price": price, "why": why, "net": xl.net(side, fill, price)}
    return it


class PathTests(unittest.TestCase):
    def test_half_target_and_trailing(self):
        # buy 100, stop 99, target 102: up to 101.6, then back down to 99
        rows = [(100, 100.4, 99.8, 100.3), (100.3, 101.6, 100.2, 101.5), (101.5, 101.5, 100.5, 100.6),
                (100.6, 100.6, 98.9, 99.0)]
        b = bars(rows)
        self.assertEqual(xl.path_exit(b, "Long", 100, 99, 102, "A")[2], "stop")
        when, price, why = xl.path_exit(b, "Long", 100, 99, 102, "B")
        self.assertEqual((why, price), ("target", 101.0))
        when, price, why = xl.path_exit(b, "Long", 100, 99, 102, "C")
        self.assertEqual(why, "stop")
        self.assertAlmostEqual(price, 100.8)                       # trail keeps half of the 1.6 best gain

    def test_closes_at_1630(self):
        b = bars([(100, 100.5, 99.5, 100.2)] * 3)
        self.assertEqual(xl.path_exit(b, "Short", 100, 101, 98, "A")[2], "16:30")


class LockTests(unittest.TestCase):
    def test_level_lock_closes_open_trades_after_the_delay(self):
        # trade 1 rises to +1.5 at the 09:15 candle close, then falls; lock at +£1 acts at 09:45 open
        up = [(100, 100.5, 99.9, 100.4), (100.4, 101.6, 100.3, 101.5), (101.5, 101.6, 101.0, 101.2),
              (101.2, 101.3, 99.5, 99.6), (99.6, 99.7, 98.8, 98.9)]
        a = item(up)
        late = item([(50, 50.2, 49.9, 50.1)] * 2, fill=50.0, stop=49.5, target=51.0, start="10:30")
        total, taken, when = xl.day_with_lock([a, late], "level", 1.0)
        self.assertEqual(when, "09:45")
        self.assertEqual(taken, 1)                                  # the 10:30 trade is never taken
        self.assertAlmostEqual(total, xl.net("Long", 100, 101.2))   # closed at the 09:45 candle's open
        self.assertLess(a["A"]["net"], 0)                           # without the lock it lost

    def test_giveback_lock(self):
        rows = [(100, 101.8, 99.9, 101.7), (101.7, 101.7, 100.6, 100.7), (100.7, 100.8, 100.5, 100.6),
                (100.6, 100.6, 98.9, 99.0)]
        total, taken, when = xl.day_with_lock([item(rows, target=103.0)], "giveback", 1.5)
        self.assertEqual(when, "09:45")                             # fell to +0.6 (under half of +1.5) at 09:15
        self.assertAlmostEqual(total, xl.net("Long", 100, 100.6))

    def test_no_lock(self):
        it = item([(100, 100.2, 99.9, 100.1)] * 3)
        total, taken, when = xl.day_with_lock([it], "level", 1.0)
        self.assertIsNone(when)
        self.assertAlmostEqual(total, it["A"]["net"])


class SummaryTests(unittest.TestCase):
    def test_summarise(self):
        rng = np.random.default_rng(3)
        days = pd.DataFrame({"day": [f"2026-09-{d:02d}" for d in range(1, 21)], "trades": 8})
        for key in "ABCDEF":
            days[key] = rng.normal(0, 2, 20)
        for key in "DEF":
            days[f"{key}_trades"] = 7
            days[f"{key}_locked_at"] = [None, "10:00"] * 10
        table = xl.summarise(days, 160)
        self.assertEqual(len(table), 6)
        self.assertEqual(len(xl.verdict_lines(table)), 6)
        self.assertEqual(table.loc[3, "Days locked"], 10)


if __name__ == "__main__":
    unittest.main()
