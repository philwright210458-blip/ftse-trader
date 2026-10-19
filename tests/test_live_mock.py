import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import ftse_live_mock as live
import ftse_trade_scanner as sc


def setup(ticker, score, direction="Long", conflict=False, stale=False):
    zone = SimpleNamespace(low=99.0, high=100.0, touch_count=4)
    return sc.TradeSetup(company=f"{ticker} plc", ticker=ticker, index_label="FTSE 100", setup_type="Breakout",
                         direction=direction, zone=zone, entry=100.0, stop=99.0, target=103.0, note="",
                         quality_score=score, conflict=conflict, stale=stale)


def info(run_time, day="2026-10-19", mode="watch"):
    data_time = pd.Timestamp(f"{day} 10:00", tz=sc.UK_TZ)
    return sc.RunInfo(data_time, "10:15", day, run_time, mode, 15, 0)


class LiveMockTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_best_scores_first_up_to_daily_limit_one_per_share(self):
        rows = live.record_live_mock(self.dir, info("2026-10-19 10:18:00"),
                                     [setup("AAA.L", 60), setup("BBB.L", 80), setup("AAA.L", 90, "Short"),
                                      setup("CCC.L", 70, conflict=True), setup("DDD.L", 95, stale=True)], 2)
        self.assertEqual([r["ticker"] for r in rows], ["AAA.L", "BBB.L"])
        self.assertEqual(rows[0]["direction"], "Short")       # highest-scoring idea for AAA
        later = live.record_live_mock(self.dir, info("2026-10-19 10:33:00"), [setup("EEE.L", 99)], 2)
        self.assertEqual(later, [])                            # daily limit already reached
        self.assertEqual(len(live.read_live(self.dir / live.LIVE_FILENAME)), 2)

    def test_numbering_continues_through_the_day(self):
        live.record_live_mock(self.dir, info("2026-10-19 10:03:00", mode="full"), [setup("AAA.L", 60)], 3)
        rows = live.record_live_mock(self.dir, info("2026-10-19 11:01:00"), [setup("AAA.L", 70), setup("BBB.L", 50)], 3)
        self.assertEqual([(r["ticker"], r["trade_no"]) for r in rows], [("BBB.L", 2)])

    def test_no_entries_after_cut_off_or_on_a_later_day(self):
        self.assertEqual(live.record_live_mock(self.dir, info("2026-10-19 15:46:00"), [setup("AAA.L", 60)]), [])
        self.assertEqual(live.record_live_mock(self.dir, info("2026-10-20 20:28:00"), [setup("AAA.L", 60)]), [])

    def test_key_matches_outcome_tracker_key(self):
        run = info("2026-10-19 10:18:00")
        row = live.record_live_mock(self.dir, run, [setup("AAA.L", 60)])[0]
        expected = run.data_time.isoformat() + "|AAA.L|Long|2026-10-19 10:18:00|watch"
        self.assertEqual(row["key"], expected)


if __name__ == "__main__":
    unittest.main()
