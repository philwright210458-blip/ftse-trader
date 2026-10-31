import unittest

import numpy as np
import pandas as pd

import ftse_dip_backtest as dm


def frame(closes, dividends=None, start="2020-01-01"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(start, periods=len(closes))
    f = pd.DataFrame({"open": closes, "high": closes * 1.001, "low": closes * 0.999, "close": closes}, index=idx)
    f["dividend"] = 0.0 if dividends is None else dividends
    return f


def slide_and_turn():
    # 25 days flat at 100, slide to 96 over 3 days, then a higher close (signal), then a rally
    closes = [100.0] * 25 + [99.0, 97.5, 96.0, 96.5] + [97, 98, 99.5, 100.5, 101, 101, 101]
    return frame(closes)


class DipMethodTests(unittest.TestCase):
    def test_signal_waits_for_the_turn(self):
        f = slide_and_turn()
        sigs = dm.signals(f, wait_for_turn=True)
        self.assertEqual([s["t"] for s in sigs], [28])               # the first higher close after the slide
        self.assertAlmostEqual(sigs[0]["depth"], -0.04)

    def test_without_waiting_buys_the_first_dip_close(self):
        f = slide_and_turn()
        self.assertEqual([s["t"] for s in dm.signals(f, wait_for_turn=False)], [27])

    def test_target_and_time_exit(self):
        f = slide_and_turn()
        hit = dm.exit_trade(f, 28)                                    # buys 97 at day 29's open
        self.assertEqual(hit["outcome"], "target")                    # 97 * 1.03 = 99.91; day 32 opens past it at 100.5
        self.assertAlmostEqual(hit["gross"], (100.5 - 97) / 97 * 100, places=6)
        flat = frame([100.0] * 30)
        timed = dm.exit_trade(flat, 10)
        self.assertEqual(timed["outcome"], "time")
        self.assertEqual(timed["days"], dm.HOLD_DAYS)
        self.assertAlmostEqual(timed["gross"], 0.0)

    def test_dividend_counted_only_if_held_over_ex_date(self):
        div = np.zeros(30)
        div[12] = 2.0                                                 # ex-date on day 12
        f = frame([100.0] * 30, div)
        self.assertAlmostEqual(dm.exit_trade(f, 10)["dividend"], 2.0)  # bought day 11, held over day 12
        self.assertAlmostEqual(dm.exit_trade(f, 11)["dividend"], 0.0)  # bought on the ex-date itself: not entitled

    def test_skip_crash_and_dividend_dip(self):
        closes = [100.0] * 25 + [95, 90, 88, 89, 90, 91, 92, 93, 94]
        self.assertTrue(dm.signals(frame(closes)))
        self.assertFalse(dm.signals(frame(closes), skip_crash=True))
        div = np.zeros(len(closes))
        div[26] = 1.0
        self.assertTrue(dm.signals(frame(closes, div))[0]["div_in_dip"])
        self.assertFalse(dm.signals(frame(closes, div), skip_dividend_dip=True))

    def test_choose_limits_open_trades(self):
        day = pd.Timestamp("2021-01-04")
        rows = [{"ticker": f"S{i}.L", "entry_day": day, "exit_day": day + pd.Timedelta(days=6), "depth": -0.03 - i / 100}
                for i in range(5)]
        rows.append({"ticker": "S0.L", "entry_day": day + pd.Timedelta(days=1), "exit_day": day + pd.Timedelta(days=8),
                     "depth": -0.09})
        taken = dm.choose(pd.DataFrame(rows))
        self.assertEqual(list(taken["ticker"]), ["S4.L", "S3.L", "S2.L"])  # deepest three; the rest wait

    def test_tidy_dividends_fixes_units(self):
        close = pd.Series([500.0, 500.0, 500.0])
        div = pd.Series([0.0, 0.2, 2000.0], index=close.index)
        fixed = dm.tidy_dividends(div, close)
        self.assertEqual(list(fixed), [0.0, 20.0, 20.0])


if __name__ == "__main__":
    unittest.main()
