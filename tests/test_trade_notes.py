import tempfile
import unittest
from pathlib import Path

import pandas as pd

import ftse_trade_notes as tn

UK = "Europe/London"
DAY = "2026-10-29"


def session(day: str, opens: list[float], start: str = "08:00", volume: float = 1000.0) -> pd.DataFrame:
    """15-minute candles from `start`, each closing where the next opens (range +-1 around the body)."""
    index = pd.date_range(f"{day} {start}", periods=len(opens), freq="15min", tz=UK)
    closes = opens[1:] + [opens[-1]]
    rows = [{"open": o, "high": max(o, c) + 1, "low": min(o, c) - 1, "close": c, "volume": volume}
            for o, c in zip(opens, closes)]
    return pd.DataFrame(rows, index=index)


def flat_history(level: float = 100.0, days: int = 12) -> pd.DataFrame:
    frames = []
    for day in pd.bdate_range(end="2026-10-28", periods=days):
        frames.append(session(str(day.date()), [level] * 34))
    return pd.concat(frames)


def daily_frame(level: float = 100.0, days: int = 60, today_open: float = 100.0, today_close: float = 103.0):
    index = list(pd.bdate_range(end="2026-10-28", periods=days)) + [pd.Timestamp(DAY)]
    rows = [{"open": level, "high": level + 1, "low": level - 1, "close": level} for _ in range(days)]
    rows.append({"open": today_open, "high": 106, "low": 95, "close": today_close})
    return tn.daily_by_date(pd.DataFrame(rows, index=pd.DatetimeIndex(index)))


def trade(direction="Long", stop=98.0, target=104.0, entry=100.5, data_time="2026-10-29T08:15:00+00:00",
          signal_time="2026-10-29 08:32:59"):
    return {"key": f"k|{direction}", "day": DAY, "trade_no": 1, "ticker": "A.L", "company": "Alpha",
            "setup": "Breakout", "direction": direction, "score": 60.0, "entry": entry, "stop": stop,
            "target": target, "data_time": data_time, "signal_time": signal_time}


def outcome(fill=100.0, exit_price=98.0, exit_time="2026-10-29T10:00:00+00:00", result="stop"):
    return {"fill": fill, "exit_price": exit_price, "exit_time": exit_time, "outcome": result}


class NoteTradeTests(unittest.TestCase):
    def setUp(self):
        # Today: 08:00 dips to 97 (below a 98 stop), then sits at 100 until the close
        today = session(DAY, [100.0] * 34)
        today.iloc[0, today.columns.get_loc("low")] = 97.0
        self.m15 = pd.concat([flat_history(), today])
        self.daily = daily_frame()

    def test_long_with_stop_inside_the_morning_range(self):
        row = tn.note_trade(trade(), outcome(), self.m15, self.daily, pd.Series(dtype=float), pd.DataFrame(), None)
        self.assertEqual(row["fill_time"], "08:45")
        self.assertEqual(row["stop_inside_range"], "Yes")
        self.assertEqual(row["trend_15m"], "Flat")
        self.assertEqual(row["minutes_held"], 75)
        self.assertAlmostEqual(row["net_gbp"], -2.10)                # -2% then 10p costs
        self.assertAlmostEqual(row["hold_to_close_gbp"], 2.90)       # daily close 103
        self.assertEqual(row["picker_view"], "No picker file")
        self.assertEqual(row["daily_trend"], "Down")                 # 100 is not above its own 50-day average
        self.assertEqual(row["trend_4h"], "")                        # not enough 4-hour bars
        self.assertAlmostEqual(row["planned_stop_pct"], 2.49)
        self.assertAlmostEqual(row["fill_stop_pct"], 2.0)
        self.assertAlmostEqual(row["signal_volume_ratio"], 1.0)

    def test_stop_not_inside_when_morning_stayed_clear(self):
        row = tn.note_trade(trade(stop=96.0), outcome(exit_price=96.0), self.m15, self.daily,
                            pd.Series(dtype=float), pd.DataFrame(), None)
        self.assertEqual(row["stop_inside_range"], "No")

    def test_short_best_and_worst(self):
        row = tn.note_trade(trade("Short", stop=102.0, target=96.0, entry=100.0),
                            outcome(exit_price=100.0, exit_time="2026-10-29T16:30:00+00:00", result="timeout"),
                            self.m15, self.daily, pd.Series(dtype=float), pd.DataFrame(), {})
        self.assertAlmostEqual(row["best_pct"], 1.0)                 # lows of 99
        self.assertAlmostEqual(row["worst_pct"], 1.0)                # highs of 101
        self.assertAlmostEqual(row["hold_to_close_gbp"], -3.10)
        self.assertEqual(row["picker_view"], "No view")

    def test_picker_and_flip_flop(self):
        setups = pd.DataFrame([
            {"ticker": "A.L", "direction": "Short", "run_time": "2026-10-29 09:02:08"},   # within the hour
            {"ticker": "A.L", "direction": "Short", "run_time": "2026-10-29 11:02:08"},   # too late
            {"ticker": "B.L", "direction": "Short", "run_time": "2026-10-29 08:47:00"},   # other share
            {"ticker": "A.L", "direction": "Long", "run_time": "2026-10-29 08:47:00"}])   # same way
        row = tn.note_trade(trade(), outcome(), self.m15, self.daily, pd.Series(dtype=float), setups,
                            {"A.L": ("Avoid", "Results today")})
        self.assertEqual(row["opposite_ideas_1h"], 1)
        self.assertEqual(row["flip_flop"], "Yes")
        self.assertEqual((row["picker_view"], row["picker_reason"]), ("Avoid", "Results today"))

    def test_up_trend_and_trends_agreeing(self):
        rising = flat_history()
        rising["close"] = [100 + 0.05 * i for i in range(len(rising))]
        rising["open"] = rising["close"]
        rising["high"], rising["low"] = rising["close"] + 1, rising["close"] - 1
        level = float(rising["close"].iloc[-1])
        m15 = pd.concat([rising, session(DAY, [level] * 34)])
        closes = pd.Series([90.0] * 200, index=pd.date_range("2026-01-01", periods=200, freq="D", tz=UK))
        row = tn.note_trade(trade(stop=level - 3, target=level + 5, entry=level),
                            outcome(fill=level, exit_price=level + 5, result="target"),
                            m15, self.daily, closes, pd.DataFrame(), None)
        self.assertEqual(row["trend_15m"], "Up")
        self.assertEqual(row["trend_4h"], "Up")
        self.assertEqual(row["trends_agreeing"], 2)                  # daily is Down (flat 100s v 50-day average)

    def test_missing_candles_give_none(self):
        self.assertIsNone(tn.note_trade(trade(), outcome(), flat_history(), self.daily,
                                        pd.Series(dtype=float), pd.DataFrame(), None))


class FourHourTests(unittest.TestCase):
    def test_blocks_from_the_open(self):
        index = pd.date_range(f"{DAY} 08:00", periods=9, freq="h", tz=UK)
        hourly = pd.DataFrame({"close": range(1, 10)}, index=index)
        closes = tn.four_hour_closes(hourly)
        self.assertEqual([t.strftime("%H:%M") for t in closes.index], ["12:00", "16:00", "17:00"])
        self.assertEqual(list(closes), [4.0, 8.0, 9.0])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        t = trade()
        pd.DataFrame([{**t, "mode": "watch", "index": "FTSE 100", "risk_pct": 2.5}]).to_csv(
            self.dir / "live_mock_trades.csv", index=False)
        o = outcome()
        pd.DataFrame([{"key": t["key"], "day": DAY, "ticker": "A.L", "direction": "Long", "horizon": 1,
                       "status": "done", "outcome": o["outcome"], "fill": o["fill"], "exit_price": o["exit_price"],
                       "exit_time": o["exit_time"], "r_gross": -1, "fill_risk_pct": 2, "ambiguous": False}]).to_csv(
            self.dir / "outcomes.v3.csv", index=False)
        today = session(DAY, [100.0] * 34)
        self.calls = 0

        def fetch(tickers):
            self.calls += 1
            return {"A.L": pd.concat([flat_history(), today])}, {"A.L": daily_frame()}, {}
        self.fetch = fetch
        self.now = pd.Timestamp(f"{DAY} 17:10", tz=UK)

    def test_writes_once_then_skips(self):
        self.assertEqual(tn.run(self.dir, False, self.now, self.fetch), 0)
        notes = pd.read_csv(self.dir / tn.NOTES_FILE)
        self.assertEqual(len(notes), 1)
        self.assertEqual(list(notes.columns), tn.COLUMNS)
        self.assertEqual(tn.run(self.dir, False, self.now, self.fetch), 0)
        self.assertEqual(self.calls, 1)                              # nothing new: no download
        self.assertEqual(len(pd.read_csv(self.dir / tn.NOTES_FILE)), 1)

    def test_rebuild_replaces_without_duplicating(self):
        tn.run(self.dir, False, self.now, self.fetch)
        tn.run(self.dir, True, self.now, self.fetch)
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(pd.read_csv(self.dir / tn.NOTES_FILE)), 1)

    def test_old_trades_are_out_of_reach(self):
        later = self.now + pd.Timedelta(days=tn.MAX_AGE_DAYS + 1)
        self.assertEqual(tn.run(self.dir, False, later, self.fetch), 0)
        self.assertEqual(self.calls, 0)


if __name__ == "__main__":
    unittest.main()
