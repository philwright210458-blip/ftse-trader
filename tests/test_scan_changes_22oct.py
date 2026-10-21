"""Changes of 22 Oct 2026: all-share watch scans, batch downloads, one live trade per scan,
and gap trades resolved from the day's high and low."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import ftse_live_mock as live
import ftse_outcomes as out
import ftse_trade_scanner as sc
from ftse_sessions import session_open_close


def session_candles(day, price=100.0, skip=(), width=0.2):
    opening, closing = session_open_close(pd.Timestamp(day))
    stamps = [s for s in pd.date_range(opening, closing, freq="15min", inclusive="left") if s.strftime("%H:%M") not in skip]
    rows = [(price, price + width, price - width, price, 1000) for _ in stamps]
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(stamps, name="datetime"))


class GapFallbackTests(unittest.TestCase):
    day = "2026-10-15"
    signal = pd.Timestamp("2026-10-15 10:32", tz=sc.UK_TZ)
    now = pd.Timestamp("2026-10-21 17:05", tz=sc.UK_TZ)

    def daily(self, high, low, close=100.0):
        return pd.DataFrame({"high": [high], "low": [low], "close": [close]}, index=[pd.Timestamp(self.day)])

    def test_gap_without_daily_stays_unresolved(self):
        data = session_candles(self.day, skip=("11:00",))
        sim = out.simulate("Long", 98.0, 106.0, self.signal, data, 1, self.now)
        self.assertIn(sim.status, ("pending", "nodata"))      # unresolved

    def test_gap_resolved_when_day_never_reached_stop_or_target(self):
        data = session_candles(self.day, skip=("11:00",))
        sim = out.simulate("Long", 98.0, 106.0, self.signal, data, 1, self.now, daily=self.daily(100.5, 99.5))
        self.assertEqual((sim.status, sim.outcome, sim.exit_price), ("done", "timeout", 100.0))

    def test_gap_stays_unresolved_when_day_touched_the_stop(self):
        data = session_candles(self.day, skip=("11:00",))
        sim = out.simulate("Long", 98.0, 106.0, self.signal, data, 1, self.now, daily=self.daily(100.5, 97.5))
        self.assertIn(sim.status, ("pending", "nodata"))      # unresolved

    def test_missing_last_candle_uses_daily_close(self):
        data = session_candles(self.day, skip=("16:15",))
        sim = out.simulate("Short", 102.0, 94.0, self.signal, data, 1, self.now, daily=self.daily(100.5, 99.5, 99.0))
        self.assertEqual((sim.status, sim.outcome, sim.exit_price), ("done", "timeout", 99.0))

    def test_daily_prices_index_is_plain_dates(self):
        raw = pd.DataFrame({"High": [1.0], "Low": [0.5], "Close": [0.8]},
                           index=pd.DatetimeIndex([pd.Timestamp("2026-10-15", tz=sc.UK_TZ)]))
        self.assertEqual(list(out.daily_prices(raw).index), [pd.Timestamp("2026-10-15")])


class LivePerScanTests(unittest.TestCase):
    def test_only_one_new_trade_per_scan(self):
        info = SimpleNamespace(day="2026-10-22", run_time="2026-10-22 08:16:30", data_time=pd.Timestamp("2026-10-22 08:00", tz=sc.UK_TZ),
                               mode="full")
        zone = sc.Zone(99.0, 99.5)
        setups = [sc.TradeSetup(f"Co {n}", f"T{n}.L", "FTSE 100", "Range", "Long", zone, 100.0, 98.0, 106.0, "",
                                quality_score=90 - n) for n in range(5)]
        with tempfile.TemporaryDirectory() as folder:
            first = live.record_live_mock(Path(folder), info, setups, 3, max_per_scan=1)
            self.assertEqual([r["ticker"] for r in first], ["T0.L"])
            info.run_time = "2026-10-22 08:31:30"
            second = live.record_live_mock(Path(folder), info, setups, 3, max_per_scan=1)
            self.assertEqual([r["ticker"] for r in second], ["T1.L"])


class BatchDownloadTests(unittest.TestCase):
    def test_fetch_many_splits_by_ticker_and_skips_missing(self):
        index = pd.date_range("2026-10-21 08:00", periods=3, freq="15min", tz=sc.UK_TZ)
        columns = pd.MultiIndex.from_product([["A.L", "B.L"], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]])
        raw = pd.DataFrame(1.0, index=index, columns=columns)
        raw.loc[:, "B.L"] = float("nan")                  # B failed to download
        with mock.patch.object(sc.yf, "download", return_value=raw):
            got = sc.fetch_many(["A.L", "B.L"], "15m", "60d", pause=0)
        self.assertEqual(list(got), ["A.L"])
        self.assertIn("close", got["A.L"].columns)

    def test_run_scan_falls_back_to_one_by_one(self):
        company = sc.Constituent("Alpha", "A", "A.L", "FTSE 100")
        with mock.patch.object(sc, "fetch_many", return_value={}), \
                mock.patch.object(sc, "fetch_data", side_effect=RuntimeError("no data")) as single:
            setups, failures, latest, lags = sc.run_scan([company], "15m", "60d", 0, batch=True)
        single.assert_called_once()
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
