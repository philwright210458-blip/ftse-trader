"""Research regressions use fictional records and no network or market claims."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from ftse_research import Candle, ResearchConfig, ResearchEngine, ResearchStore, Signal
from ftse_research.models import iso, stamp
from ftse_sessions import session_open_close


DAY = "2026-10-16"


def at(clock, day=DAY):
    return pd.Timestamp(f"{day} {clock}", tz="Europe/London")


def candle(ticker="TEST", start="09:00", open=1000, close=1000, high=1002,
           low=998, volume=100, fetched=None, day=DAY, quote_unit="GBp"):
    begin = at(start, day)
    end = begin + pd.Timedelta(minutes=15)
    return Candle(ticker, iso(begin), iso(end), open, high, low, close, volume,
                  iso(fetched or end + pd.Timedelta(minutes=1)), quote_unit=quote_unit,
                  source="synthetic-test")


def signal(ticker="TEST", available="08:46", direction="Long", strategy="test", stop=980, target=1030):
    return Signal(ticker, strategy, direction, iso(at("08:45")), iso(at(available)),
                  stop, target, 1000, "GBp", 80, "Synthetic test signal")


class ResearchCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "research.sqlite"
        self.store = ResearchStore(self.path)
        self.engine = ResearchEngine(self.store, ResearchConfig(stamp_duty_pct=0, spread_bps=0, slippage_bps=0))
        self.engine.set_universe([{"ticker": "TEST", "company": "Fictional Test", "index": "FTSE 100", "quote_unit": "GBp"}])

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_signal_waits_for_first_future_bar_not_source_close(self):
        self.engine.ingest_candles([candle(start="08:45"), candle(start="09:00")])
        before = self.engine.run(at("08:46"), [signal()])
        self.assertEqual(before["evaluation"]["sample_counts"]["pending"], 1)
        result = self.engine.run(at("09:16"), [])
        position = result["paper"]["positions"][0]
        self.assertEqual(stamp(position["entry_at"]), at("09:00"))
        self.assertGreaterEqual(stamp(position["entry_at"]), stamp(position["signal_available_at"]))
        self.assertEqual(stamp(position["entry_observed_at"]), at("09:16"))

    def test_backdated_external_signal_cannot_fill_existing_bars(self):
        self.engine.ingest_candles([candle(start="09:00")])
        result = self.engine.run(at("09:16"), [signal()])
        self.assertEqual(result["paper"]["positions"], [])
        self.assertEqual(stamp(result["candidates"][0]["available_at"]), at("09:16"))

    def test_missing_first_future_interval_cannot_slide_to_later_price(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:15")])
        result = self.engine.run(at("09:31"), [])
        self.assertEqual(result["paper"]["positions"], [])
        self.assertEqual(result["candidates"][0]["status"], "missing")

    def test_stale_entry_observation_does_not_backfill_after_outage(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00", fetched=at("10:20"))])
        result = self.engine.run(at("10:20"), [])
        self.assertEqual(result["candidates"][0]["status"], "missed")
        self.assertEqual(result["paper"]["positions"], [])

    def test_zero_volume_entry_is_skipped(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00", volume=0)])
        result = self.engine.run(at("09:16"), [])
        self.assertEqual(result["candidates"][0]["status"], "skipped")

    def test_ambiguous_stop_target_uses_stop(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00", low=975, high=1040)])
        result = self.engine.run(at("09:16"), [])
        trade = result["paper"]["closed_trades"][0]
        self.assertEqual(trade["reason"], "stop")
        self.assertEqual(trade["exit_price"], 980)
        self.assertAlmostEqual(trade["pnl"], -2.0)

    def test_adverse_gap_fills_at_open_not_stop(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00"),
            candle(start="09:15", open=970, close=975, high=977, low=965)])
        result = self.engine.run(at("09:31"), [])
        trade = result["paper"]["closed_trades"][0]
        self.assertEqual(trade["exit_price"], 970)
        self.assertEqual(trade["reason"], "gap-stop")

    def test_missing_exit_interval_keeps_unresolved_until_recovered(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00"),
            candle(start="09:30", open=1000, close=1030, high=1040, low=998)])
        first = self.engine.run(at("09:46"), [])
        self.assertEqual(len(first["paper"]["positions"]), 1)
        self.assertFalse(first["paper"]["closed_trades"])
        self.engine.ingest_candles([candle(start="09:15", low=970, fetched=at("10:00"))])
        recovered = self.engine.run(at("10:00"), [])
        self.assertEqual(recovered["paper"]["closed_trades"][0]["reason"], "stop")
        self.assertEqual(stamp(recovered["paper"]["closed_trades"][0]["exit_at"]), at("09:30"))

    def test_missing_close_never_invents_a_session_timeout(self):
        self.engine.run(at("15:46"), [Signal("TEST", "test", "Long", iso(at("15:45")),
            iso(at("15:46")), 900, 1100, 1000)])
        self.engine.ingest_candles([candle(start="16:00")])
        result = self.engine.run(at("17:00"), [])
        self.assertEqual(len(result["paper"]["positions"]), 1)
        self.assertTrue(any(e["kind"] == "unresolved-close" for e in result["paper"]["events"]))

    def test_true_close_bar_and_early_close_resolve_timeout(self):
        # Christmas Eve is a London half-day. Full calendar close is 12:30.
        day = "2026-12-24"
        start, end = session_open_close(day)
        self.assertEqual(end.hour, 12)
        sig_at = at("11:46", day)
        sig = Signal("TEST", "halfday", "Long", iso(at("11:45", day)), iso(sig_at), 900, 1100, 1000)
        self.engine.run(sig_at, [sig])
        self.engine.ingest_candles([candle(start="12:00", day=day), candle(start="12:15", day=day)])
        result = self.engine.run(at("12:31", day), [])
        self.assertEqual(result["paper"]["closed_trades"][0]["reason"], "session-close")

    def test_repeat_run_and_restart_never_duplicate_or_reset_cash(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00", low=975)])
        first = self.engine.run(at("09:16"), [])
        self.assertEqual(first["paper"]["realised_balance"], 998)
        second = self.engine.run(at("09:16"), [])
        self.assertEqual(first["paper"], second["paper"])
        self.store.close()
        self.store = ResearchStore(self.path)
        self.engine = ResearchEngine(self.store)
        third = self.engine.run(at("09:17"), [])
        self.assertEqual(third["paper"]["realised_balance"], 998)
        self.assertEqual(len(third["paper"]["closed_trades"]), 1)

    def test_open_position_prevents_duplicate_strategy_and_reentry(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00"), candle(start="09:15", high=1040)])
        self.engine.run(at("09:31"), [])
        later = Signal("TEST", "test", "Long", iso(at("09:30")), iso(at("09:31")), 980, 1030, 1000)
        self.engine.run(at("09:31"), [later])  # same clock/run key is idempotent
        self.engine.run(at("09:32"), [later])
        self.engine.ingest_candles([candle(start="09:45")])
        result = self.engine.run(at("10:01"), [])
        self.assertEqual(len(result["paper"]["closed_trades"]), 1)
        self.assertTrue(any("re-entry" in row["reason"] for row in result["candidates"]))

    def test_synthetic_short_is_explicit_and_not_a_long_purchase(self):
        self.engine.run(at("08:46"), [signal(direction="Short", stop=1020, target=970)])
        self.engine.ingest_candles([candle(start="09:00", low=960)])
        result = self.engine.run(at("09:16"), [])
        self.assertEqual(result["candidates"][0]["status"], "skipped")
        self.assertIn("short", result["candidates"][0]["reason"])
        self.assertEqual(result["paper"]["realised_balance"], 1000)

    def test_gbp_and_pence_position_values_match(self):
        self.engine.set_universe([{"ticker": "TEST", "quote_unit": "GBP"}])
        sig = Signal("TEST", "test", "Long", iso(at("08:45")), iso(at("08:46")), 9.8, 10.3, 10, "GBP")
        self.engine.run(at("08:46"), [sig])
        self.engine.ingest_candles([candle(start="09:00", open=10, close=10, high=10.4, low=9.9, quote_unit="GBP")])
        result = self.engine.run(at("09:16"), [])
        self.assertAlmostEqual(result["paper"]["closed_trades"][0]["pnl"], 3.0)

    def test_incomplete_candle_not_inserted_and_revision_is_audited(self):
        original = candle(start="09:00")
        forming = candle(start="09:15", fetched=at("09:20"))
        changed = candle(start="09:00", close=1001)
        counts = self.engine.ingest_candles([original, forming])
        self.assertEqual(counts, {"inserted": 1, "duplicates": 0, "revisions": 0, "incomplete": 1})
        result = self.engine.ingest_candles([original, changed, changed])
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["revisions"], 1)
        self.assertEqual(self.store.candles("TEST")[0]["close"], 1000)

    def test_store_transaction_rolls_back_partial_invalid_batch(self):
        bad = candle(start="09:15", close=-1)
        with self.assertRaises(ValueError):
            self.engine.ingest_candles([candle(), bad])
        self.assertFalse(self.store.candles("TEST"))

    def test_backup_restores_committed_ledger_and_integrity(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00", high=1040)])
        result = self.engine.run(at("09:16"), [])
        destination = Path(self.directory.name) / "backup.sqlite"
        self.store.backup(destination)
        with ResearchStore(destination) as restored:
            snapshot = ResearchEngine(restored).snapshot(at("09:16"))
            self.assertEqual(result["paper"], snapshot["paper"])

    def test_no_trade_session_reconciles_and_staleness_advances(self):
        self.engine.ingest_candles([candle(start="09:00")])
        result = self.engine.run(at("09:16"), [])
        self.assertTrue(result["evaluation"]["sample_counts"]["reconciled"])
        self.assertEqual(result["evaluation"]["closed_trades"], 0)
        later = self.engine.snapshot(at("10:00"))
        self.assertTrue(later["watchlist"][0]["stale"])
        self.assertEqual(later["health"]["state"], "degraded")

    def test_initial_capital_and_demo_mode_cannot_silently_change(self):
        with self.assertRaises(ValueError):
            ResearchEngine(self.store, ResearchConfig(capital=5000))
        with self.assertRaises(ValueError):
            ResearchEngine(self.store, ResearchConfig(mode="synthetic-demo"))

    def test_invalid_direction_and_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            signal(direction="Buy").validate()
        with self.assertRaises(ValueError):
            stamp("2026-10-16 09:00")

    def test_legacy_records_are_visible_but_not_corrected_paper_evidence(self):
        self.store.connection.execute("INSERT INTO legacy_records VALUES(?,?,?,?,?)",
            ("legacy-example", "scan_data/outcomes.csv", "filehash", 2,
             json.dumps({"ticker": "TEST", "pnl": "100000", "validation": "approved"})))
        result = self.engine.snapshot(at("09:16"))
        self.assertEqual(result["legacy"]["counts"], {"scan_data/outcomes.csv": 1})
        self.assertEqual(result["legacy"]["records"][0]["validation"], "legacy-unverified")
        self.assertEqual(result["evaluation"]["closed_trades"], 0)
        self.assertEqual(result["paper"]["realised_balance"], 1000)

    def test_daily_adjustment_revision_prevents_false_split_mover(self):
        records = []
        for day, raw, adjusted in (("2026-10-14", 1000, 1000), ("2026-10-15", 530, 530)):
            start, end = session_open_close(day)
            records.append(Candle("TEST", iso(start), iso(end), raw, raw + 2, raw - 2, raw, 1000,
                iso(stamp(end) + pd.Timedelta(minutes=1)), "1d", "GBp", adjusted, "synthetic-test"))
        self.engine.ingest_candles(records)
        original = records[0]
        revised = Candle("TEST", original.start, original.end, 1000, 1002, 998, 1000, 1000,
                         iso(at("17:00", "2026-10-15")), "1d", "GBp", 500, "synthetic-test")
        self.engine.ingest_candles([revised])
        before = self.engine.snapshot(at("07:45"))["watchlist"][0]
        self.assertAlmostEqual(before["previous_change_pct"], 6.0)
        self.assertTrue(before["is_mover"])
        self.assertEqual(before["previous_close"], 530)
        self.assertEqual(before["status"], "unavailable")
        self.engine.ingest_candles([
            candle(start="08:00", open=530, close=529, high=531, low=528),
            candle(start="08:15", open=529, close=528, high=530, low=527),
            candle(start="08:30", open=528, close=527, high=529, low=526)])
        after = self.engine.snapshot(at("08:46"))["watchlist"][0]
        self.assertEqual(after["status"], "reversal")
        self.assertEqual(after["opening_gap_pct"], 0)
        self.assertEqual(after["opening_price"], 530)

    def test_no_adjusted_previous_close_means_no_inferred_mover(self):
        start, end = session_open_close("2026-10-15")
        self.engine.ingest_candles([Candle("TEST", iso(start), iso(end), 1000, 1002, 998, 1000, 10,
            iso(stamp(end) + pd.Timedelta(minutes=1)), "1d", "GBp", None, "synthetic-test")])
        watch = self.engine.snapshot(at("07:45"))["watchlist"][0]
        self.assertIsNone(watch["previous_change_pct"])
        self.assertFalse(watch["is_mover"])

    def test_daily_loss_limit_blocks_next_strategy_without_reset(self):
        self.engine = ResearchEngine(self.store, ResearchConfig(stamp_duty_pct=0, spread_bps=0, slippage_bps=0, daily_loss_limit_pct=.1))
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00", low=975)])
        self.engine.run(at("09:16"), [])
        next_signal = Signal("TEST", "different-test", "Long", iso(at("09:15")), iso(at("09:17")), 980, 1030, 1000)
        self.engine.run(at("09:17"), [next_signal])
        self.engine.ingest_candles([candle(start="09:30")])
        result = self.engine.run(at("09:46"), [])
        self.assertEqual(result["paper"]["realised_balance"], 998)
        self.assertTrue(any("Daily virtual loss limit" in row["reason"] for row in result["candidates"]))

    def test_account_exposure_cannot_exceed_virtual_cash(self):
        # Capital cannot change after journal creation, so use a fresh journal.
        with ResearchStore(":memory:") as store:
            engine = ResearchEngine(store, ResearchConfig(capital=200, max_positions=3, stamp_duty_pct=0, spread_bps=0, slippage_bps=0))
            engine.set_universe([{"ticker": f"TEST{i}", "quote_unit": "GBp"} for i in range(3)])
            engine.run(at("08:46"), [signal(ticker=f"TEST{i}") for i in range(3)])
            engine.ingest_candles([candle(ticker=f"TEST{i}", start="09:00") for i in range(3)])
            result = engine.run(at("09:16"), [])
            self.assertEqual(len(result["paper"]["positions"]), 2)
            self.assertTrue(any("Insufficient" in row["reason"] for row in result["candidates"]))

    def test_late_exit_observation_does_not_free_capacity_for_earlier_entry(self):
        self.engine = ResearchEngine(self.store, ResearchConfig(max_positions=1, stamp_duty_pct=0, spread_bps=0, slippage_bps=0))
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00")])
        self.engine.run(at("09:16"), [])
        later = Signal("TEST", "second-strategy", "Long", iso(at("09:15")), iso(at("09:17")), 980, 1030, 1000)
        self.engine.run(at("09:17"), [later])
        self.engine.ingest_candles([candle(start="09:15", high=1040, fetched=at("09:31")),
                                  candle(start="09:30")])
        result = self.engine.run(at("09:46"), [])
        self.assertEqual(len(result["paper"]["closed_trades"]), 1)
        self.assertTrue(any("Maximum concurrent" in row["reason"] for row in result["candidates"]))

    def test_equal_time_candidates_allocate_in_score_order(self):
        self.engine = ResearchEngine(self.store, ResearchConfig(max_positions=1, stamp_duty_pct=0, spread_bps=0, slippage_bps=0))
        low = Signal("TEST", "low-score", "Long", iso(at("08:45")), iso(at("08:46")), 980, 1030, 1000, score=10)
        high = Signal("TEST", "high-score", "Long", iso(at("08:45")), iso(at("08:46")), 980, 1030, 1000, score=90)
        self.engine.run(at("08:46"), [low, high])
        self.engine.ingest_candles([candle(start="09:00")])
        result = self.engine.run(at("09:16"), [])
        self.assertEqual(result["paper"]["positions"][0]["strategy"], "high-score")

    def test_closed_positions_use_their_recorded_cost_configuration(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00")])
        self.engine.run(at("09:16"), [])
        self.engine = ResearchEngine(self.store, ResearchConfig(stamp_duty_pct=0, spread_bps=0, slippage_bps=0, commission_per_side=10))
        self.engine.ingest_candles([candle(start="09:15", high=1040)])
        result = self.engine.run(at("09:31"), [])
        self.assertAlmostEqual(result["paper"]["closed_trades"][0]["pnl"], 3.0)

    def test_pending_signal_is_rejected_after_configuration_changes(self):
        self.engine.run(at("08:46"), [signal()])
        self.engine = ResearchEngine(self.store, ResearchConfig(spread_bps=20, slippage_bps=5))
        self.engine.ingest_candles([candle(start="09:00")])
        result = self.engine.run(at("09:16"), [])
        self.assertEqual(result["candidates"][0]["status"], "skipped")
        self.assertIn("configuration change", result["candidates"][0]["reason"])
        self.assertTrue(result["evaluation"]["configuration_changed"])

    def test_purchase_duty_is_additional_to_confirmed_position_notional(self):
        self.engine = ResearchEngine(self.store, ResearchConfig(stamp_duty_pct=.5, spread_bps=0, slippage_bps=0))
        self.engine.run(at("08:46"), [signal()])
        self.engine.ingest_candles([candle(start="09:00")])
        opened = self.engine.run(at("09:16"), [])
        trade = opened["paper"]["positions"][0]
        self.assertAlmostEqual(trade["entry_notional"], 100)
        self.assertAlmostEqual(trade["entry_cash_required"], 100.5)
        self.assertAlmostEqual(opened["paper"]["realised_balance"], 1000 - trade["entry_stamp_duty"])
        self.engine.ingest_candles([candle(start="09:15", high=1040)])
        closed = self.engine.run(at("09:31"), [])["paper"]["closed_trades"][0]
        self.assertAlmostEqual(closed["pnl"], 3 - trade["entry_stamp_duty"])
        self.assertAlmostEqual(closed["costs"], trade["entry_stamp_duty"])

    def test_entry_rejected_if_capital_covers_notional_but_not_duty(self):
        with ResearchStore(":memory:") as store:
            engine = ResearchEngine(store, ResearchConfig(capital=100, stamp_duty_pct=.5, spread_bps=0, slippage_bps=0))
            engine.set_universe([{"ticker": "TEST", "quote_unit": "GBp"}])
            engine.run(at("08:46"), [signal()])
            engine.ingest_candles([candle(start="09:00")])
            result = engine.run(at("09:16"), [])
            self.assertEqual(result["paper"]["positions"], [])
            self.assertIn("Insufficient", result["candidates"][0]["reason"])

    def test_collection_failure_still_advances_health_without_fake_entries(self):
        self.engine.run(at("08:46"), [signal()])
        with patch("ftse_research.collection.collect_yahoo", return_value=([], ["TEST: feed unavailable"])):
            result = self.engine.collect_and_run(["TEST"])
        self.assertEqual(result["health"]["state"], "failed")
        self.assertIn("feed unavailable", result["health"]["last_error"])
        self.assertEqual(result["paper"]["positions"], [])

    def test_scanner_history_window_is_bounded_but_archive_remains_complete(self):
        self.engine = ResearchEngine(self.store, ResearchConfig(history_days=1))
        self.engine.ingest_candles([candle(start="09:00", day="2026-10-14"), candle(start="09:00")])
        bounded = self.engine._intraday("TEST", at("09:16"))
        self.assertEqual(len(bounded), 1)
        self.assertEqual(len(self.store.candles("TEST")), 2)

    def test_collection_uses_incremental_period_only_after_every_symbol_has_history(self):
        with patch("ftse_research.collection.collect_yahoo", return_value=([], ["offline"])) as provider:
            self.engine.collect_and_run(["TEST"])
            self.assertEqual(provider.call_args.kwargs["intraday_period"], "60d")
        self.engine.ingest_candles([candle(start="09:00")])
        with patch("ftse_research.collection.collect_yahoo", return_value=([], ["offline"])) as provider:
            self.engine.collect_and_run(["TEST"])
            self.assertEqual(provider.call_args.kwargs["intraday_period"], "5d")


class ResearchDemoCase(unittest.TestCase):
    def test_fictional_demo_runs_persistently_and_is_idempotent(self):
        with ResearchStore(":memory:") as store:
            engine = ResearchEngine(store, ResearchConfig(mode="synthetic-demo"))
            snapshot = engine.seed_demo("2026-10-17T10:00:00Z")
            self.assertEqual(snapshot["mode"], "synthetic-demo")
            self.assertEqual(len(snapshot["watchlist"]), 3)
            self.assertEqual(len([w for w in snapshot["watchlist"] if w["is_mover"]]), 2)
            self.assertEqual(snapshot["evaluation"]["closed_trades"], 1)
            self.assertFalse(snapshot["evaluation"]["live_approved"])
            self.assertEqual(snapshot["paper"], engine.seed_demo()["paper"])
            self.assertLess(len(json.dumps(snapshot).encode()), 1_500_000)


if __name__ == "__main__":
    unittest.main()
