import unittest

import pandas as pd

import ftse_let_it_run_backtest as lr

UK = "Europe/London"
DAY = "2026-11-13"


def bars(rows, start="09:00"):
    idx = pd.date_range(f"{DAY} {start}", periods=len(rows), freq="15min", tz=UK)
    return pd.DataFrame([dict(zip(("open", "high", "low", "close"), r)) for r in rows], index=idx)


# buy at 100, stop 99 (1.0 away), scanner target 102, so half target 101
class LetItRunTests(unittest.TestCase):
    def test_g_locks_half_target_then_closes_on_retreat_after_delay(self):
        rows = [(100, 101.2, 99.9, 101.1),      # 09:00 reaches half target: armed from 09:15
                (101.1, 101.8, 101.1, 101.7),   # 09:15 runs on
                (101.7, 102.5, 101.6, 102.4),   # 09:30 runs past the old full target
                (102.4, 102.5, 101.9, 101.95),  # 09:45 closes below 09:30's low 101.6? no (101.95 > 101.6)
                (101.95, 102.0, 101.4, 101.5),  # 10:00 closes 101.5 < 101.9 (09:45 low): retreat seen
                (101.5, 101.6, 101.2, 101.3),   # 10:15 still waiting (known at 10:30)
                (101.25, 101.4, 101.1, 101.2)]  # 10:30 open: closed here
        when, price, why = lr.run_exit(bars(rows), "Long", 100, 99, 102, "G")
        self.assertEqual(why, "retreat")
        self.assertEqual(when.strftime("%H:%M"), "10:30")
        self.assertAlmostEqual(price, 101.25)

    def test_g_stop_at_half_target_protects_the_gain(self):
        rows = [(100, 101.2, 99.9, 101.1), (101.1, 101.15, 100.5, 100.6)]
        when, price, why = lr.run_exit(bars(rows), "Long", 100, 99, 102, "G")
        self.assertEqual((why, price), ("stop", 101.0))

    def test_g_before_half_target_uses_the_original_stop(self):
        rows = [(100, 100.5, 99.5, 100.1), (100.1, 100.2, 98.8, 98.9)]
        self.assertEqual(lr.run_exit(bars(rows), "Long", 100, 99, 102, "G")[1:], (99.0, "stop"))

    def test_h1_and_h2_trail_from_best(self):
        rows = [(100, 101.2, 99.9, 101.1), (101.1, 103.0, 101.05, 102.9), (102.9, 102.9, 101.5, 101.6)]
        # H1: after best 103.0 the stop is max(101, 103 - 1) = 102
        self.assertEqual(lr.run_exit(bars(rows), "Long", 100, 99, 102, "H1")[1:], (102.0, "stop"))
        # H2: max(101, 103 - 0.5) = 102.5
        self.assertEqual(lr.run_exit(bars(rows), "Long", 100, 99, 102, "H2")[1:], (102.5, "stop"))

    def test_i_trails_from_the_fill(self):
        rows = [(100, 100.6, 99.8, 100.5), (100.5, 100.5, 99.5, 99.6)]
        # best 100.6 after the first candle, so the stop is 99.6
        self.assertEqual(lr.run_exit(bars(rows), "Long", 100, 99, 102, "I")[1:], (99.6, "stop"))

    def test_sell_mirror(self):
        # sell at 100, stop 101, target 98, half 99
        rows = [(100, 100.1, 98.8, 98.9), (98.9, 99.3, 98.9, 99.2), (99.2, 99.4, 99.0, 99.3)]
        self.assertEqual(lr.run_exit(bars(rows), "Short", 100, 101, 98, "G")[1:], (99.0, "stop"))

    def test_runs_to_1630(self):
        rows = [(100, 101.2, 99.9, 101.1), (101.1, 101.6, 101.1, 101.5), (101.5, 102.0, 101.4, 101.9)]
        when, price, why = lr.run_exit(bars(rows), "Long", 100, 99, 102, "H1")
        self.assertEqual((why, price), ("16:30", 101.9))


if __name__ == "__main__":
    unittest.main()
