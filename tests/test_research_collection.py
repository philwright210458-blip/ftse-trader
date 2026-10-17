"""Provider boundary checks with injected fictional frames; never network calls."""
import unittest

import pandas as pd

from ftse_research.collection import collect_yahoo


class CollectionTests(unittest.TestCase):
    def test_partial_and_zero_data_bars_do_not_become_opening_signals(self):
        def download(**kwargs):
            if kwargs["interval"] == "1d":
                return pd.DataFrame({"Open": [1000], "High": [1001], "Low": [999], "Close": [1000],
                                     "Adj Close": [1000], "Volume": [100]}, index=pd.to_datetime(["2026-10-15"]))
            return pd.DataFrame({"Open": [1000, 1000], "High": [1001, 1001], "Low": [999, 999],
                                 "Close": [1000, 1000], "Volume": [0, 100]},
                                index=pd.date_range("2026-10-16 08:00", periods=2, freq="15min", tz="Europe/London"))
        records, errors = collect_yahoo([{"ticker": "TEST", "quote_unit": "GBp"}],
                                        "2026-10-16T07:20:00Z", downloader=download)
        self.assertFalse(errors)
        self.assertEqual(len(records), 2)  # one prior daily close, one completed zero-volume bar
        self.assertEqual([c.volume for c in records if c.interval == "15m"], [0])
        self.assertTrue(all(pd.Timestamp(c.end) <= pd.Timestamp(c.fetched_at) for c in records))

    def test_naive_intraday_timestamp_rejected(self):
        def download(**kwargs):
            return pd.DataFrame({"Open": [1000], "High": [1001], "Low": [999], "Close": [1000],
                                 "Adj Close": [1000], "Volume": [100]}, index=pd.to_datetime(["2026-10-16 08:00"]))
        records, errors = collect_yahoo([{"ticker": "TEST", "quote_unit": "GBp"}],
                                        "2026-10-16T08:20:00Z", downloader=download)
        self.assertTrue(any("15m" in message for message in errors))
        self.assertFalse([row for row in records if row.interval == "15m"])

    def test_provider_failure_has_one_attempt_per_requested_interval(self):
        calls = []
        def download(**kwargs):
            calls.append(kwargs["interval"])
            raise TimeoutError("Fictional outage")
        records, errors = collect_yahoo([{"ticker": "TEST", "quote_unit": "GBp"}],
                                        "2026-10-16T08:20:00Z", downloader=download)
        self.assertEqual(calls, ["1d", "15m"])
        self.assertEqual(len(errors), 2)
        self.assertFalse(records)

    def test_intraday_initial_download_bootstraps_sixty_day_history(self):
        periods = []
        def download(**kwargs):
            periods.append((kwargs["interval"], kwargs["period"]))
            return pd.DataFrame()
        collect_yahoo([{"ticker": "TEST", "quote_unit": "GBp"}],
                      "2026-10-16T08:20:00Z", downloader=download)
        self.assertEqual(periods, [("1d", "1mo"), ("15m", "60d")])

    def test_grouped_multi_ticker_frame_preserves_instrument_units(self):
        def download(**kwargs):
            if kwargs["interval"] == "1d":
                return pd.DataFrame()
            frame = pd.DataFrame({"Open": [10], "High": [11], "Low": [9], "Close": [10], "Volume": [100]},
                                 index=pd.DatetimeIndex([pd.Timestamp("2026-10-16 08:00", tz="Europe/London")]))
            return pd.concat({"GBP-TEST": frame, "PENCE-TEST": frame * 100}, axis=1)
        records, errors = collect_yahoo([{"ticker": "GBP-TEST", "quote_unit": "GBP"},
                                         {"ticker": "PENCE-TEST", "quote_unit": "GBp"}],
                                        "2026-10-16T07:20:00Z", downloader=download)
        self.assertEqual({row.ticker: row.quote_unit for row in records}, {"GBP-TEST": "GBP", "PENCE-TEST": "GBp"})


if __name__ == "__main__":
    unittest.main()
