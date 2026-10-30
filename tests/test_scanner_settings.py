import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd

import ftse_live_mock as lm
import ftse_settings as fs
from ftse_execution import simulate

UK = "Europe/London"


class SettingsFileTests(unittest.TestCase):
    def test_shipped_file_is_valid(self):
        raw = json.loads(fs.SETTINGS_PATH.read_text())
        self.assertTrue(set(fs.DEFAULTS) <= set(raw), "every setting is listed in config/scanner.json")
        settings = fs.load()
        self.assertIn(fs.candle_delay_minutes(settings), (0, 15))
        self.assertTrue(fs.describe(settings).startswith("Scanner settings"))

    def test_defaults_are_off(self):
        self.assertEqual(fs.candle_delay_minutes(fs.DEFAULTS), 0)
        self.assertEqual(fs.chase_kwargs(fs.DEFAULTS), {})
        self.assertIsNone(fs.DEFAULTS["no_breakouts_before"])

    def test_missing_or_broken_file_means_off(self):
        folder = Path(tempfile.mkdtemp())
        self.assertEqual(fs.load(folder / "none.json"), fs.DEFAULTS)
        (folder / "bad.json").write_text("{not json")
        self.assertEqual(fs.load(folder / "bad.json"), fs.DEFAULTS)

    def test_switched_on(self):
        path = Path(tempfile.mkdtemp()) / "s.json"
        path.write_text(json.dumps({"ignore_partial_candles": True, "data_delay_minutes": 15, "chase_limit_pct": 0.5,
                                    "chase_from": "2026-11-02", "no_breakouts_before": "09:00"}))
        settings = fs.load(path)
        self.assertEqual(fs.candle_delay_minutes(settings), 15)
        self.assertEqual(fs.chase_kwargs(settings), {"chase_limit_pct": 0.5, "chase_from": "2026-11-02"})
        self.assertIn("no breakout trades before 09:00", fs.describe(settings))

    def test_chase_needs_both_limit_and_date(self):
        self.assertEqual(fs.chase_kwargs({"chase_limit_pct": 0.5, "chase_from": None}), {})
        self.assertEqual(fs.chase_kwargs({"chase_limit_pct": None, "chase_from": "2026-11-02"}), {})


def candles(prices, start="2026-11-02 08:00"):
    index = pd.date_range(start, periods=len(prices), freq="15min", tz=UK)
    return pd.DataFrame({"open": prices, "high": [p + 0.2 for p in prices], "low": [p - 0.2 for p in prices],
                         "close": prices, "volume": 1000.0}, index=index)


class ChaseCheckTests(unittest.TestCase):
    def setUp(self):
        # 12 flat candles at 100, then the entry candle opens at 101 (1% above the 9 EMA of about 100)
        self.data = candles([100.0] * 12 + [101.0] * 22)
        self.signal = pd.Timestamp("2026-11-02 11:00", tz=UK)
        self.now = pd.Timestamp("2026-11-02 17:00", tz=UK)

    def sim(self, side="Long", **chase):
        stop, target = (99.0, 104.0) if side == "Long" else (103.0, 98.0)
        return simulate(side, stop, target, self.signal, self.data, 1, self.now, "15m", **chase)

    def test_off_by_default(self):
        self.assertEqual(self.sim().status, "done")

    def test_buy_too_far_above_the_ema_is_skipped(self):
        result = self.sim(chase_limit_pct=0.5, chase_from="2026-11-01")
        self.assertEqual((result.status, result.outcome), ("skipped", "chased"))
        self.assertEqual(result.fill, 101.0)

    def test_within_the_limit_is_kept(self):
        self.assertEqual(self.sim(chase_limit_pct=1.5, chase_from="2026-11-01").status, "done")

    def test_only_from_the_start_date(self):
        self.assertEqual(self.sim(chase_limit_pct=0.5, chase_from="2026-11-03").status, "done")

    def test_a_sell_above_the_ema_is_not_chasing(self):
        self.assertEqual(self.sim("Short", chase_limit_pct=0.5, chase_from="2026-11-01").status, "done")

    def test_progress_label(self):
        row = {"signal_time": "2026-11-02 11:00:00", "data_time": "", "direction": "Long", "stop": 99.0,
               "target": 104.0, "entry": 101.0}
        settings = {**fs.DEFAULTS, "chase_limit_pct": 0.5, "chase_from": "2026-11-01"}
        with mock.patch.object(fs, "load", return_value=settings):
            result = lm.mock_progress(row, self.data, now=self.now)
        self.assertEqual(result["status"], "Skipped: entry too far past the 9 EMA")


class NoBreakoutsBeforeTests(unittest.TestCase):
    def setups(self):
        def setup(ticker, kind, score):
            return SimpleNamespace(company=ticker, ticker=ticker, index_label="FTSE 100", setup_type=kind,
                                   direction="Long", quality_score=score, entry=100.0, stop=99.0, target=103.0,
                                   conflict=False, stale=False)
        return [setup("A.L", "Breakout", 90), setup("B.L", "Range", 80)]

    def info(self, clock):
        return SimpleNamespace(day="2026-11-02", run_time=f"2026-11-02 {clock}:03", mode="watch",
                               data_time=pd.Timestamp(f"2026-11-02 08:15", tz=UK))

    def test_breakouts_skipped_before_the_time(self):
        rows = lm.record_live_mock(Path(tempfile.mkdtemp()), self.info("08:32"), self.setups(), no_breakouts_before="09:00")
        self.assertEqual([r["ticker"] for r in rows], ["B.L"])

    def test_breakouts_taken_after_the_time_and_when_off(self):
        rows = lm.record_live_mock(Path(tempfile.mkdtemp()), self.info("09:02"), self.setups(), no_breakouts_before="09:00")
        self.assertEqual(sorted(r["ticker"] for r in rows), ["A.L", "B.L"])
        rows = lm.record_live_mock(Path(tempfile.mkdtemp()), self.info("08:32"), self.setups())
        self.assertEqual(sorted(r["ticker"] for r in rows), ["A.L", "B.L"])


if __name__ == "__main__":
    unittest.main()
