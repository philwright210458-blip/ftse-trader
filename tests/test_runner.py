import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from io import BytesIO
from ftse_research_cli import (
    backup,
    export_records,
    import_legacy,
    load_config,
    push_snapshot,
    runner_lock,
    tick,
    write_json,
)


class RunnerTests(unittest.TestCase):
    def test_default_disabled_does_not_touch_network_or_database(self):
        result = tick({"enabled": False, "database": "does-not-exist.sqlite"})
        self.assertEqual(result["state"], "disabled")
        self.assertFalse(Path("does-not-exist.sqlite").exists())

    def test_config_requires_explicit_boolean(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "config.json"
            p.write_text('{"enabled":"false"}')
            with self.assertRaises(ValueError):
                load_config(p)

    def test_secure_publication_validation(self):
        for url in (
            "http://example.com",
            "https://user:password@example.com",
            "https://example.com/wrongpath",
            "file:///tmp/test",
        ):
            with self.assertRaises(ValueError):
                push_snapshot({}, url, "x" * 40)
        with self.assertRaises(ValueError):
            push_snapshot({}, "https://example.com", "short")

    def test_publication_requires_matching_persisted_readback(self):
        snapshot = {"generated_at": "2026-10-16T15:45:00+00:00"}
        ack = {"status": "accepted", "revision": "a" * 64, "generated_at": "2026-10-16T15:45:00.000Z"}
        opener = Mock()
        opener.open.side_effect = [BytesIO(json.dumps(ack).encode()), BytesIO(json.dumps(ack).encode())]
        with patch("ftse_research_cli.urllib.request.build_opener", return_value=opener):
            self.assertTrue(push_snapshot(snapshot, "https://example.com", "x" * 40)["readback_verified"])
        opener = Mock()
        opener.open.side_effect = [
            BytesIO(json.dumps(ack).encode()),
            BytesIO(json.dumps(dict(ack, revision="b" * 64)).encode()),
        ]
        with patch("ftse_research_cli.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                push_snapshot(snapshot, "https://example.com", "x" * 40)

    def test_collector_process_lock_serialises_different_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "jobs.sqlite")
            with runner_lock(path) as first:
                self.assertTrue(first)
                with runner_lock(path) as second:
                    self.assertFalse(second)
            with runner_lock(path) as third:
                self.assertTrue(third)

    def test_full_export_no_overwrite_and_numeric_strings_guarded(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "source.sqlite"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE trades (id TEXT, pnl REAL)")
                con.execute("INSERT INTO trades VALUES (?,?)", ("=unsafe", -2.5))
            out = Path(folder) / "out.csv"
            self.assertEqual(export_records(str(db), out, "trades")["records"], 1)
            self.assertIn("'=unsafe,-2.5", out.read_text(encoding="utf-8-sig"))
            with self.assertRaises(ValueError):
                export_records(str(db), out, "trades")
            with self.assertRaises(ValueError):
                export_records(str(db), out, "trades;DROP TABLE trades")

    def test_atomic_json_rejects_nonfinite_and_backup_preserves_rows(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "snapshot.json"
            write_json(p, {"ok": 1})
            with self.assertRaises(ValueError):
                write_json(p, {"bad": float("nan")})
            self.assertEqual(json.loads(p.read_text()), {"ok": 1})
            db = Path(folder) / "source.sqlite"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE test (id INTEGER)")
                con.execute("INSERT INTO test VALUES (1)")
            target = Path(folder) / "backup.sqlite"
            self.assertEqual(backup(str(db), str(target))["state"], "verified")
            with self.assertRaises(ValueError):
                backup(str(db), str(target))

    def test_legacy_import_preserves_sources_and_does_not_become_paper_profit(self):
        from ftse_research import ResearchStore, ResearchEngine

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "outcomes.csv"
            source = "key,status,r_gross\nold,done,999\n"
            path.write_text(source)
            with ResearchStore(Path(folder) / "research.sqlite") as store:
                engine = ResearchEngine(store)
                self.assertEqual(import_legacy(store, folder)["inserted"], 1)
                self.assertEqual(import_legacy(store, folder)["duplicates"], 1)
                self.assertEqual(path.read_text(), source)
                snap = engine.snapshot()
                self.assertEqual(snap["legacy"]["records"][0]["validation"], "legacy-unverified")
                self.assertEqual(snap["paper"]["closed_trades"], [])
                self.assertEqual(snap["paper"]["realised_balance"], 1000)


class ConfigurationSafetyTests(unittest.TestCase):
    def test_boolean_controls_do_not_accept_truthy_strings(self):
        from ftse_research import ResearchConfig

        for name in ("allow_synthetic_shorts", "allow_same_session_reentry"):
            with self.assertRaises(ValueError):
                ResearchConfig.from_dict({name: "false"})


if __name__ == "__main__":
    unittest.main()
