import unittest
from unittest import mock

import pandas as pd

import ftse_scan_limit_backtest as sl


def ideas(rows):
    return pd.DataFrame([{"day": "2026-11-02", "run_time": rt, "ticker": tk, "direction": d, "score": sc,
                          "data_time": "2026-11-02T08:15:00+00:00", "stop_pct": 0.5} for rt, tk, d, sc in rows])


def always_open(row, intraday, now, check):
    return {"status": "pending", "outcome": "", "exit_time": "", "fill": 100.0, "fill_risk_pct": 0.5}


class ScanLimitTests(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(sl.ec, "simulate_checked", side_effect=always_open)
        self.patch.start()
        first = "2026-11-02 08:32:00"
        second = "2026-11-02 08:47:00"
        self.ideas = ideas([(first, "A.L", "Long", 90), (first, "B.L", "Long", 80), (first, "C.L", "Long", 70),
                            (first, "D.L", "Short", 60), (second, "E.L", "Long", 50), (second, "F.L", "Short", 40)])

    def tearDown(self):
        self.patch.stop()

    def taken(self, **limits):
        return list(sl.live_trader(self.ideas, {}, pd.Timestamp("2026-11-02 17:00", tz="Europe/London"), **limits)["ticker"])

    def test_current_rules_fill_five_slots(self):
        self.assertEqual(self.taken(), ["A.L", "B.L", "C.L", "D.L", "E.L"])

    def test_per_scan_limit_spreads_trades(self):
        self.assertEqual(self.taken(per_scan=2), ["A.L", "B.L", "E.L", "F.L"])
        self.assertEqual(self.taken(per_scan=1), ["A.L", "E.L"])

    def test_same_direction_per_scan(self):
        self.assertEqual(self.taken(same_direction_per_scan=2), ["A.L", "B.L", "D.L", "E.L", "F.L"])

    def test_same_direction_open(self):
        self.assertEqual(self.taken(same_direction_open=3), ["A.L", "B.L", "C.L", "D.L", "F.L"])


if __name__ == "__main__":
    unittest.main()
