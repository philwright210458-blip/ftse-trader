"""Fill check (28 Oct 2026): skip a trade when the entry price leaves less than half the planned room to the stop."""

import unittest

import pandas as pd

import ftse_execution as ex
import ftse_live_mock as live

TZ = "Europe/London"


def candles(day, opens):
    index = pd.date_range(f"{day} 08:45", periods=len(opens), freq="15min", tz=TZ)
    rows = [(o, o + 0.5, o - 0.5, o) for o in opens]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index).assign(volume=1000)


class FillCheck(unittest.TestCase):
    def sim(self, day, fill, planned=1364.0, stop=1369.94):
        data = candles(day, [fill, fill - 1, fill - 2])
        signal = pd.Timestamp(f"{day} 08:32:26", tz=TZ)
        return ex.simulate("Short", stop, 1346.19, signal, data, 1, pd.Timestamp(f"{day} 18:00", tz=TZ), "15m",
                           planned_entry=planned)

    def test_pearson_like_fill_is_skipped_from_28_october(self):
        s = self.sim("2026-10-28", 1368.5)                    # 0.10% left of a planned 0.44%
        self.assertEqual((s.status, s.outcome), ("skipped", "thin_stop"))
        self.assertEqual(pd.Timestamp(s.observed_at), pd.Timestamp("2026-10-28 08:45", tz=TZ))

    def test_enough_room_trades_normally(self):
        self.assertNotEqual(self.sim("2026-10-28", 1366.0).outcome, "thin_stop")   # 3.94 of 5.94 left

    def test_not_applied_before_28_october_or_without_planned_price(self):
        self.assertNotEqual(self.sim("2026-10-27", 1368.5).outcome, "thin_stop")
        self.assertNotEqual(self.sim("2026-10-28", 1368.5, planned=None).outcome, "thin_stop")
        self.assertNotEqual(self.sim("2026-10-28", 1368.5, planned="").outcome, "thin_stop")

    def test_live_sheet_wording_and_slot_freed(self):
        row = {"signal_time": "2026-10-28 08:32:26", "data_time": "2026-10-28T08:15:00+00:00", "direction": "Short",
               "entry": 1364.0, "stop": 1369.94, "target": 1346.19}
        progress = live.mock_progress(row, candles("2026-10-28", [1368.5, 1367, 1366]), 15,
                                      now=pd.Timestamp("2026-10-28 09:20", tz=TZ))
        self.assertEqual(progress["status"], "Skipped: stop too close at entry")
        self.assertEqual(live.still_open([{"progress": progress}]), 0)


if __name__ == "__main__":
    unittest.main()
