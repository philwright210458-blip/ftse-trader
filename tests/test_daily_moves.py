"""Daily FTSE 100 moves and picker scorecard (26 Oct 2026)."""

import unittest

import pandas as pd

import ftse_daily_moves as dm


def frame(rows, start="2026-10-22"):
    index = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)


class DailyMoves(unittest.TestCase):
    def test_moves_sorted_and_picker_scored(self):
        frames = {"CCC.L": frame([(4800, 4800, 4700, 4764), (4764, 4780, 4600, 4618), (4620, 5300, 4610, 5219)]),
                  "AAF.L": frame([(270, 275, 268, 273), (273, 275, 272, 274.4), (274, 276, 271, 272)]),
                  "BRBY.L": frame([(1050, 1060, 1040, 1056), (1056, 1061, 1050, 1058), (1058, 1062, 1050, 1058)])}
        names = {"CCC.L": "Computacenter", "AAF.L": "Airtel Africa", "BRBY.L": "Burberry"}
        views = {"CCC.L": ("Buy", ""), "AAF.L": ("Buy", ""), "BRBY.L": ("Sell", "")}
        table = dm.day_moves(frames, names, "2026-10-26", views)
        self.assertEqual(list(table["ticker"]), ["CCC.L", "BRBY.L", "AAF.L"])
        ccc = table.iloc[0]
        self.assertAlmostEqual(ccc["change_pct"], 13.01)
        self.assertEqual((ccc["picker_view"], ccc["picker_right"]), ("Buy", "Yes"))
        self.assertEqual(table.set_index("ticker").loc["AAF.L", "picker_right"], "No")
        self.assertEqual(table.set_index("ticker").loc["BRBY.L", "picker_right"], "")   # flat day: not scored

    def test_missing_day_and_no_picker(self):
        frames = {"CCC.L": frame([(1, 1, 1, 1), (1, 1.1, 0.9, 1.05)])}
        self.assertTrue(dm.day_moves(frames, {}, "2026-10-26", None).empty)
        table = dm.day_moves(frames, {}, "2026-10-23", None)
        self.assertEqual((table.iloc[0]["picker_view"], table.iloc[0]["picker_right"]), ("", ""))

    def test_picker_paper_trades_and_running_file(self):
        import tempfile
        from pathlib import Path
        frames = {"IAG.L": frame([(300, 301, 299, 300), (300, 310, 299, 308.55)], start="2026-10-23"),
                  "BP.L": frame([(450, 452, 448, 451), (451, 452, 446, 447.8)], start="2026-10-23"),
                  "XX.L": frame([(10, 11, 9, 10), (10, 11, 9, 10.5)], start="2026-10-23")}
        views = {"IAG.L": ("Buy", ""), "BP.L": ("Sell", ""), "XX.L": ("Avoid", "")}
        table = dm.day_moves(frames, {}, "2026-10-26", views)
        trades = dm.picker_trades(table)
        self.assertEqual(sorted(trades["ticker"]), ["BP.L", "IAG.L"])            # Avoid is not traded
        iag = trades.set_index("ticker").loc["IAG.L"]
        self.assertEqual((iag["gross_gbp"], iag["costs_gbp"], iag["net_gbp"]), (2.85, 0.1, 2.75))
        self.assertAlmostEqual(trades.set_index("ticker").loc["BP.L", "net_gbp"], 0.61)
        self.assertAlmostEqual(table.set_index("ticker").loc["IAG.L", "picker_net_gbp"], 2.75)
        with tempfile.TemporaryDirectory() as folder:
            dm.save_picker_trades(Path(folder), "2026-10-26", trades)
            every = dm.save_picker_trades(Path(folder), "2026-10-26", trades)   # re-run replaces, no duplicates
            self.assertEqual(len(every), 2)


if __name__ == "__main__":
    unittest.main()
