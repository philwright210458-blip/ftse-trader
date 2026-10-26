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
        def reclaim(last):
            frame = bars(last)
            frame.iloc[-2] = [99.2, 99.3, 98.7, 98.8]          # the candle before closed below the zone
            return frame
        self.assertEqual(sc.entry_rule_note(setup("Breakout", "Long"), reclaim((99.0, 99.8, 98.9, 99.6))), "")
        self.assertIn("Reclaim", sc.entry_rule_note(setup("Breakout", "Long"), reclaim((99.0, 99.4, 98.9, 99.3))))

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


class TriggerRuleTests(unittest.TestCase):
    """22 Oct 2026: fresh reclaims only, and only trades whose target is worth it after costs."""

    def test_worth_trading_after_costs(self):
        # Hammerson-like buy: 0.42% stop, 1.25% target. With spread-bet costs (10p, from 26 Oct 2026):
        # £1.15 gain v 52p loss: kept (with stamp duty it was 65p v £1.02 and skipped).
        buy = sc.TradeSetup("Ham", "H.L", "FTSE 250", "Range", "Long", sc.Zone(346.9, 347.2), 348.0, 346.55, 352.35, "")
        self.assertEqual(sc.worth_trading_note(buy), "")
        # 0.8% stop, 1.2% target buy: £1.10 gain v 90p loss is under 1.5x: skipped
        weak = sc.TradeSetup("W", "W.L", "FTSE 100", "Range", "Long", sc.Zone(99, 100), 100.0, 99.2, 101.2, "")
        self.assertIn("Not worth it", sc.worth_trading_note(weak))
        # NatWest-like sell: 0.99% stop, 2.97% target -> £2.87 v £1.09: kept
        sell = sc.TradeSetup("Nat", "N.L", "FTSE 100", "Breakout", "Short", sc.Zone(640, 641), 641.0, 647.35, 621.94, "")
        self.assertEqual(sc.worth_trading_note(sell), "")

    def test_reclaim_must_be_on_latest_candle(self):
        index = pd.date_range("2026-09-01 08:00", periods=20, freq="15min", tz=sc.UK_TZ)
        rows = [(100.0, 100.5, 99.5, 100.0)] * 17 + [(99.2, 99.3, 98.6, 98.8), (98.8, 99.8, 98.7, 99.7), (99.7, 99.9, 99.5, 99.8)]
        df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)
        s = setup("Breakout", "Long")
        self.assertFalse(sc.reclaim_is_fresh(s, df))           # back above at the 19th candle, now two candles on
        self.assertTrue(sc.reclaim_is_fresh(s, df.iloc[:-1]))  # judged on the candle that reclaimed
