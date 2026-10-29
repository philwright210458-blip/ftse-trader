import unittest

import numpy as np
import pandas as pd

import ftse_feasibility_backtest as fb

UK = "Europe/London"


def bars(rows, start="2026-10-29 08:45"):
    index = pd.date_range(start, periods=len(rows), freq="15min", tz=UK)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)


class ExitTests(unittest.TestCase):
    def test_stop_first_when_both_reached(self):
        b = bars([(100, 103, 97, 100)])
        self.assertEqual(fb.run_exit(b, "Long", 100, 98, 102), (98, "stop"))

    def test_gap_through_stop_fills_at_open(self):
        b = bars([(100, 100.5, 99.5, 100), (97, 97.5, 96, 97)])
        self.assertEqual(fb.run_exit(b, "Long", 100, 98, 104), (97, "stop"))

    def test_target_and_short_side(self):
        b = bars([(100, 100.5, 98.9, 99)])
        self.assertEqual(fb.run_exit(b, "Short", 100, 101, 99), (99, "target"))

    def test_breakeven_moves_the_stop(self):
        b = bars([(100, 102.1, 99.5, 102), (102, 102.5, 99.8, 100)])   # +2 = one stop distance, then back to 99.8
        self.assertEqual(fb.run_exit(b, "Long", 100, 98, 110, breakeven_after=2), (100, "stop"))
        self.assertEqual(fb.run_exit(b, "Long", 100, 98, 110), (100, "16:30"))

    def test_time_limit_and_direction_only(self):
        b = bars([(100, 100.5, 99.5, 100.2), (100.2, 100.6, 99.9, 100.4), (100.4, 101, 100, 100.9)])
        self.assertEqual(fb.run_exit(b, "Long", 100, 98, 110, max_bars=2), (100.4, "time limit"))
        self.assertEqual(fb.run_exit(b, "Long", 100, None, None), (100.9, "16:30"))

    def test_money(self):
        self.assertAlmostEqual(fb.net_gbp("Long", 100, 101), 0.90)
        self.assertAlmostEqual(fb.net_gbp("Short", 100, 101), -1.10)

    def test_mirrored_levels(self):
        self.assertEqual(fb.mirrored({"fill": 100, "stop": 98, "target": 104}), (102, 96))


class ChanceTests(unittest.TestCase):
    def test_a_real_edge_is_rarely_matched_by_chance(self):
        actual = np.full(200, 1.0)
        opposite = np.full(200, -1.0)
        result = fb.chance_test(actual, opposite, [[-1.0, 1.0]] * 200, runs=200)
        self.assertEqual(result["random_direction_as_good"], 0.0)
        self.assertAlmostEqual(result["random_direction_average_total"], 0.0, delta=40)
        self.assertLess(result["random_time_as_good"], 0.05)

    def test_no_edge_is_often_matched(self):
        rng = np.random.default_rng(1)
        actual = rng.normal(0, 1, 200)
        result = fb.chance_test(actual, -actual, [[a, -a] for a in actual], runs=500)
        self.assertGreater(result["random_direction_as_good"], 0.05)


class SelfSearchTests(unittest.TestCase):
    def table(self, second_half_helps: bool):
        rows = []
        days = [f"2026-10-{d:02d}" for d in range(1, 29)]
        for n, day in enumerate(days * 6):
            bad = n % 3 == 0
            late = day >= "2026-10-15"
            net = (-2.0 if bad and (second_half_helps or not late) else 1.0 if bad else 0.5)
            rows.append({"day": day, "net_gbp": net, "stop_pretested": "Yes" if bad else "No",
                         "setup": "Range", "direction": "Long", "score": float(n % 7)})
        return pd.DataFrame(rows)

    def test_finds_and_confirms_a_real_pattern(self):
        found = fb.self_search(self.table(True), "2026-10-15")
        best = found[found["Candidate"]].iloc[0]
        self.assertEqual(best["Feature"], "stop_pretested")
        self.assertIn((best["Group"], best["Action"]), [("Yes", "Skip"), ("No", "Only")])   # the same rule
        self.assertEqual(best["Check on second half"], "Confirmed on unseen half")

    def test_rejects_a_pattern_that_fades(self):
        found = fb.self_search(self.table(False), "2026-10-15")
        best = found[found["Candidate"]].iloc[0]
        self.assertEqual(best["Feature"], "stop_pretested")
        self.assertEqual(best["Check on second half"], "Not confirmed: did not help on the second half")


class RuleTests(unittest.TestCase):
    def test_breakout_intact_buffer(self):
        row = pd.Series({"setup": "Breakout", "direction": "Long", "zone_low": 3784.0, "zone_high": 3788.03,
                         "atr": 8.0, "stop_pct": 0.73})
        result = {"fill": 3790.0, "fill_risk_pct": 0.42}
        self.assertTrue(fb.breakout_intact(0.15)(row, result))             # Admiral, 29 Oct: kept
        self.assertFalse(fb.breakout_intact(0.25)(row, result))            # cancelled by 0.03 of a point
        self.assertTrue(fb.breakout_intact(0.5)(row.replace({"Breakout": "Range"}), result))   # range trades unaffected

    def test_fill_check_still_applies(self):
        row = pd.Series({"setup": "Range", "direction": "Long", "zone_low": 1.0, "zone_high": 2.0, "atr": 1.0, "stop_pct": 1.0})
        self.assertFalse(fb.breakout_intact(0.0)(row, {"fill": 2.5, "fill_risk_pct": 0.4}))


if __name__ == "__main__":
    unittest.main()
