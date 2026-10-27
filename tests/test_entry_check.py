"""Fill check and daily trend filter backtest (27 Oct 2026)."""

import unittest
from unittest.mock import patch

import pandas as pd

import ftse_entry_check_backtest as ec

TZ = "Europe/London"


def idea(ticker, run="2026-10-27 08:32:00", score=90, direction="Short", stop_pct=0.44, entry=1364.0):
    return {"day": "2026-10-27", "run_time": run, "data_time": "2026-10-27T08:15:00+00:00", "ticker": ticker,
            "score": score, "direction": direction, "stop_pct": stop_pct, "entry": entry, "stop": 1369.94, "target": 1346.19}


class FillCheck(unittest.TestCase):
    def fake(self, risks):
        def run(row, intraday, now):
            return {"status": "done", "outcome": "stop", "fill": 1368.5, "exit_time": "2026-10-27T09:15:00+00:00",
                    "r_gross": -1.0, "fill_risk_pct": risks[row["ticker"]], "observed_at": ""}
        return run

    def test_pearson_is_skipped_by_both_checks(self):
        with patch.object(ec.mc, "simulate_one", side_effect=self.fake({"PSON.L": 0.105})):
            row = idea("PSON.L")
            now = pd.Timestamp("2026-10-27 18:00", tz=TZ)
            for check in (ec.half_left, ec.two_tenths_left):
                r = ec.simulate_checked(row, {}, now, check)
                self.assertEqual((r["status"], r["outcome"]), ("skipped", "thin_stop"))
                self.assertEqual(pd.Timestamp(r["observed_at"]), pd.Timestamp("2026-10-27 08:45", tz=TZ))
            self.assertEqual(ec.simulate_checked(row, {}, now, None)["status"], "done")

    def test_minimum_room_adds_to_the_fill_check(self):
        check = ec.room_at_least(0.7)
        self.assertTrue(check({"stop_pct": 0.8}, {"fill_risk_pct": 0.75}))
        self.assertFalse(check({"stop_pct": 0.8}, {"fill_risk_pct": 0.65}))       # under 0.7%
        self.assertFalse(check({"stop_pct": 2.0}, {"fill_risk_pct": 0.9}))        # under half the planned room

    def test_check_thresholds(self):
        self.assertTrue(ec.half_left({"stop_pct": 0.44}, {"fill_risk_pct": 0.23}))
        self.assertFalse(ec.half_left({"stop_pct": 0.44}, {"fill_risk_pct": 0.21}))
        self.assertTrue(ec.two_tenths_left({"stop_pct": 0.1}, {"fill_risk_pct": 0.2}))

    def test_skipped_trade_frees_its_place_at_the_fill(self):
        ideas = pd.DataFrame([idea(f"S{i}.L", score=90 - i) for i in range(5)] +
                             [idea("THIN.L", score=99)] + [idea("LATE.L", run="2026-10-27 09:02:00")])
        risks = {f"S{i}.L": 0.5 for i in range(5)} | {"THIN.L": 0.05, "LATE.L": 0.5}

        def run(row, intraday, now):
            r = self.fake(risks)(row, intraday, now)
            return {**r, "exit_time": "2026-10-27T16:30:00+00:00"}
        with patch.object(ec.mc, "simulate_one", side_effect=run):
            taken = ec.live_trader(ideas, {}, pd.Timestamp("2026-10-27 18:00", tz=TZ), ec.half_left)
        self.assertEqual(list(taken["ticker"]), ["THIN.L", "S0.L", "S1.L", "S2.L", "S3.L", "LATE.L"])
        self.assertEqual(taken.set_index("ticker").loc["THIN.L", "outcome"], "thin_stop")


class DailyTrend(unittest.TestCase):
    def test_buys_above_and_sells_below_the_50_day_average(self):
        closes = pd.Series([100.0] * 60, index=pd.bdate_range("2026-08-01", periods=60))
        day = str(pd.bdate_range("2026-08-01", periods=61)[-1].date())
        ideas = pd.DataFrame([dict(idea("A.L", direction="Long", entry=101), day=day),
                              dict(idea("A.L", direction="Short", entry=101), day=day),
                              dict(idea("B.L", direction="Short", entry=99), day=day)])
        flags = ec.add_daily_trend(ideas, {"A.L": closes, "B.L": closes})["daily_trend_ok"].tolist()
        self.assertEqual(flags, [True, False, True])


if __name__ == "__main__":
    unittest.main()
