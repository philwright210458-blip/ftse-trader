"""Deterministic regressions for the seven reviewed simulation defects."""

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import ftse_outcomes as out
import ftse_trade_scanner as sc
from ftse_sessions import is_session, previous_session, session_for, session_open_close


def stamp(value):
    return pd.Timestamp(value, tz="Europe/London")


def bars(start="2026-10-15 10:45", end="2026-10-15 16:15", **changes):
    times = pd.date_range(stamp(start), stamp(end), freq="15min")
    result = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10}, index=times)
    for column, value in changes.items():
        result[column] = value
    return result


def candidate(key="test", day="2026-10-15", as_of="10:30", run_time="2026-10-15 10:31:12", ticker="TEST.L"):
    return dict(
        key=key,
        day=day,
        as_of=as_of,
        run_time=run_time,
        mode="full",
        ticker=ticker,
        direction="Long",
        stop=95.0,
        target=115.0,
        score=90.0,
        company="Test",
        index="FTSE 100",
        setup="Range",
        touches=4,
        conflict=False,
        data_time=f"{day}T10:15:00+01:00",
        entry=100,
        risk_pct=5,
        zone_low=96,
        zone_high=99,
    )


def outcome(key="test", result="target", r=3.0, exit_time="2026-10-15T15:00:00+01:00", status="done"):
    return dict(
        key=key,
        day="2026-10-15",
        ticker="TEST.L",
        direction="Long",
        horizon=1,
        status=status,
        outcome=result,
        fill=100.0,
        exit_price=100 + 5 * r,
        exit_time=exit_time,
        r_gross=r,
        fill_risk_pct=5.0,
        ambiguous=False,
    )


class Sessions(unittest.TestCase):
    def test_holiday_and_previous_session(self):
        self.assertFalse(is_session("2026-12-25"))
        self.assertIsNone(session_for("2026-12-25"))
        self.assertEqual(str(previous_session("2026-12-29").date()), "2026-12-24")
        self.assertEqual(str(previous_session("2026-10-19").date()), "2026-10-16")
        with self.assertRaises(ValueError):
            session_open_close("2026-12-25")

    def test_dst_and_early_close(self):
        summer, _ = session_open_close("2026-10-23")
        winter, _ = session_open_close("2026-10-26")
        self.assertEqual(summer.hour, 8)
        self.assertEqual(winter.hour, 8)
        self.assertEqual(summer.tz_convert("UTC").hour, 7)
        self.assertEqual(winter.tz_convert("UTC").hour, 8)
        opening, closing = session_open_close("2026-12-24")
        self.assertEqual(closing.strftime("%H:%M"), "12:30")
        result = out.simulate("Long", 95, 115, opening, bars("2026-12-24 08:00", "2026-12-24 12:15"), 1, closing)
        self.assertEqual((result.status, result.outcome), ("done", "timeout"))
        self.assertEqual(pd.Timestamp(result.exit_time), closing)


class VolumeAndCompletion(unittest.TestCase):
    def test_all_zero_and_missing_volume(self):
        self.assertTrue(sc.drop_untraded_bars(bars(volume=0)).empty)
        self.assertTrue(sc.drop_untraded_bars(bars(volume=float("nan"))).empty)
        self.assertEqual(len(sc.drop_untraded_bars(bars().drop(columns="volume"))), len(bars()))

    def test_single_forming_candle_removed(self):
        data = bars("2026-10-15 16:00", "2026-10-15 16:00")
        self.assertTrue(sc.completed_candles(data, "15m", stamp("2026-10-15 16:05")).empty)
        self.assertEqual(len(sc.completed_candles(data, "15m", stamp("2026-10-15 16:15"))), 1)

    def test_utc_and_naive_provider_indices(self):
        data = bars()
        utc = data.tz_convert("UTC")
        naive = data.tz_localize(None)
        now = stamp("2026-10-15 17:00")
        for frame in (utc, naive):
            result = sc.completed_candles(frame, "15m", now)
            self.assertTrue(result.index.tz_convert("UTC").equals(data.index.tz_convert("UTC")))


class Simulation(unittest.TestCase):
    def test_scan_availability_not_signal_close(self):
        data = bars("2026-10-15 10:30", "2026-10-15 16:15")
        data.loc[stamp("2026-10-15 10:30"), "high"] = 116
        data.loc[stamp("2026-10-15 10:45"), ["open", "high", "low", "close"]] = [116, 117, 114, 116]
        with tempfile.TemporaryDirectory() as folder, patch.object(sc, "fetch_data", return_value=data):
            path = Path(folder) / "outcomes.v2.csv"
            out.evaluate_pending(
                pd.DataFrame([candidate()]), pd.DataFrame(columns=out.OUTCOME_COLUMNS), [1], "15m", "60d", 0, path
            )
            result = out.read_outcomes(path).iloc[0]
        self.assertEqual((result["status"], result["outcome"], result["fill"]), ("skipped", "gap", 116))

    def test_forming_bar_cannot_complete_or_hit_stop(self):
        data = bars()
        data.loc[stamp("2026-10-15 16:00"), "low"] = 90
        result = out.simulate("Long", 95, 115, stamp("2026-10-15 10:31:12"), data, 1, stamp("2026-10-15 16:05"))
        self.assertEqual(result.status, "pending")
        result = out.simulate("Long", 95, 115, stamp("2026-10-15 10:31:12"), data, 1, stamp("2026-10-15 16:15"))
        self.assertEqual((result.status, result.outcome), ("done", "stop"))

    def test_timeout_requires_final_bar_and_continuous_coverage(self):
        data = bars()
        available, now = stamp("2026-10-15 10:31:12"), stamp("2026-10-15 17:00")
        for incomplete in (data.iloc[:-1], data.drop(stamp("2026-10-15 12:00"))):
            self.assertEqual(out.simulate("Long", 95, 115, available, incomplete, 1, now).status, "pending")
        self.assertEqual(out.simulate("Long", 95, 115, available, data, 1, now).outcome, "timeout")

    def test_missing_before_target_is_not_a_win(self):
        data = bars().drop(stamp("2026-10-15 11:00"))
        data.loc[stamp("2026-10-15 11:15"), "high"] = 120
        self.assertEqual(
            out.simulate("Long", 95, 115, stamp("2026-10-15 10:31:12"), data, 1, stamp("2026-10-15 17:00")).status,
            "pending",
        )

    def test_early_terminal_target_both_directions(self):
        for direction, stop, target, high, low in (("Long", 95, 115, 116, 99), ("Short", 105, 85, 101, 84)):
            data = bars("2026-10-15 10:45", "2026-10-15 10:45", high=high, low=low)
            result = out.simulate(
                direction, stop, target, stamp("2026-10-15 10:31:12"), data, 5, stamp("2026-10-15 11:00")
            )
            self.assertEqual((result.status, result.outcome, result.r_gross), ("done", "target", 3))

    def test_ambiguous_assumes_stop_and_gaps_can_exceed_one_r(self):
        data = bars("2026-10-15 10:45", "2026-10-15 11:00")
        data.loc[stamp("2026-10-15 10:45"), ["high", "low"]] = [116, 94]
        result = out.simulate("Long", 95, 115, stamp("2026-10-15 10:31:12"), data, 1, stamp("2026-10-15 11:15"))
        self.assertEqual((result.outcome, result.ambiguous), ("stop", True))
        data = bars("2026-10-15 10:45", "2026-10-15 11:00")
        data.loc[stamp("2026-10-15 11:00"), ["open", "high", "low", "close"]] = [90, 91, 89, 90]
        result = out.simulate("Long", 95, 115, stamp("2026-10-15 10:31:12"), data, 1, stamp("2026-10-15 11:15"))
        self.assertEqual((result.outcome, result.r_gross), ("stop", -2))

    def test_horizon_uses_sessions_not_observed_days(self):
        data = pd.concat([bars("2026-10-16 16:00", "2026-10-16 16:15"), bars("2026-10-19 08:00", "2026-10-19 16:15")])
        result = out.simulate("Long", 95, 115, stamp("2026-10-16 16:00"), data, 2, stamp("2026-10-19 16:30"))
        self.assertEqual(result.outcome, "timeout")
        self.assertEqual(pd.Timestamp(result.exit_time), stamp("2026-10-19 16:30"))

    def test_zero_volume_is_unresolved(self):
        result = out.simulate(
            "Long", 95, 115, stamp("2026-10-15 10:31:12"), bars(volume=0), 1, stamp("2026-10-15 17:00")
        )
        self.assertEqual(result.status, "pending")


class DecisionsAndStatistics(unittest.TestCase):
    def test_empty_exact_decision_run_not_later_scan(self):
        late = candidate(as_of="10:45", run_time="2026-10-15 10:46:00")
        earlier = dict(late, as_of="10:30", run_time="2026-10-15 10:31:00")
        runs = pd.DataFrame([earlier, late])
        result = out.build_picks(pd.DataFrame([late]), "10:30", runs)
        self.assertTrue(all(frame.empty for frame in result.values()))
        self.assertTrue(all(frame.empty for frame in out.build_picks(pd.DataFrame([late]), "10:30").values()))

    def test_entry_cutoff_applies_to_availability_and_day(self):
        with tempfile.TemporaryDirectory() as folder:
            rows = [candidate(run_time="2026-10-15 15:30:01"), candidate(key="late", run_time="2026-10-16 08:00:00")]
            pd.DataFrame(rows)[sc.SETUP_COLUMNS].to_csv(Path(folder) / "setups.csv", index=False)
            self.assertTrue(out.load_candidates(Path(folder), "15:30").empty)

    def test_realised_drawdown_exit_order_and_no_fixed_breakeven(self):
        trades = pd.DataFrame(
            [
                outcome("monday", "stop", -1, "2026-10-16T15:00:00+01:00"),
                outcome("tuesday", "target", 3, "2026-10-13T15:00:00+01:00"),
                outcome("wednesday", "stop", -1, "2026-10-14T15:00:00+01:00"),
            ]
        )
        stats = out.summarise(out.add_costs(trades, 0, 0))
        self.assertEqual(stats["drawdown"], -2)
        self.assertNotIn("breakeven", stats)

    def test_workbook_counts_missing_and_dates_realisation(self):
        frame = pd.DataFrame([candidate(), candidate("missing", ticker="MISS.L")])
        outcomes = pd.DataFrame(
            [outcome(exit_time="2026-10-16T15:00:00+01:00"), outcome("missing", "missing_coverage", status="nodata")]
        )
        picks = {out.LIVE: frame.assign(rank=[1, 2])}
        args = argparse.Namespace(
            horizons="1", horizon=1, cost_pct=0.0, stamp_duty_pct=0.0, pick_time="10:30", position_gbp=100.0
        )
        workbook = out.build_report(args, frame, frame, picks, outcomes, {}, "test")
        live = workbook["Live Mock Trades"]
        self.assertEqual([live.cell(out.TOTALS_ROW, c).value for c in (2, 6)], [1, 1])
        results = [live.cell(r, 11).value for r in range(out.TRADES_HEADER_ROW + 1, live.max_row + 1)]
        self.assertEqual(sorted(results), ["No data", "Target hit"])
        self.assertEqual(workbook["Daily"].cell(2, 1).value, "2026-10-16")

    def test_missing_outcome_retried_and_replaced_without_duplicates(self):
        missing = pd.DataFrame([outcome(status="nodata", result="missing_coverage")])
        data = bars(high=116)
        with tempfile.TemporaryDirectory() as folder, patch.object(sc, "fetch_data", return_value=data):
            path = Path(folder) / out.CACHE_FILENAME
            missing.to_csv(path, index=False)
            result = out.evaluate_pending(pd.DataFrame([candidate()]), missing, [1], "15m", "60d", 0, path)
            refreshed = out.read_outcomes(path)
            self.assertEqual((result[0], len(refreshed), refreshed.iloc[0]["status"]), (1, 1, "done"))
            second = out.evaluate_pending(pd.DataFrame([candidate()]), refreshed, [1], "15m", "60d", 0, path)
            self.assertEqual(second, (0, 0, 0))
            self.assertEqual(len(out.read_outcomes(path)), 1)

    def test_empty_conflicting_candidates_keep_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            pd.DataFrame([dict(candidate(), conflict=True)])[sc.SETUP_COLUMNS].to_csv(
                Path(folder) / "setups.csv", index=False
            )
            result = out.load_candidates(Path(folder), "15:30")
            self.assertTrue(result.empty)
            self.assertIn("key", result.columns)

    def test_first_sighting_uses_publication_order(self):
        stale = candidate("late", as_of="10:15", run_time="2026-10-15 10:40:00")
        earlier = candidate("early", as_of="10:30", run_time="2026-10-15 10:31:00")
        result = out.first_sightings(pd.DataFrame([stale, earlier]))
        self.assertEqual(result.iloc[0]["key"], "early")

    def test_v1_cache_preserved(self):
        self.assertNotIn(out.CACHE_FILENAME, ("outcomes.csv", "outcomes.v2.csv"))
        self.assertEqual(out.SIMULATION_VERSION, "availability-session-v3")


if __name__ == "__main__":
    unittest.main()
