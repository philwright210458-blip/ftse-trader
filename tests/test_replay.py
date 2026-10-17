"""Progressive engine replay regressions, with no network or operational journal."""

from copy import deepcopy
from dataclasses import asdict, replace
import json
import unittest
from unittest.mock import patch

import pandas as pd

from ftse_research.models import Candle, Signal, iso, stamp
from ftse_research.replay import _run_scenario, build_bundle, build_demo_replay, replay_recorded


def at(clock):
    return pd.Timestamp(f"2026-10-16 {clock}", tz="Europe/London")


def bar(clock="09:00", fetched="09:16", source="synthetic-test"):
    start = at(clock)
    return Candle(
        "TEST",
        iso(start),
        iso(start + pd.Timedelta(minutes=15)),
        1000,
        1003,
        999,
        1001,
        1000,
        iso(at(fetched)),
        source=source,
    )


UNIVERSE = [{"ticker": "TEST", "company": "Fictional Test", "index": "FTSE 100", "quote_unit": "GBp"}]


class ProgressiveProof(unittest.TestCase):
    def test_smallest_progressive_engine_replay_has_no_future_entry_or_candle(self):
        signal = Signal("TEST", "authored-proof", "Long", iso(at("08:45")), iso(at("08:46")), 980, 1030, 1000)
        with patch("ftse_research.collection.collect_yahoo", side_effect=AssertionError("Replay must not fetch")):
            scenario = _run_scenario(
                scenario_id="smallest-proof",
                title="Smallest proof",
                description="Fictional",
                source_kind="synthetic",
                session="2026-10-16",
                records=[bar()],
                universe=UNIVERSE,
                clocks=[at("08:46"), at("09:00"), at("09:16")],
                injected_signals=[signal],
            )
        early, forming, observed = [frame["snapshot"] for frame in scenario["frames"]]
        self.assertEqual(early["paper"]["positions"], [])
        self.assertEqual(forming["candles"]["TEST"], [])
        self.assertEqual(forming["paper"]["positions"], [])
        self.assertEqual(len(observed["paper"]["positions"]), 1)
        position = observed["paper"]["positions"][0]
        self.assertEqual(stamp(position["entry_at"]), at("09:00"))
        self.assertGreaterEqual(stamp(position["entry_at"]), stamp(position["signal_available_at"]))
        self.assertAlmostEqual(position["entry_notional"], 100)
        self.assertGreater(position["entry_stamp_duty"], 0)
        self.assertFalse(scenario["configuration"]["allow_synthetic_shorts"])


class ReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = build_demo_replay()

    def test_bundle_is_deterministic_portable_and_bounded(self):
        self.assertEqual(self.bundle, build_demo_replay())
        self.assertEqual(self.bundle["kind"], "paper-replay")
        self.assertEqual(self.bundle["schema_version"], 1)
        self.assertLess(len(json.dumps(self.bundle).encode()), 4 * 1024 * 1024)
        self.assertEqual(len(self.bundle["scenarios"]), 3)

    def test_every_frame_reveals_only_information_known_by_its_clock(self):
        for scenario in self.bundle["scenarios"]:
            previous = None
            for index, frame in enumerate(scenario["frames"]):
                clock = stamp(frame["at"])
                self.assertEqual(frame["index"], index)
                if previous is not None:
                    self.assertGreater(clock, previous)
                previous = clock
                snapshot = frame["snapshot"]
                self.assertEqual(stamp(snapshot["generated_at"]), clock)
                for candles in snapshot["candles"].values():
                    for candle in candles:
                        self.assertLessEqual(stamp(candle["available_at"]), clock)
                for signal in snapshot["candidates"]:
                    self.assertLessEqual(stamp(signal["available_at"]), clock)
                for position in snapshot["paper"]["positions"] + snapshot["paper"]["closed_trades"]:
                    self.assertLessEqual(stamp(position["entry_observed_at"]), clock)
                    self.assertGreaterEqual(stamp(position["entry_at"]), stamp(position["signal_available_at"]))
                    if position.get("exit_observed_at"):
                        self.assertLessEqual(stamp(position["exit_observed_at"]), clock)
                for event in snapshot["paper"]["events"]:
                    self.assertLessEqual(stamp(event["at"]), clock)

    def test_short_is_skipped_under_unchanged_defaults(self):
        scenario = self.bundle["scenarios"][0]
        last = scenario["frames"][-1]["snapshot"]
        self.assertEqual(last["candidates"][0]["status"], "skipped")
        self.assertIn("short", last["candidates"][0]["reason"])
        self.assertEqual(last["paper"]["closed_trades"], [])
        self.assertEqual(last["paper"]["positions"], [])
        self.assertEqual(last["paper"]["realised_balance"], 1000)

    def test_faller_future_fill_and_costed_target_exit(self):
        scenario = self.bundle["scenarios"][1]
        at_signal = next(frame["snapshot"] for frame in scenario["frames"] if stamp(frame["at"]) == at("08:46"))
        self.assertEqual(at_signal["candidates"][0]["status"], "pending")
        self.assertEqual(at_signal["paper"]["positions"], [])
        last = scenario["frames"][-1]["snapshot"]
        trade = last["paper"]["closed_trades"][0]
        self.assertEqual(trade["reason"], "target")
        self.assertEqual(stamp(trade["entry_at"]), at("09:00"))
        self.assertGreater(trade["costs"], 0.5)
        self.assertFalse(last["evaluation"]["live_approved"])

    def test_missing_interval_withholds_later_favourable_target(self):
        scenario = self.bundle["scenarios"][2]
        last = scenario["frames"][-1]["snapshot"]
        self.assertEqual(len(last["paper"]["positions"]), 1)
        self.assertEqual(last["paper"]["closed_trades"], [])
        self.assertEqual(last["paper"]["realised_balance"], 999.5)
        kinds = {event["kind"] for event in last["paper"]["events"]}
        self.assertIn("unresolved-data-gap", kinds)
        self.assertIn("unresolved-close", kinds)
        before_delay = next(frame for frame in scenario["frames"] if stamp(frame["at"]) == at("08:46"))
        self.assertFalse(
            any(stamp(row["time"]) == at("08:15") for row in before_delay["snapshot"]["candles"]["REPLAY-GAP"])
        )

    def test_frame_copies_support_backwards_navigation_without_mutation(self):
        bundle = deepcopy(self.bundle)
        frames = bundle["scenarios"][1]["frames"]
        before = deepcopy(frames[0])
        frames[-1]["snapshot"]["paper"]["closed_trades"][0]["pnl"] = 123456
        frames[-1]["snapshot"]["watchlist"][0]["reason"] = "changed"
        self.assertEqual(frames[0], before)
        wrapped = build_bundle(bundle["scenarios"])
        bundle["scenarios"][0]["title"] = "outside mutation"
        self.assertNotEqual(wrapped["scenarios"][0]["title"], "outside mutation")

    def test_every_synthetic_frame_explains_authored_setups_and_isolation(self):
        self.assertFalse(self.bundle["provenance"]["operational_journal_modified"])
        for scenario in self.bundle["scenarios"]:
            self.assertEqual(scenario["source_kind"], "synthetic")
            self.assertIn("authored", scenario["signal_source"])
            for frame in scenario["frames"]:
                self.assertEqual(frame["snapshot"]["mode"], "synthetic-demo")
                self.assertIn("authored", frame["snapshot"]["replay"]["signal_source"])
                self.assertFalse(frame["snapshot"]["replay"]["network_access"])
                self.assertTrue(frame["snapshot"]["replay"]["is_replay"])
                self.assertTrue(frame["snapshot"]["replay"]["not_forward_paper"])
                report = frame["snapshot"]["daily_report"]
                self.assertIn("REPLAY", report["text"])
                self.assertIn("Scenario-authored", report["text"])
                self.assertEqual(report["provenance"]["kind"], "synthetic-replay")
                self.assertTrue(frame["snapshot"]["decision_journal"]["provenance"]["is_replay"])

    def test_recorded_helper_preserves_delayed_observation_and_inputs(self):
        records = [bar(fetched="09:40", source="recorded-provider-capture")]
        before = deepcopy(asdict(records[0]))
        scenario = replay_recorded(records, UNIVERSE, "2026-10-16", clocks=[at("09:16"), at("09:40")])
        self.assertEqual(scenario["source_kind"], "recorded")
        self.assertEqual(scenario["frames"][0]["snapshot"]["candles"]["TEST"], [])
        self.assertEqual(len(scenario["frames"][1]["snapshot"]["candles"]["TEST"]), 1)
        self.assertIn("Production scanner", scenario["signal_source"])
        self.assertIn("not independently verified", scenario["limitations"][0])
        self.assertEqual(asdict(records[0]), before)
        report = scenario["frames"][-1]["snapshot"]["daily_report"]
        self.assertIn("REPLAY", report["text"])
        self.assertIn("current production scanner", report["text"])
        self.assertIn("not independently verified", report["text"])
        self.assertEqual(report["provenance"]["kind"], "recorded-replay")

    def test_recorded_rejects_synthetic_mixed_units_missing_times_and_future_days(self):
        invalid = [
            [bar(source="synthetic-fixture")],
            [bar(source="recorded-one"), bar(clock="09:15", fetched="09:31", source="recorded-two")],
            [replace(bar(source="recorded"), quote_unit="GBP")],
            [{key: value for key, value in asdict(bar(source="recorded")).items() if key != "fetched_at"}],
            [replace(bar(source="recorded"), fetched_at=iso(at("09:05")))],
            [replace(bar(source="recorded"), end=iso(at("09:14")))],
            [replace(bar(source="recorded"), start=iso(at("09:01")), end=iso(at("09:16")))],
            [
                replace(
                    bar(source="recorded"),
                    start="2026-10-19T08:00:00+01:00",
                    end="2026-10-19T08:15:00+01:00",
                    fetched_at="2026-10-19T08:16:00+01:00",
                )
            ],
        ]
        for records in invalid:
            with self.subTest(records=records), self.assertRaises((ValueError, TypeError)):
                replay_recorded(records, UNIVERSE, "2026-10-16")

    def test_clock_order_and_source_mode_cannot_silently_change(self):
        records = [bar(source="recorded")]
        with self.assertRaises(ValueError):
            replay_recorded(records, UNIVERSE, "2026-10-16", clocks=[at("09:40"), at("09:16")])
        with self.assertRaises(ValueError):
            replay_recorded(records, UNIVERSE, "2026-10-16", configuration={"mode": "synthetic-demo"})
        with self.assertRaises(ValueError):
            replay_recorded(records, [{**UNIVERSE[0], "source_kind": "synthetic"}], "2026-10-16")

    def test_malformed_record_inputs_raise_clear_validation_errors(self):
        valid = asdict(bar(source="recorded"))
        for records, universe in (
            (None, UNIVERSE),
            (valid, UNIVERSE),
            ([valid], None),
            ([valid], {"TEST": UNIVERSE[0]}),
            ([valid], ["TEST"]),
            ([{**valid, "unknown_field": 1}], UNIVERSE),
            ([{**valid, "open": "1000"}], UNIVERSE),
            ([{**valid, "ticker": 123}], UNIVERSE),
            ([{**valid, "source": None}], UNIVERSE),
            ([{**valid, "fetched_at": None}], UNIVERSE),
            ([{**valid, "available_at": None}], UNIVERSE),
            ([valid], [{**UNIVERSE[0], "quote_unit": []}]),
            ([valid], [{**UNIVERSE[0], "source": 123}]),
        ):
            with self.subTest(records=records, universe=universe), self.assertRaises(ValueError):
                replay_recorded(records, universe, "2026-10-16")
        with self.assertRaises(ValueError):
            replay_recorded([valid], UNIVERSE, "2026-10-16", configuration=123)
        with self.assertRaises(ValueError):
            replay_recorded([valid], UNIVERSE, "2026-10-16", clocks=[None])


if __name__ == "__main__":
    unittest.main()
