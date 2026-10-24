"""Target cap (24 Oct 2026): smaller of 2% and the share's typical daily move."""

import unittest

import pandas as pd

import ftse_trade_scanner as sc


def history(day_moves, today_rows=2):
    """15-minute candles: one session per entry in day_moves (high-low as % of a 100 open), then today."""
    frames = []
    days = pd.bdate_range("2026-10-01", periods=len(day_moves) + 1)
    for move, day in zip(list(day_moves) + [50.0], days):
        index = pd.date_range(f"{day.date()} 08:00", periods=today_rows if day == days[-1] else 4, freq="15min", tz=sc.UK_TZ)
        frame = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1000}, index=index)
        frame.iloc[1, frame.columns.get_loc("high")] = 100 + move   # today's big move must be ignored
        frames.append(frame)
    return pd.concat(frames)


def setup(direction="Short", entry=100.0, target=90.0, stop=101.0):
    zone = sc.Zone(low=99.0, high=100.5)
    return sc.TradeSetup("Alpha", "A.L", "FTSE 100", "Breakout", direction, zone, entry, stop, target, "Note.")


class TargetCap(unittest.TestCase):
    def test_typical_move_uses_previous_days_only(self):
        self.assertAlmostEqual(sc.typical_daily_move_pct(history([1.0] * 5 + [3.0] * 5)), 2.0)
        self.assertIsNone(sc.typical_daily_move_pct(history([1.0] * 9)))

    def test_capped_at_typical_move_when_below_two_percent(self):
        s = setup()
        self.assertTrue(sc.cap_target(s, history([1.2] * 10)))
        self.assertAlmostEqual(s.target, 98.8)
        self.assertIn("Target capped at 1.20% (typical daily move 1.2%; was 90.00)", s.note)

    def test_capped_at_two_percent_for_lively_shares(self):
        s = setup(direction="Long", target=110.0, stop=99.0)
        self.assertTrue(sc.cap_target(s, history([3.5] * 10)))
        self.assertAlmostEqual(s.target, 102.0)
        self.assertIn("2% maximum", s.note)

    def test_near_targets_unchanged_and_cap_can_be_turned_off(self):
        s = setup(target=99.0)
        self.assertFalse(sc.cap_target(s, history([1.5] * 10)))
        self.assertEqual(s.target, 99.0)
        far = setup()
        self.assertFalse(sc.cap_target(far, history([1.5] * 10), max_pct=0))
        self.assertEqual(far.target, 90.0)

    def test_two_percent_applies_without_enough_history(self):
        s = setup()
        self.assertTrue(sc.cap_target(s, history([1.0] * 3)))
        self.assertAlmostEqual(s.target, 98.0)

    def test_capped_target_feeds_the_costs_check(self):
        s = setup(stop=101.0)                      # 1% stop, 10% target: passes before the cap
        self.assertEqual(sc.worth_trading_note(s), "")
        sc.cap_target(s, history([1.2] * 10))      # 1.2% target against a 1% stop now fails
        self.assertIn("Not worth it after costs", sc.worth_trading_note(s))


if __name__ == "__main__":
    unittest.main()
