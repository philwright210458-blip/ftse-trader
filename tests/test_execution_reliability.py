"""Regressions for the October 2026 reliability review; no market access."""

import unittest
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import ftse_live_mock as live
import ftse_model_check_backtest as model
import ftse_momentum_live as momentum
import ftse_outcomes as outcomes
import ftse_structure as structure_model
import ftse_structure_live as structure
from tests.structure_fixture import textbook
from tests.test_momentum_live import market_until, NAMES, TICKERS, at


def stamp(value):
    return pd.Timestamp(value, tz="Europe/London")


def candles(start, rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"],
                        index=pd.date_range(stamp(start), periods=len(rows), freq="15min"))


class ReviewRegressions(unittest.TestCase):
    def test_missing_entry_bar_cannot_report_a_win_or_release_a_slot(self):
        row = dict(signal_time="2026-10-21 10:03:48", direction="Long", stop=99., target=103.)
        data = candles("2026-10-21 10:30", [(100., 104., 99.5, 103.)])
        progress = live.mock_progress(row, data)
        final = outcomes.simulate("Long", 99., 103., stamp(row["signal_time"]), data, 1,
                                  stamp("2026-10-21 11:00"))
        self.assertEqual(final.status, "pending")
        self.assertEqual(live.still_open([{"progress": progress}]), 1)
        self.assertIsNone(progress["net"])

    def test_backtest_reserves_slots_before_future_gap_rejections(self):
        rows = [dict(day="2026-10-21", run_time="2026-10-21 10:03:48", ticker=f"S{i}.L", score=100-i)
                for i in range(7)]
        def result(row, intraday, now):
            return {"status": "skipped", "outcome": "gap", "exit_time": ""}
        with patch.object(model, "simulate_one", side_effect=result):
            taken = model.live_trader(pd.DataFrame(rows), {}, stamp("2026-10-22"))
        self.assertEqual(list(taken["ticker"]), [f"S{i}.L" for i in range(5)])

    def test_missing_share_recovers_its_own_structure_signal(self):
        frame = textbook()
        day = frame.index[structure_model.signals(frame)[0]["t"]]
        empty = pd.DataFrame(columns=structure.COLUMNS)
        with patch.object(structure, "FIRST_SIGNAL_DAY", str(frame.index[0].date())):
            trades, checked = structure.step(empty, {"A.L": frame}, {}, {}, None, day, "first run")
            trades, checked = structure.step(trades, {"A.L": frame, "B.L": frame}, {}, {}, checked,
                                             frame.index[-1], "recovery")
            again, _ = structure.step(trades, {"A.L": frame, "B.L": frame}, {}, {}, checked,
                                      frame.index[-1], "rerun")
        self.assertEqual(set(trades["ticker"]), {"A.L", "B.L"})
        self.assertEqual(len(again), 2)

    def test_momentum_requires_the_original_sessions_observed_open(self):
        market = momentum.Market.__new__(momentum.Market)
        days = pd.to_datetime(["2026-10-30", "2026-11-02"])
        market.open = pd.DataFrame({"A.L": [100., float("nan")]}, index=days)
        market.close_ff = pd.DataFrame({"A.L": [100., 100.]}, index=days)
        self.assertIsNone(market.open_price("A.L", days[-1]))


class ExecutionClocks(unittest.TestCase):
    def test_future_bars_and_gap_rejection_are_invisible_until_observed(self):
        data = candles("2026-10-21 10:15", [(104., 105., 103.5, 104.)])
        row = dict(signal_time="2026-10-21 10:03:48", direction="Long", stop=99., target=103.)
        before = live.mock_progress(row, data, now=stamp("2026-10-21 10:29"))
        after = live.mock_progress(row, data, now=stamp("2026-10-21 10:30"))
        self.assertEqual(live.still_open([{"progress": before}]), 1)
        self.assertEqual(live.still_open([{"progress": after}]), 0)
        self.assertTrue(after["status"].startswith("Skipped"))

    def test_missing_entry_does_not_become_a_rejection_when_later_data_arrives(self):
        data = candles("2026-10-21 11:00", [(100., 104., 99.5, 103.)])
        sim = outcomes.simulate("Long", 99., 103., stamp("2026-10-21 10:03"), data, 1,
                                stamp("2026-10-21 12:00"))
        self.assertEqual((sim.status, sim.state), ("pending", "missing"))

    def test_early_close_uses_the_exchange_calendar(self):
        data = candles("2026-12-24 12:00", [(100., 100.5, 99.5, 100.)] * 2)
        row = dict(signal_time="2026-12-24 11:59:00", direction="Long", stop=99., target=103.)
        progress = live.mock_progress(row, data, now=stamp("2026-12-24 12:30"))
        final = outcomes.simulate("Long", 99., 103., stamp(row["signal_time"]), data, 1,
                                  stamp("2026-12-24 12:30"))
        self.assertEqual((progress["status"], progress["time"]), ("Closed at end of day", "12:30"))
        self.assertEqual(final.exit_price, progress["price"])

    def test_daily_range_cannot_bridge_a_gap_before_the_close(self):
        data = candles("2026-10-21 10:15", [(100., 100.5, 99.5, 100.)] * 4).drop(stamp("2026-10-21 10:30"))
        daily = pd.DataFrame({"high": [100.5], "low": [99.5], "close": [100.]},
                             index=pd.to_datetime(["2026-10-21"]))
        sim = outcomes.simulate("Long", 99., 103., stamp("2026-10-21 10:03"), data, 1,
                                stamp("2026-10-21 11:15"), daily=daily)
        self.assertEqual(sim.state, "missing")

    def test_backtest_and_live_selection_agree_at_each_scan(self):
        import ftse_trade_scanner as scanner
        from tests.test_live_mock import setup
        # First five decisions all gap beyond their targets. The next scan is
        # before those entry candles finish; the third can reuse the five slots.
        prices = {f"S{i}.L": candles("2026-10-21 10:15", [(104., 105., 103.5, 104.)] * 4)
                  for i in range(15)}
        scans = ["2026-10-21 10:03:00", "2026-10-21 10:20:00", "2026-10-21 10:31:00"]
        ideas, live_tickers = [], []
        with tempfile.TemporaryDirectory() as folder:
            for n, clock in enumerate(scans):
                rows = [dict(day="2026-10-21", run_time=clock, data_time="2026-10-21T10:00:00+01:00",
                             ticker=f"S{i}.L", score=100-i, direction="Long", stop=99., target=103.)
                        for i in range(n*5, (n+1)*5)]
                ideas.extend(rows)
                existing = live.read_live(Path(folder) / live.LIVE_FILENAME).to_dict("records")
                progress = [{"progress": live.mock_progress(r, prices[r["ticker"]], now=stamp(clock))}
                            for r in existing]
                info = scanner.RunInfo(stamp("2026-10-21 10:00"), "10:15", "2026-10-21", clock, "watch", 15, 0)
                added = live.record_live_mock(Path(folder), info, [setup(r["ticker"], r["score"]) for r in rows],
                                              max_open=5, open_now=live.still_open(progress))
                live_tickers.extend(r["ticker"] for r in added)
        backtest = model.live_trader(pd.DataFrame(ideas), prices, stamp("2026-10-21 17:00"))
        self.assertEqual(list(backtest["ticker"]), live_tickers)
        self.assertEqual(live_tickers, [f"S{i}.L" for i in list(range(5)) + list(range(10, 15))])


class StructureRecovery(unittest.TestCase):
    def test_missing_session_stops_progress_and_recovery_matches_clean_run(self):
        frame = textbook()
        signal = structure_model.signals(frame)[0]
        signal_day = frame.index[signal["t"]]
        missing = frame.drop(signal_day)
        empty = pd.DataFrame(columns=structure.COLUMNS)
        with patch.object(structure, "FIRST_SIGNAL_DAY", str(signal_day.date())):
            trades, checked = structure.step(empty, {"A.L": missing}, {}, {}, None, frame.index[-1], "first")
            self.assertNotIn("A.L", checked)
            recovered, _ = structure.step(trades, {"A.L": frame}, {}, {}, checked, frame.index[-1], "recovered")
            clean, _ = structure.step(empty, {"A.L": frame}, {}, {}, None, frame.index[-1], "clean")
        fields = ["signal_day", "fill_day", "fill", "exit_measured", "net_measured", "net_2x"]
        pd.testing.assert_frame_equal(recovered[fields], clean[fields])
        self.assertEqual(recovered.iloc[0]["recording"], "Recovered after missing data")

    def test_multiple_signals_after_closed_trades_match_daily_runs(self):
        days = pd.bdate_range("2026-10-19", periods=5)
        frame = pd.DataFrame({"open": 100., "high": 104., "low": 99.5, "close": 103.}, index=days)
        signals = [dict(t=i, L=95., H=105., P=99., depth=.5, stop=99., target=103.) for i in (0, 2)]
        def found(visible):
            return [s for s in signals if s["t"] < len(visible)]
        empty = pd.DataFrame(columns=structure.COLUMNS)
        with patch.object(structure, "FIRST_SIGNAL_DAY", str(days[0].date())), patch.object(structure.st, "signals", side_effect=found):
            caught_up, _ = structure.step(empty, {"A.L": frame}, {}, {}, None, days[-1], "catch up")
            daily, checked = empty, None
            for day in days:
                daily, checked = structure.step(daily, {"A.L": frame}, {}, {}, checked, day, "daily")
        fields = ["signal_day", "fill_day", "fill", "net_measured", "net_2x"]
        self.assertEqual(len(caught_up), 2)
        pd.testing.assert_frame_equal(caught_up[fields], daily[fields])

    def test_legacy_state_is_backed_up_and_does_not_hide_missing_shares(self):
        frame = textbook()
        signal_day = frame.index[structure_model.signals(frame)[0]["t"]]
        with tempfile.TemporaryDirectory() as folder, patch.object(structure, "FIRST_SIGNAL_DAY", str(signal_day.date())):
            folder = Path(folder)
            universe = folder / "universe.csv"
            universe.write_text("company,epic,yahoo_ticker,index,type\nAlpha,A,A.L,FTSE 100,company\n")
            state = json.dumps({"checked_through": str(frame.index[-1].date())})
            (folder / "state.json").write_text(state)
            pd.DataFrame(columns=structure.COLUMNS[:-1]).to_csv(folder / "trades.csv", index=False)
            original = (folder / "trades.csv").read_bytes()
            args = SimpleNamespace(universe=str(universe), period="2y", data_dir=str(folder), out=str(folder / "report.xlsx"))
            now = stamp(str(frame.index[-1].date()) + " 17:05")
            with patch.object(structure.db, "download", return_value={"A.L": frame}):
                structure.run(args, now)
                structure.run(args, now)
            self.assertEqual((folder / "trades.pre-reliability.csv").read_bytes(), original)
            self.assertEqual((folder / "state.pre-reliability.json").read_text(), state)
            self.assertEqual(len(pd.read_csv(folder / "trades.csv")), 1)
            self.assertEqual(json.loads((folder / "state.json").read_text())["schema_version"], 2)


class MomentumRecovery(unittest.TestCase):
    def test_missing_buy_open_reserves_cash_then_fills_original_session_once(self):
        orders = momentum.step(pd.DataFrame(columns=momentum.ORDER_COLUMNS), market_until("2026-10-30"), NAMES,
                               at("2026-10-30 17:05"))
        ticker = orders.loc[orders["action"] == "Buy", "ticker"].iloc[0]
        market = market_until("2026-11-03")
        original_open = market.open.at[pd.Timestamp("2026-11-02"), ticker]
        market.open.at[pd.Timestamp("2026-11-02"), ticker] = float("nan")
        waiting = momentum.step(orders, market, NAMES, at("2026-11-03 17:05"))
        self.assertEqual(momentum.cash_now(waiting), 1000.)
        self.assertFalse((waiting["status"] == "filled").any())
        self.assertTrue((waiting["status"] == "waiting for opening prices").all())
        with tempfile.TemporaryDirectory() as folder:
            momentum.write_orders(Path(folder), waiting)
            waiting = momentum.read_orders(Path(folder))
        market.open.at[pd.Timestamp("2026-11-02"), ticker] = original_open
        filled = momentum.step(waiting, market, NAMES, at("2026-11-03 17:05"))
        self.assertEqual(set(filled["fill_date"]), {"2026-11-02"})
        self.assertAlmostEqual(float(filled.loc[filled["ticker"] == ticker, "fill_price"].iloc[0]), original_open, places=3)
        self.assertTrue((filled["amount_gbp"].astype(float) == 100.).all())
        again = momentum.step(filled, market, NAMES, at("2026-11-03 18:05"))
        pd.testing.assert_frame_equal(filled, again)

    def test_missing_sell_does_not_fund_buys_or_allow_later_decisions(self):
        initial = momentum.step(pd.DataFrame(columns=momentum.ORDER_COLUMNS), market_until("2026-11-02"), NAMES,
                                at("2026-11-02 17:05"))
        held = initial.loc[initial["action"] == "Buy", "ticker"].iloc[0]
        new = next(t for t in TICKERS if t not in set(initial["ticker"]))
        plans = pd.DataFrame([dict(decided_for="2026-11-30", action="Sell", ticker=held, status="planned"),
                              dict(decided_for="2026-11-30", action="Buy", ticker=new, status="planned")]).reindex(columns=momentum.ORDER_COLUMNS)
        orders = pd.concat([initial, plans], ignore_index=True)
        market = market_until("2027-01-04")
        market.open.at[pd.Timestamp("2026-12-01"), held] = float("nan")
        waiting = momentum.step(orders, market, NAMES, at("2027-01-04 17:05"))
        self.assertEqual(momentum.cash_now(waiting), momentum.cash_now(initial))
        self.assertEqual(len(waiting), len(orders))
        self.assertEqual(len(momentum.positions(waiting)), 10)
        self.assertTrue((waiting.tail(2)["status"] == "waiting for opening prices").all())


if __name__ == "__main__":
    unittest.main()
