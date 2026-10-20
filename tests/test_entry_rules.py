import unittest

import pandas as pd

import ftse_backtest as bt
import ftse_trade_scanner as sc


def bars(last):
    index = pd.date_range("2026-09-01 08:00", periods=20, freq="15min", tz=sc.UK_TZ)
    rows = [(100.0, 100.5, 99.5, 100.0)] * 19 + [last]          # typical candle range 1.0
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)


def setup(kind, direction, company="Alpha", score=80.0):
    zone = sc.Zone(99.0, 99.2)
    return sc.TradeSetup(company, "A.L", "FTSE 100", kind, direction, zone, 100.0, 98.0, 106.0, "", quality_score=score)


class EntryRuleTests(unittest.TestCase):
    def test_reclaim_distance(self):
        self.assertEqual(sc.entry_rule_note(setup("Breakout", "Long"), bars((99.0, 99.8, 98.9, 99.6))), "")
        self.assertIn("Reclaim", sc.entry_rule_note(setup("Breakout", "Long"), bars((99.0, 99.4, 98.9, 99.3))))

    def test_rejection_candle(self):
        self.assertEqual(sc.entry_rule_note(setup("Range", "Long"), bars((99.6, 99.8, 98.9, 99.7))), "")
        self.assertIn("rejection", sc.entry_rule_note(setup("Range", "Long"), bars((99.7, 99.8, 99.0, 99.1))))
        self.assertIn("smaller", sc.entry_rule_note(setup("Range", "Long"), bars((99.3, 99.4, 99.1, 99.35))))

    def test_both_ways_excludes_every_setup_on_that_share(self):
        items = [setup("Range", "Long", score=90), setup("Range", "Short", score=50), setup("Range", "Long", "Beta")]
        sc.annotate_conflicts(items)
        sc.apply_entry_rules(items)
        self.assertEqual([s.conflict for s in items], [True, True, False])
        self.assertIn("both ways", items[0].conflict_note)

    def test_failing_rule_excluded_and_passing_setup_preferred(self):
        weak = setup("Range", "Long", score=70)
        strong_but_fails = setup("Breakout", "Long", score=90)
        strong_but_fails.filter_note = "Reclaim close less than 0.5 ATR back across the zone edge."
        items = [weak, strong_but_fails]
        sc.annotate_conflicts(items)
        sc.apply_entry_rules(items)
        chosen = sc.dedupe_setups(items)
        self.assertEqual(len(chosen), 1)
        self.assertIs(chosen[0], weak)

    def test_backtest_uses_the_live_numbers(self):
        self.assertEqual((bt.RECLAIM_DISPLACEMENT_ATR, bt.REJECTION_MIN_RANGE_ATR, bt.REJECTION_WICK_SHARE,
                          bt.REJECTION_CLOSE_LOCATION), (0.5, 0.75, 0.4, 0.6))


if __name__ == "__main__":
    unittest.main()
