"""FTSE Trader Today page (GitHub Pages)."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from openpyxl import Workbook

import ftse_site as site


class SitePage(unittest.TestCase):
    def test_builds_from_saved_files_and_escapes_text(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wb = Workbook(); ws = wb.active
            ws.append(["Scan generated 2026-10-27 10:47 UK time | watch scan, data to 10:45 UK"])
            ws.append(["Mock trades made today ..."])
            ws.append(["#", "Signal time", "Company", "Ticker", "Setup", "Direction", "Entry (filled)", "Stop", "Target",
                       "Status so far", "At", "Latest or exit price", "Before costs per £100 (£)", "Costs (£)",
                       "After costs per £100 (£)"])
            ws.append([1, "08:32", "Alpha <b>plc</b>", "A.L", "Range", "Buy", 100, 99, 102, "Open", "10:45", 100.5, 0.5, 0.1, 0.4])
            ws.append([2, "09:02", "Beta", "B.L", "Reclaim", "Sell", 50, 51, 49, "Stopped out", "09:30", 51, -2, 0.1, -2.1])
            ws.append([3, "09:17", "Gamma", "C.L", "Range", "Buy", None, 99, 102,
                       "Skipped: stop too far away (loss over about £1)", "", None, None, None, None])
            ws.append([4, "09:32", "Delta", "D.L", "Range", "Buy", None, 99, 102,
                       "Skipped: target left smaller than the stop", "", None, None, None, None])
            wb.save(root / "ftse_scan_latest.xlsx")
            (root / "scan_data" / "picker").mkdir(parents=True)
            pd.DataFrame([{"day": "2026-10-27", "ticker": "A.L", "company": "Alpha", "view": "Buy", "reason": "Upgrade",
                           "made_at": "2026-10-27 07:10"}]).to_csv(root / "scan_data" / "picker" / "2026-10-27.csv", index=False)
            page = site.build(root, datetime(2026, 10, 27, 11, 0, tzinfo=ZoneInfo("Europe/London")))
        self.assertIn("<title>FTSE Trader Today</title>", page)
        self.assertIn("Market open", page)
        self.assertIn("Alpha &lt;b&gt;plc&lt;/b&gt;", page)          # shown as text, never as markup
        self.assertIn("-£1.70", page)                                 # today's total after costs
        self.assertIn("2 trade(s): 0 won, 1 lost, 1 open", page)
        self.assertIn("Agrees", page)
        # skipped ideas are not trades: one folded line, not table rows
        self.assertIn("2 idea(s) skipped by the entry rules (1 stop too wide, 1 small target)", page)
        self.assertNotIn("<td>Gamma</td>", page)

    def test_momentum_panel(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, value in (("ftse_momentum_live.xlsx", 992.1), ("ftse_momentum_live_no_filter.xlsx", 990.12)):
                wb = Workbook(); ws = wb.active; ws.title = "Summary"
                for row in (("Started", "2026-11-02"), ("Prices up to", "2026-11-04"), ("Value now (£)", value),
                            ("£1,000 in a FTSE 100 tracker on the same day is now (£)", 1010.71),
                            ("Next decision", "after the close on 2026-11-30"),
                            ("FTSE 100 versus its 200-day average", "above: the model may hold shares")):
                    ws.append(row)
                h = wb.create_sheet("Holdings")
                h.append(["Company", "Share", "Bought", "Buy price", "Cost (£)", "Value now (£)", "Dividends (£)",
                          "Profit / loss (£)", "Per £100 (£)"])
                h.append(["CMC Markets", "CMCX.L", "2026-11-02", 695, 100, 101.84, 0, 1.84, 1.84])
                wb.save(root / name)
            page = site.build(root, datetime(2026, 11, 4, 20, 0, tzinfo=ZoneInfo("Europe/London")))
        self.assertIn("Momentum model (monthly)", page)
        self.assertIn("£992.10", page)
        self.assertIn("-£9.88", page)                                  # the no-filter portfolio's change
        self.assertIn("+£10.71", page)                                 # the tracker
        self.assertIn("after the close on 2026-11-30", page)
        self.assertIn("in the market", page)
        self.assertIn("CMC Markets", page)

    def test_empty_repo_still_builds(self):
        with tempfile.TemporaryDirectory() as folder:
            page = site.build(Path(folder), datetime(2026, 10, 31, 12, 0, tzinfo=ZoneInfo("Europe/London")))
        self.assertIn("No trades yet today", page)
        self.assertIn("Market closed", page)


if __name__ == "__main__":
    unittest.main()
