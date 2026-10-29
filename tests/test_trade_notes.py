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
        self.assertEqual(row["stop_pretested"], "Yes")
        self.assertEqual(row["ema_state"], "Flat")
        self.assertEqual(row["minutes_held"], 75)
        self.assertAlmostEqual(row["net_gbp"], -2.10)                # -2% then 10p costs
        self.assertAlmostEqual(row["close_outcome_gbp"], 2.90)       # daily close 103
        self.assertEqual(row["picker_view"], "No picker file")
        self.assertEqual(row["daily_trend"], "Down")                 # 100 is not above its own 50-day average
        self.assertEqual(row["trend_4h"], "")                        # not enough 4-hour bars
        self.assertAlmostEqual(row["planned_stop_pct"], 2.49)
        self.assertAlmostEqual(row["fill_stop_pct"], 2.0)
        self.assertAlmostEqual(row["signal_volume_ratio"], 1.0)

    def test_stop_not_inside_when_morning_stayed_clear(self):
        row = tn.note_trade(trade(stop=96.0), outcome(exit_price=96.0), self.m15, self.daily,
                            pd.Series(dtype=float), pd.DataFrame(), None)
        self.assertEqual(row["stop_pretested"], "No")

    def test_short_best_and_worst(self):
        row = tn.note_trade(trade("Short", stop=102.0, target=96.0, entry=100.0),
                            outcome(exit_price=100.0, exit_time="2026-10-29T16:30:00+00:00", result="timeout"),
                            self.m15, self.daily, pd.Series(dtype=float), pd.DataFrame(), {})
        self.assertAlmostEqual(row["mfe_pct"], 1.0)                 # lows of 99
        self.assertAlmostEqual(row["mae_pct"], 1.0)                # highs of 101
        self.assertAlmostEqual(row["close_outcome_gbp"], -3.10)
        self.assertEqual(row["picker_view"], "No view")

    def test_picker_and_flip_flop(self):
        setups = pd.DataFrame([
            {"ticker": "A.L", "direction": "Short", "run_time": "2026-10-29 08:02:08"},   # within the hour before
            {"ticker": "A.L", "direction": "Short", "run_time": "2026-10-29 09:02:08"},   # after the signal: not known yet
            {"ticker": "A.L", "direction": "Short", "run_time": "2026-10-29 07:02:08"},   # too early
            {"ticker": "B.L", "direction": "Short", "run_time": "2026-10-29 08:47:00"},   # other share
            {"ticker": "A.L", "direction": "Long", "run_time": "2026-10-29 08:47:00"}])   # same way
        row = tn.note_trade(trade(), outcome(), self.m15, self.daily, pd.Series(dtype=float), setups,
                            {"A.L": ("Avoid", "Results today")})
        self.assertEqual(row["flip_count_60m"], 1)
        self.assertEqual(row["regime"], "Sideways")                  # flat and flip-flopping
        self.assertEqual((row["picker_view"], row["picker_reason"]), ("Avoid", "Results today"))
        self.assertEqual(row["news_day"], "Yes")

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
        self.assertEqual(row["ema_state"], "Up")
        self.assertEqual(row["trend_4h"], "Up")
        self.assertEqual(row["trends_agreeing"], 2)                  # daily is Down (flat 100s v 50-day average)

    def test_missing_candles_give_none(self):
        self.assertIsNone(tn.note_trade(trade(), outcome(), flat_history(), self.daily,
                                        pd.Series(dtype=float), pd.DataFrame(), None))


class FeasibilityTests(unittest.TestCase):
    def setUp(self):
        today = session(DAY, [100.0] * 34)
        today.iloc[0, today.columns.get_loc("low")] = 97.0
        self.m15 = pd.concat([flat_history(), today])
        self.daily = daily_frame()

    def note(self, t, o, setups=None, views=None, m15=None):
        return tn.note_trade(t, o, self.m15 if m15 is None else m15, self.daily, pd.Series(dtype=float),
                             pd.DataFrame() if setups is None else setups, views)

    def setup_row(self, low, high, setup="Breakout", direction="Long"):
        return pd.DataFrame([{"ticker": "A.L", "direction": direction, "setup": setup,
                              "run_time": "2026-10-29 08:32:59", "zone_low": low, "zone_high": high}])

    def test_room_band(self):
        row = self.note(trade(entry=102.0), outcome())            # planned 4.0 away, 2.0 left after the fill
        self.assertEqual(row["fill_room_pct"], 51)
        self.assertEqual(row["fill_room_band"], "50-75%")
        self.assertEqual([tn.room_band(v) for v in (95, 80, 60, 30, None)],
                         ["90%+", "75-90%", "50-75%", "under 50%", ""])

    def test_breakout_still_past_the_zone(self):
        row = self.note(trade(), outcome(), self.setup_row(98.5, 99.5))
        self.assertEqual((row["fill_vs_zone"], row["signal_intact"]), ("Beyond", "Yes"))
        self.assertAlmostEqual(row["fill_zone_margin_pct"], 0.5)
        self.assertEqual((row["zone_low"], row["zone_high"]), (98.5, 99.5))

    def test_breakout_back_inside_the_zone(self):
        row = self.note(trade(), outcome(), self.setup_row(99.5, 100.5))
        self.assertEqual((row["fill_vs_zone"], row["signal_intact"]), ("Inside", "No"))

    def test_range_inside_is_fine_but_through_is_not(self):
        ranged = {**trade(), "setup": "Range"}
        inside = self.note(ranged, outcome(), self.setup_row(99.5, 100.5, "Range"))
        through = self.note(ranged, outcome(), self.setup_row(100.5, 101.0, "Range"))
        self.assertEqual(inside["signal_intact"], "Yes")
        self.assertEqual((through["fill_vs_zone"], through["signal_intact"]), ("Through", "No"))

    def test_short_zone_sides(self):
        self.assertEqual(tn.zone_position("Short", 99.0, 99.5, 100.0)[0], "Beyond")
        self.assertEqual(tn.zone_position("Short", 101.0, 99.5, 100.0)[0], "Through")
        self.assertEqual(tn.zone_position("Long", 100.0, None, None), ("", None))

    def test_no_zone_found(self):
        row = self.note(trade(), outcome())
        self.assertEqual((row["fill_vs_zone"], row["signal_intact"]), ("", ""))

    def test_news_day_from_picker_reason(self):
        self.assertEqual(self.note(trade(), outcome(), views={"A.L": ("Buy", "Strong US lead")})["news_day"], "No")
        self.assertEqual(self.note(trade(), outcome(), views={"A.L": ("Avoid", "Trading update out")})["news_day"], "Yes")
        self.assertEqual(self.note(trade(), outcome(), views=None)["news_day"], "")

    def test_regime_labels(self):
        self.assertEqual(tn.regime_label("Flat", 0, 0.5, -0.3, "Up", "Down"), "Sideways")
        self.assertEqual(tn.regime_label("Flat", 0, 3.0, -0.3, "Up", "Down"), "Mixed")
        self.assertEqual(tn.regime_label("Down", 0, 5.0, -7.0, "Up", "Down"), "Trending")
        self.assertEqual(tn.regime_label("Up", 0, -2.0, -1.5, "Down", "Down"), "Mixed")

    def test_stop_by_a_wick_on_a_volume_spike(self):
        today = session(DAY, [100.0] * 34)
        stop_bar = today.index[7]                                    # 09:45
        today.loc[stop_bar, ["low", "volume"]] = [97.5, 9000.0]       # one print at 97.5, then back to 100
        m15 = pd.concat([flat_history(), today])
        row = self.note(trade(), outcome(exit_price=98.0), m15=m15)
        self.assertIn("stop hit only by a wick that closed back (09:45) on a volume spike (9x)", row["data_quality_flag"])

    def test_extreme_candle_flag(self):
        today = session(DAY, [100.0] * 34)
        today.loc[today.index[4], "high"] = 110.0                   # 09:00, 11 points against a normal 2
        m15 = pd.concat([flat_history(), today])
        row = self.note(trade(), outcome(exit_price=104.0, result="target"), m15=m15)
        self.assertIn("extreme wick 09:00", row["data_quality_flag"])


class ZoneAgeTests(unittest.TestCase):
    def wave(self):
        index = pd.date_range(f"{DAY} 08:00", periods=34, freq="15min", tz=UK)
        prices = [100 + abs((i % 10) - 5) / 5 for i in range(34)]          # lows of 100 at 09:15, 11:45, 14:15
        frame = pd.DataFrame({"open": prices, "close": prices, "high": [p + 0.05 for p in prices],
                              "low": [p - 0.05 for p in prices], "volume": 1000.0}, index=index)
        return pd.concat([flat_history(level=110.0), frame])

    def test_zone_found_with_first_touch_and_count(self):
        frame = self.wave()
        first, touches = tn.zone_history(frame, pd.Timestamp(f"{DAY} 16:00", tz=UK), 99.95, 99.95)
        self.assertEqual((first.strftime("%H:%M"), touches), ("09:15", 3))

    def test_unmatched_zone(self):
        self.assertEqual(tn.zone_history(self.wave(), pd.Timestamp(f"{DAY} 16:00", tz=UK), 95.0, 96.0), (None, 0))
        self.assertEqual(tn.zone_history(self.wave(), pd.Timestamp(f"{DAY} 16:00", tz=UK), None, None), (None, 0))

    def test_age_in_trading_minutes(self):
        frame = self.wave()
        t = trade(stop=99.0, target=102.0, entry=100.2, data_time="2026-10-29T15:15:00+00:00",
                  signal_time="2026-10-29 15:32:00")
        setups = pd.DataFrame([{"ticker": "A.L", "direction": "Long", "setup": "Breakout", "run_time": t["signal_time"],
                                "zone_low": 99.95, "zone_high": 99.95}])
        row = tn.note_trade(t, outcome(fill=100.2, exit_price=99.0, exit_time="2026-10-29T16:15:00+00:00"),
                            frame, daily_frame(level=110.0), pd.Series(dtype=float), setups, None)
        self.assertEqual(row["zone_touches"], 3)
        self.assertEqual(row["zone_formed_today"], "Yes")
        self.assertEqual(row["zone_age_minutes"], 25 * 15)                  # 09:15 to the 15:15 candle, inclusive


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

    def test_old_layout_is_rebuilt(self):
        pd.DataFrame([{"key": "k|Long", "day": DAY, "stop_inside_range": "Yes"}]).to_csv(self.dir / tn.NOTES_FILE, index=False)
        tn.run(self.dir, False, self.now, self.fetch)
        notes = pd.read_csv(self.dir / tn.NOTES_FILE)
        self.assertEqual(list(notes.columns), tn.COLUMNS)
        self.assertEqual(len(notes), 1)

    def test_skipped_trade_gets_a_counterfactual(self):
        t = {**trade(), "key": "k|skip"}
        pd.DataFrame([{**trade(), "mode": "watch", "index": "FTSE 100", "risk_pct": 2.5},
                      {**t, "mode": "watch", "index": "FTSE 100", "risk_pct": 2.5}]).to_csv(
            self.dir / "live_mock_trades.csv", index=False)
        outcomes = pd.read_csv(self.dir / "outcomes.v3.csv")
        skip = outcomes.iloc[0].copy()
        skip["key"], skip["status"], skip["outcome"], skip["exit_price"], skip["exit_time"] = "k|skip", "skipped", "thin_stop", None, None
        pd.concat([outcomes, skip.to_frame().T]).to_csv(self.dir / "outcomes.v3.csv", index=False)
        tn.run(self.dir, False, self.now, self.fetch)
        notes = pd.read_csv(self.dir / tn.NOTES_FILE).set_index("key")
        self.assertEqual(notes.loc["k|Long", "entered"], "Yes")
        self.assertEqual(notes.loc["k|skip", "entered"], "No: fill check")
        self.assertTrue(pd.isna(notes.loc["k|skip", "net_gbp"]))
        self.assertEqual(notes.loc["k|skip", "counterfactual_result"], "timeout")   # flat day: closed at 16:30
        self.assertAlmostEqual(notes.loc["k|skip", "counterfactual_gbp"], -0.10)

    def test_old_trades_are_out_of_reach(self):
        later = self.now + pd.Timedelta(days=tn.MAX_AGE_DAYS + 1)
        self.assertEqual(tn.run(self.dir, False, later, self.fetch), 0)
        self.assertEqual(self.calls, 0)


if __name__ == "__main__":
    unittest.main()
