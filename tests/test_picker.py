"""Morning picker views used by the scanner (26 Oct 2026)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import ftse_live_mock as live
import ftse_picker as picker
import ftse_trade_scanner as sc


def setup(ticker, direction="Long", score=60.0, conflict=False):
    zone = SimpleNamespace(low=99.0, high=100.0, touch_count=4)
    return sc.TradeSetup(company=f"{ticker} plc", ticker=ticker, index_label="FTSE 100", setup_type="Range",
                         direction=direction, zone=zone, entry=100.0, stop=99.0, target=102.0, note="",
                         quality_score=score, conflict=conflict)


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def write(self, rows, day="2026-10-26"):
        path = picker.picker_path(self.dir, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(path, index=False)

    def test_missing_or_broken_file_means_no_views(self):
        self.assertIsNone(picker.load_views(self.dir, "2026-10-26"))
        path = picker.picker_path(self.dir, "2026-10-26")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x00\x01not,a\ncsv\"")
        self.assertIn(picker.load_views(self.dir, "2026-10-26"), (None, {}))

    def test_load_ignores_bad_rows(self):
        self.write([{"day": "2026-10-26", "ticker": "brby.l", "company": "Burberry", "view": "sell", "reason": "Warning"},
                    {"day": "2026-10-26", "ticker": "AAA.L", "company": "A", "view": "Maybe", "reason": ""}])
        self.assertEqual(picker.load_views(self.dir, "2026-10-26"), {"BRBY.L": ("Sell", "Warning")})

    def test_against_and_avoid_skipped_agreeing_marked_neutral_unchanged(self):
        views = {"A.L": ("Buy", "Upgrade"), "B.L": ("Sell", ""), "C.L": ("Avoid", "Results today")}
        ideas = [setup("A.L"), setup("A.L", "Short"), setup("B.L"), setup("C.L", "Short"), setup("D.L")]
        self.assertEqual(picker.apply_views(ideas, views), 3)
        self.assertEqual([s.conflict for s in ideas], [False, True, True, True, False])
        self.assertEqual([s.picker_agrees for s in ideas], [True, False, False, False, False])
        self.assertEqual(ideas[3].conflict_note, "Morning picker says Avoid (Results today); skipped.")
        self.assertIn("Morning picker agrees (Buy)", ideas[0].note)

    def test_live_trader_takes_agreeing_ideas_first(self):
        info = sc.RunInfo(pd.Timestamp("2026-10-26 10:00", tz=sc.UK_TZ), "10:15", "2026-10-26",
                          "2026-10-26 10:17:00", "watch", 100, 0)
        ideas = [setup("A.L", score=90), setup("B.L", score=60)]
        picker.apply_views(ideas, {"B.L": ("Buy", "")})
        rows = live.record_live_mock(self.dir, info, ideas, 10, max_open=1, open_now=0)
        self.assertEqual([r["ticker"] for r in rows], ["B.L"])

    def test_annotate_labels_without_changing_trades(self):
        views = {"A.L": ("Buy", "Upgrade"), "B.L": ("Sell", ""), "C.L": ("Avoid", "Results today")}
        ideas = [setup("A.L"), setup("B.L"), setup("C.L", "Short"), setup("D.L")]
        counts = picker.annotate_views(ideas, views)
        self.assertEqual(counts, {"Agrees": 1, "Opposes": 1, "Avoid": 1, "No view": 1})
        self.assertEqual([s.picker_alignment for s in ideas], ["Agrees", "Opposes", "Avoid", "No view"])
        self.assertFalse(any(s.conflict for s in ideas))
        self.assertFalse(any(getattr(s, "picker_agrees", False) for s in ideas))   # no priority in the live trader
        self.assertIn("Morning picker: Avoid (avoid): Results today.", ideas[2].note)


if __name__ == "__main__":
    unittest.main()
