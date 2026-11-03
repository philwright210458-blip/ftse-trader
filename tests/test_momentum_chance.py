import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pandas as pd

import ftse_momentum as mm
import ftse_momentum_chance as mc

DATES = pd.bdate_range("2009-01-01", "2014-12-31")


def prices(drifts, seed=0, noise=0.01):
    rng = np.random.default_rng(seed)
    data = {f"T{n}.L": 100 * np.exp(np.cumsum(d / 252 + rng.normal(0, noise, len(DATES)))) for n, d in enumerate(drifts)}
    close = pd.DataFrame(data, index=DATES)
    return close.copy(), close


MARKET = pd.Series(np.linspace(5000, 7000, len(DATES)), index=DATES)


class ChanceTests(unittest.TestCase):
    def test_random_ranker_keeps_the_same_eligible_shares(self):
        open_, close = prices([0.3, 0.2, 0.1, -0.3, -0.4, 0.0], seed=2)
        close_ff = close.ffill()
        sma = close_ff.rolling(mm.TREND_DAYS, min_periods=mm.TREND_DAYS).mean()
        counts = close.notna().cumsum()
        seen = pd.DataFrame(np.where(close.notna(), DATES.values[:, None], np.datetime64("NaT")),
                            index=DATES, columns=close.columns).ffill()
        real = mm.ranking(close_ff, sma, counts, seen, 900)
        shuffled = mc.random_ranker(np.random.default_rng(1))(close_ff, sma, counts, seen, 900)
        self.assertGreater(len(real), 0)
        self.assertEqual(set(real.index), set(shuffled.index))

    def test_default_model_unchanged_by_hook(self):
        open_, close = prices([0.4] * 5 + [-0.1] * 15)
        a = mm.run_model(open_, close, MARKET, mm.Settings(top=5, keep=10))
        b = mm.run_model(open_, close, MARKET, mm.Settings(top=5, keep=10), rank_fn=mm.ranking)
        pd.testing.assert_series_equal(a.values, b.values)

    def test_sticky_random_trades_less_than_fresh_random(self):
        open_, close = prices([0.1] * 150, seed=9)
        settings = mm.Settings(start="2010-03-01")
        fresh = mm.run_model(open_, close, MARKET, settings, rank_fn=mc.random_ranker(np.random.default_rng(1), 1.0))
        sticky = mm.run_model(open_, close, MARKET, settings, rank_fn=mc.random_ranker(np.random.default_rng(1), 0.05))
        self.assertLess(len(sticky.trades), len(fresh.trades) / 2)

    def test_growth_and_luck(self):
        values = pd.Series([100.0, 121.0], index=pd.to_datetime(["2020-01-01", "2022-01-01"]))
        self.assertAlmostEqual(mc.growth(values, values.index[0], values.index[-1]), 0.1, places=3)
        self.assertAlmostEqual(mc.luck(0.1, np.array([0.0, 0.05, 0.1, 0.2])), 0.5)
        self.assertIsNone(mc.luck(float("nan"), np.array([0.1])))

    def test_strong_momentum_beats_random_portfolios(self):
        open_, close = prices([0.5] * 5 + [0.0] * 60, seed=5)
        row = mc.run_variant("test", {"start": "2010-03-01", "market_filter": True}, open_, close, MARKET, draws=20, seed=1)
        self.assertGreater(row["Model: growth a year"], row["Random portfolios: average growth a year"])
        self.assertLess(row["Random as good: whole"], 0.1)
        self.assertEqual(len(mc.verdict_lines([row])), 1)

    def test_run_writes_a_verdict(self):
        open_, close = prices([0.3] * 4 + [0.0] * 8, seed=7)
        fake = (open_, close, MARKET)
        variants = {"A": ("A. test", {"start": "2010-03-01", "market_filter": True})}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(mc, "load_prices", lambda *a: fake), \
                mock.patch.object(mc, "VARIANTS", variants):
            out = Path(folder, "m.xlsx")
            self.assertEqual(mc.run(SimpleNamespace(universe="", limit=None, draws=5, variants=None, out=str(out))), 0)
            self.assertTrue(out.with_suffix(".json").exists())


if __name__ == "__main__":
    unittest.main()
