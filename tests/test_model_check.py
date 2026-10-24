"""Model check backtest (24 Oct 2026): 4-hour SMA, three-candle strategy, live-trader slots."""

import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import ftse_model_check_backtest as mc

TZ = "Europe/London"


def bars(day, rows, start="08:00", freq="15min"):
    index = pd.date_range(f"{day} {start}", periods=len(rows), freq=freq, tz=TZ)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=index)


class FourHour(unittest.TestCase):
    def test_blocks_end_times_and_closes(self):
        hourly = bars("2026-10-20", [(100, 101, 99, 100 + i) for i in range(9)], freq="1h")   # 08:00 ... 16:00
        closes = mc.four_hour_closes(hourly, "08:00")
        self.assertEqual(list(closes.index.strftime("%H:%M")), ["12:00", "16:00", "17:00"])
        self.assertEqual(list(closes.values), [103.0, 107.0, 108.0])
        self.assertIsNone(mc.sma_at(closes, pd.Timestamp("2026-10-20 13:00", tz=TZ), bars=2))
        self.assertAlmostEqual(mc.sma_at(closes, pd.Timestamp("2026-10-20 16:00", tz=TZ), bars=2), 105.0)


class ThreeCandle(unittest.TestCase):
    def closes(self, level):
        index = pd.date_range("2026-01-01", periods=250, freq="4h", tz=TZ)
        return pd.Series(level, index=index)

    def test_buy_after_dip_below_candle_one_hits_target(self):
        day = bars("2026-10-20", [(100, 105, 98, 101),     # Candle 1: range 98-105
                                  (101, 102, 96, 99),      # Candle 2: dips to 96, closes back inside at 99
                                  (99, 103, 98.5, 102.5),  # Candle 3: breaks 102 -> buy at 102
                                  (102.5, 105.5, 102, 105)])  # target 105 reached
        trades = mc.three_candle(day, self.closes(90.0), "08:00", "16:30", 1.0)
        t = trades.iloc[0]
        self.assertEqual((t["direction"], t["entry"], t["stop"], t["target"], t["result"]), ("Buy", 102, 95, 105, "Target hit"))
        self.assertEqual((t["points"], t["net_points"], t["risk_points"]), (3, 2, 7))

    def test_no_trade_against_the_trend_or_without_close_back_inside(self):
        day = bars("2026-10-20", [(100, 105, 98, 101), (101, 102, 96, 99), (99, 103, 98.5, 102.5), (102, 103, 101, 102)])
        self.assertFalse(mc.three_candle(day, self.closes(110.0), "08:00", "16:30", 1.0).iloc[0]["taken"])
        outside = bars("2026-10-20", [(100, 105, 98, 101), (101, 102, 96, 97), (97, 103, 96.5, 102), (102, 103, 101, 102)])
        self.assertFalse(mc.three_candle(outside, self.closes(90.0), "08:00", "16:30", 1.0).iloc[0]["taken"])

    def test_sell_stopped_counts_stop_first(self):
        day = bars("2026-10-20", [(100, 102, 95, 99),       # range 95-102
                                  (99, 104, 98, 100),       # pokes above to 104, closes inside
                                  (100, 106, 97, 97.5),     # breaks 98 (sell) but also reaches the 105 stop: stop first
                                  (97, 98, 94, 95)])
        t = mc.three_candle(day, self.closes(110.0), "08:00", "16:30", 1.0).iloc[0]
        self.assertEqual((t["direction"], t["result"], t["exit"]), ("Sell", "Stopped out", 105))


class Slots(unittest.TestCase):
    def test_five_open_then_a_closed_trade_frees_a_slot(self):
        rows = [{"day": "2026-10-20", "run_time": "2026-10-20 09:02:00", "ticker": f"S{i}.L", "score": 90 - i,
                 "direction": "Short"} for i in range(7)]
        rows += [{"day": "2026-10-20", "run_time": "2026-10-20 11:02:00", "ticker": "Z.L", "score": 99, "direction": "Short"}]
        exits = {"S0.L": "2026-10-20T10:00:00+01:00"}

        def fake(row, intraday, now):
            return {"status": "done", "outcome": "stop", "fill": 100, "r_gross": -1, "fill_risk_pct": 1,
                    "exit_time": exits.get(row["ticker"], "2026-10-20T16:30:00+01:00")}
        with patch.object(mc, "simulate_one", side_effect=fake):
            taken = mc.live_trader(pd.DataFrame(rows), {}, pd.Timestamp("2026-10-21", tz=TZ))
        self.assertEqual(list(taken["ticker"]), ["S0.L", "S1.L", "S2.L", "S3.L", "S4.L", "Z.L"])


if __name__ == "__main__":
    unittest.main()


class EndToEnd(unittest.TestCase):
    def test_run_on_synthetic_prices(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        import ftse_trade_scanner as sc
        rng = np.random.default_rng(5)

        def series(freq, periods_per_day, days, start="08:00"):
            parts = []
            for day in pd.bdate_range("2026-06-01", periods=days):
                index = pd.date_range(f"{day.date()} {start}", periods=periods_per_day, freq=freq, tz=sc.UK_TZ)
                walk = 100 + np.cumsum(rng.normal(0, 0.4, periods_per_day)) + 3 * np.sin(np.arange(periods_per_day) / 3)
                parts.append(pd.DataFrame({"open": walk, "high": walk + 0.4, "low": walk - 0.4, "close": walk + 0.1,
                                           "volume": 1000}, index=index))
            return pd.concat(parts)
        m15 = {t: series("15min", 34, 110)[-34 * 16:] for t in ("A.L", "B.L")}
        h1 = {t: series("1h", 9, 110) for t in ("A.L", "B.L")}
        universe = [SimpleNamespace(company=t, yahoo_ticker=t, index="FTSE 100", kind="company") for t in m15]
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(sc, "load_universe", return_value=universe), \
                patch.object(sc, "fetch_many", side_effect=lambda t, i, p: m15 if i == "15m" else h1), \
                patch.object(sc, "fetch_data", side_effect=lambda t, i, p: series("15min", 34, 60) if i == "15m" else series("1h", 9, 110)), \
                patch.object(sc, "completed_candles", side_effect=lambda f, *a: f), \
                patch.object(sc, "drop_untraded_bars", side_effect=lambda f: f):
            args = SimpleNamespace(universe="x", warmup_days=12, limit=None, workers=1, out=str(Path(folder) / "m.xlsx"))
            self.assertEqual(mc.run(args), 0)
            self.assertTrue((Path(folder) / "m.xlsx").exists())
