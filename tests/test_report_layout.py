"""Slim outcomes workbook (23 Oct 2026): live mock trades with totals and one comparison line, plus the daily balance."""

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
        self.assertEqual(self.book.sheetnames, ["Live Mock Trades", "Daily"])
        self.assertIn("SYNTHETIC FIXTURE", self.live.cell(2, 1).value)
        text = " ".join(str(c.value or "") for ws in self.book.worksheets for row in ws for c in row)
        for gone in ("Top 2 per day", "at or after 10:30", "Strategy Comparison", "Breakdowns"):
            self.assertNotIn(gone, text)

    def test_totals_count_net_pounds(self):
        row = [self.live.cell(out.TOTALS_ROW, c).value for c in range(1, 10)]
        net, done, won, lost, flat, waiting, gross, dealing, stamp = row
        self.assertEqual((done, won, lost, flat, waiting), (4, 1, 2, 1, 3))
        self.assertAlmostEqual(gross, 2.7)
        self.assertAlmostEqual(dealing, -0.4)
        self.assertAlmostEqual(stamp, -1.5)
        self.assertAlmostEqual(net, 0.8)

    def test_trade_list_and_daily_reconcile_with_totals(self):
        net = self.live.cell(out.TOTALS_ROW, 1).value
        self.assertEqual([self.live.cell(out.TRADES_HEADER_ROW, c).value for c in (11, 17)], ["Result", "Net after all fees (£)"])
        rows = list(self.trade_rows(self.live))
        self.assertEqual(len(rows), 7)
        for col, total_col in ((14, 7), (15, 8), (16, 9), (17, 1)):
            self.assertAlmostEqual(sum(float(self.live.cell(r, col).value or 0) for r in rows),
                                   self.live.cell(out.TOTALS_ROW, total_col).value)
        daily = self.book["Daily"]
        self.assertAlmostEqual(daily.cell(daily.max_row - 3, 2).value, net)

    def test_comparison_with_every_idea(self):
        _, done, avg, every_done, every_avg, diff = [self.live.cell(out.COMPARE_ROW, c).value for c in range(1, 7)]
        self.assertEqual((done, every_done), (4, 5))
        self.assertAlmostEqual(avg, 0.2)
        self.assertAlmostEqual(avg - every_avg, diff, places=2)

    def test_money_formats_and_no_risk_multiples(self):
        for col in (1, 7, 8, 9):
            self.assertEqual(self.live.cell(out.TOTALS_ROW, col).number_format, MONEY)
        self.assertEqual(self.live.cell(out.TRADES_HEADER_ROW + 1, 17).number_format, MONEY)
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
        self.assertAlmostEqual(sum(live.cell(r, 17).value for r in self.trade_rows(live)), 0.99)

    def test_pending_only_headline_is_not_zero_profit(self):
        self.args.horizon = 2
        book = out.build_report(self.args, self.pool, self.pool, self.picks, self.results, {}, "SYNTHETIC PENDING")
        live = book["Live Mock Trades"]
        self.assertEqual(live.cell(out.TOTALS_ROW, 1).value, "Awaiting results")
        self.assertEqual(live.cell(out.TOTALS_ROW, 6).value, 7)

    def test_empty_states(self):
        empty = pd.DataFrame(columns=self.pool.columns)
        results = pd.DataFrame(columns=out.OUTCOME_COLUMNS)
        book = out.build_report(self.args, empty, empty, {out.LIVE: empty.copy()}, results, {}, "SYNTHETIC EMPTY")
        self.assertEqual([book["Live Mock Trades"].cell(out.TOTALS_ROW, c).value for c in range(1, 7)], [0, 0, 0, 0, 0, 0])
        book = out.build_report(self.args, empty, empty, {}, results, {}, "SYNTHETIC NO LIVE")
        self.assertEqual(book["Live Mock Trades"].cell(out.COMPARE_ROW, 3).value, "Awaiting results")


if __name__ == "__main__":
    unittest.main()
