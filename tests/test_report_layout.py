"""Workbook arithmetic, coverage and interpretation regressions."""

import importlib.util
from pathlib import Path
import unittest

import pandas as pd
import ftse_outcomes as out

spec = importlib.util.spec_from_file_location("report_fixture", Path(__file__).with_name("report_fixture.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.args, self.pool, self.picks, self.results = module.fixture()
        self.book = out.build_report(self.args, self.pool, self.pool, self.picks, self.results, {}, "SYNTHETIC FIXTURE")

    def test_tab_order_and_explicit_fixture_label(self):
        self.assertEqual(
            self.book.sheetnames,
            ["Summary", "Strategy Comparison", "Breakdowns", "Mock Trades", "Live Mock Trades", "Daily", "All Outcomes"],
        )
        self.assertIn("SYNTHETIC FIXTURE", self.book["Summary"].cell(2, 1).value)

    def test_main_counts_net_pounds_not_price_event(self):
        ws = self.book["Summary"]
        self.assertEqual([ws.cell(6, c).value for c in range(2, 6)], [4, 1, 2, 1])
        self.assertEqual([ws.cell(12, c).value for c in range(1, 6)], [7, 4, 1, 1, 1])
        # Two targets, but one is a net loss after costs; timeout is flat net.
        self.assertEqual([ws.cell(18, c).value for c in range(2, 5)], [2, 1, 1])

    def test_actual_cash_bridge_mock_and_daily_reconcile(self):
        ws = self.book["Summary"]
        gross, dealing, stamp, net = [ws.cell(9, c).value for c in range(1, 5)]
        self.assertAlmostEqual(gross, 2.7)
        self.assertAlmostEqual(dealing, -0.4)
        self.assertAlmostEqual(stamp, -1.5)
        self.assertAlmostEqual(net, 0.8)
        self.assertAlmostEqual(gross + dealing + stamp, net)
        self.assertEqual(ws.cell(6, 1).value, net)
        mock = self.book["Mock Trades"]
        totals = [sum(float(mock.cell(r, c).value or 0) for r in range(3, mock.max_row + 1)) for c in (14, 15, 16, 17)]
        for expected, actual in zip((gross, dealing, stamp, net), totals):
            self.assertAlmostEqual(expected, actual)
        self.assertAlmostEqual(self.book["Daily"].cell(3, 3).value, net)

    def test_alternative_scenarios_reconcile_and_same_cost_basis(self):
        ws = self.book["Strategy Comparison"]
        headers = [
            r
            for r in range(1, ws.max_row + 1)
            if ws.cell(r, 1).value == "Scenario" and ws.cell(r, 3).value == "Completed"
        ]
        self.assertEqual(len(headers), 3)
        for block, header in enumerate(headers):
            for row in range(header + 1, header + 5):
                selected, completed, pending, missing, skipped = [ws.cell(row, c).value for c in range(2, 7)]
                self.assertEqual(selected, completed + skipped + pending + missing)
                if completed:
                    avg, poolavg, delta = [ws.cell(row, c).value for c in (7, 8, 9)]
                    self.assertAlmostEqual(avg - poolavg, delta, places=3)
                elif selected:
                    self.assertEqual(ws.cell(row, 7).value, "Awaiting results")
                    self.assertEqual(ws.cell(row, 10).value, "Awaiting results")
            if block:
                self.assertEqual(ws.cell(header + 2, 4).value, 7)
        text = " ".join(str(c.value or "") for row in ws for c in row)
        self.assertIn("rather than a matched-time causal ranking test", text)
        self.assertNotIn("Break-even target-hit rate", text)

    def test_no_risk_multiple_units_shown(self):
        import re
        for sheet in self.book.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str):
                        self.assertIsNone(re.search(r"(\bnet R\b|\bavg R\b|cumulative R|\bR,|\(R\)| R after)", cell.value, re.I),
                                          f"{sheet.title}!{cell.coordinate}: {cell.value}")

    def test_currency_r_and_sample_formats(self):
        summary = self.book["Summary"]
        for row, col in ((6, 1), (9, 1), (9, 2), (9, 3), (9, 4)):
            self.assertIn("£", summary.cell(row, col).number_format)
            self.assertIn("0.00", summary.cell(row, col).number_format)
        self.assertIn("Evidence: 4 completed trades across 2 market day", summary.cell(4, 1).value)
        self.assertIn("£", self.book["Mock Trades"].cell(3, 17).number_format)
        self.assertIn("£", self.book["Daily"].cell(2, 3).number_format)
        comparison = self.book["Strategy Comparison"]
        self.assertIn("£", comparison.cell(7, 9).number_format)
        self.assertIn("£", comparison.cell(14, 9).number_format)
        alltexts = " ".join(str(c.value or "") for row in self.book["Breakdowns"] for c in row)
        self.assertIn("at least 4 bars apart", alltexts)
        self.assertIn("0.3% (FTSE 100) or 0.4% (FTSE 250)", alltexts)
        self.assertIn("5 trades / 3 market days", alltexts)

    def test_breakdown_weighted_mean_and_count_shares(self):
        ws = self.book["Breakdowns"]
        baseline = float(ws.cell(6, 4).value)
        groups = []
        row = 7
        while isinstance(ws.cell(row, 2).value, int):
            groups.append([ws.cell(row, c).value for c in (2, 3, 4, 5)])
            row += 1
        self.assertEqual(sum(g[0] for g in groups), 5)
        self.assertAlmostEqual(sum(g[1] for g in groups), 1)
        self.assertAlmostEqual(sum(g[0] * g[2] for g in groups) / 5, baseline, places=3)
        for n, share, avg, delta in groups:
            self.assertAlmostEqual(avg - baseline, delta, places=3)
        text = " ".join(str(c.value or "") for row in ws for c in row)
        self.assertNotIn("reasonable", text)
        self.assertIn("may be correlated", text)

    def test_supporting_event_counts_are_integers(self):
        ws = self.book["Breakdowns"]
        header = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == "Scope")
        for col in (2, 3, 4, 5):
            self.assertEqual(ws.cell(header + 1, col).number_format, "0")
        self.assertEqual(self.book["Summary"].cell(6, 1).number_format, '"£"#,##0.00;[Red]-"£"#,##0.00;"£"0.00')

    def test_fractional_pennies_reconcile_per_trade(self):
        self.args.cost_pct = 0
        self.args.stamp_duty_pct = 0
        frame = self.pool.iloc[:3].copy()
        results = self.results.iloc[:3].copy()
        results["r_gross"] = 0.334
        results["fill_risk_pct"] = 1.0
        picks = {name: frame.copy() for name in out.PICK_SCENARIOS}
        book = out.build_report(self.args, frame, frame, picks, results, {}, "SYNTHETIC PENNY CHECK")
        self.assertEqual(book["Summary"].cell(6, 1).value, 0.99)
        mock = book["Mock Trades"]
        self.assertAlmostEqual(sum(mock.cell(r, 17).value for r in range(3, 6)), 0.99)
        self.assertEqual(book["Daily"].cell(3, 3).value, 0.99)

    def test_pending_only_headline_is_not_zero_profit(self):
        self.args.horizon = 2
        book = out.build_report(self.args, self.pool, self.pool, self.picks, self.results, {}, "SYNTHETIC PENDING")
        self.assertEqual(book["Summary"].cell(6, 1).value, "Awaiting results")
        self.assertEqual([book["Summary"].cell(9, c).value for c in range(1, 5)], ["Awaiting results"] * 4)

    def test_zero_completed_and_empty_candidate_states(self):
        empty = pd.DataFrame(columns=self.pool.columns)
        picks = {name: empty.copy() for name in out.PICK_SCENARIOS}
        results = pd.DataFrame(columns=out.OUTCOME_COLUMNS)
        book = out.build_report(self.args, empty, empty, picks, results, {}, "SYNTHETIC EMPTY")
        self.assertEqual([book["Summary"].cell(6, c).value for c in range(1, 6)], [0, 0, 0, 0, 0])
        self.assertEqual([book["Summary"].cell(12, c).value for c in range(1, 6)], [0, 0, 0, 0, 0])
        self.assertIn("No completed", book["Breakdowns"].cell(4, 1).value)


if __name__ == "__main__":
    unittest.main()
