"""Compact outcomes workbook (28 Oct 2026): one totals row, trades from row 5, daily balance, notes on their own sheet."""

import importlib.util
import re
from pathlib import Path
import unittest

import pandas as pd
import ftse_outcomes as out

spec = importlib.util.spec_from_file_location("report_fixture", Path(__file__).with_name("report_fixture.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

MONEY = '"£"#,##0.00;[Red]-"£"#,##0.00;"£"0.00'


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.args, self.pool, self.picks, self.results = module.fixture()
        self.book = out.build_report(self.args, self.pool, self.pool, self.picks, self.results, {}, "SYNTHETIC FIXTURE")
        self.live = self.book["Live Mock Trades"]

    def trade_rows(self, ws):
        return range(out.TRADES_HEADER_ROW + 1, ws.max_row + 1)

    def test_only_two_sheets_and_no_fixed_time_trader(self):
        self.assertEqual(self.book.sheetnames, ["Live Mock Trades", "Daily", "Notes"])
        self.assertEqual(out.TRADES_HEADER_ROW, 4)                            # trades start near the top
        self.assertEqual(self.live.freeze_panes, "A5")
        self.assertIn("SYNTHETIC FIXTURE", self.book["Notes"].cell(2, 2).value)
        self.assertLessEqual(sum(self.live.column_dimensions[c].width for c in "ABCDEFGHIJKLMNOP"), 170)
        text = " ".join(str(c.value or "") for ws in self.book.worksheets for row in ws for c in row)
        for gone in ("Top 2 per day", "at or after 10:30", "Strategy Comparison", "Breakdowns"):
            self.assertNotIn(gone, text)

    def test_totals_count_net_pounds(self):
        net, done, won, lost, flat = [self.live.cell(out.TOTALS_ROW, c).value for c in range(1, 6)]
        self.assertEqual((done, won, lost, flat), (4, 1, 2, 1))
        self.assertAlmostEqual(net, 0.8)
        self.assertEqual(self.live.cell(out.TOTALS_ROW, 9).value, "SYNTHETIC FIXTURE")   # updated time

    def test_trade_list_and_daily_reconcile_with_totals(self):
        net = self.live.cell(out.TOTALS_ROW, 1).value
        self.assertEqual([self.live.cell(out.TRADES_HEADER_ROW, c).value for c in (11, 15)], ["Result", "After £"])
        rows = list(self.trade_rows(self.live))
        self.assertEqual(len(rows), 4)                                       # completed trades only
        results = {self.live.cell(r, 11).value for r in rows}
        self.assertFalse(any(str(v).startswith(("Skipped", "Not resolved", "No data")) for v in results))
        for r in rows:                                                       # before + costs = after
            before, costs, after = (float(self.live.cell(r, c).value) for c in (13, 14, 15))
            self.assertAlmostEqual(before + costs, after, places=2)
        self.assertAlmostEqual(sum(float(self.live.cell(r, 15).value) for r in rows), net)
        self.assertAlmostEqual(self.live.cell(rows[-1], 16).value, net)       # running total ends at the net
        self.assertAlmostEqual(sum(float(self.live.cell(r, 14).value) for r in rows), -1.9)   # dealing + stamp
        daily = self.book["Daily"]
        self.assertAlmostEqual(daily.cell(daily.max_row - 3, 2).value, net)

    def test_comparison_with_every_idea(self):
        avg, every_avg, diff = [self.live.cell(out.COMPARE_ROW, c).value for c in (6, 7, 8)]
        self.assertAlmostEqual(avg, 0.2)
        self.assertAlmostEqual(avg - every_avg, diff, places=2)

    def test_money_formats_and_no_risk_multiples(self):
        for col in (1, 6, 7, 8):
            self.assertEqual(self.live.cell(out.TOTALS_ROW, col).number_format, MONEY)
        for col in (13, 14, 15, 16):
            self.assertEqual(self.live.cell(out.TRADES_HEADER_ROW + 1, col).number_format, MONEY)
        self.assertEqual(self.book["Daily"].cell(2, 2).number_format, MONEY)
        for sheet in self.book.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str):
                        self.assertIsNone(re.search(r"(\bnet R\b|\bavg R\b|cumulative R|\bR,|\(R\)| R after)", cell.value, re.I))

    def test_fractional_pennies_reconcile_per_trade(self):
        self.args.cost_pct = 0
        self.args.stamp_duty_pct = 0
        frame = self.pool.iloc[:3].copy()
        results = self.results.iloc[:3].copy()
        results["r_gross"] = 0.334
        results["fill_risk_pct"] = 1.0
        book = out.build_report(self.args, frame, frame, {out.LIVE: frame.copy()}, results, {}, "SYNTHETIC PENNY CHECK")
        live = book["Live Mock Trades"]
        self.assertEqual(live.cell(out.TOTALS_ROW, 1).value, 0.99)
        self.assertAlmostEqual(sum(live.cell(r, 15).value for r in self.trade_rows(live)), 0.99)

    def test_pending_only_headline_is_not_zero_profit(self):
        self.args.horizon = 2
        book = out.build_report(self.args, self.pool, self.pool, self.picks, self.results, {}, "SYNTHETIC PENDING")
        live = book["Live Mock Trades"]
        self.assertEqual(live.cell(out.TOTALS_ROW, 1).value, "Awaiting results")
        self.assertEqual(live.max_row, out.TRADES_HEADER_ROW)                 # nothing completed, nothing listed

    def test_empty_states(self):
        empty = pd.DataFrame(columns=self.pool.columns)
        results = pd.DataFrame(columns=out.OUTCOME_COLUMNS)
        book = out.build_report(self.args, empty, empty, {out.LIVE: empty.copy()}, results, {}, "SYNTHETIC EMPTY")
        self.assertEqual([book["Live Mock Trades"].cell(out.TOTALS_ROW, c).value for c in range(1, 6)], [0, 0, 0, 0, 0])
        book = out.build_report(self.args, empty, empty, {}, results, {}, "SYNTHETIC NO LIVE")
        self.assertEqual(book["Live Mock Trades"].cell(out.COMPARE_ROW, 6).value, "Awaiting results")


if __name__ == "__main__":
    unittest.main()
