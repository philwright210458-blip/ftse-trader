"""Replay boundaries must not write or publish the operational research ledger."""

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ftse_research_cli import main, push_snapshot


class ReplayCliTests(unittest.TestCase):
    def run_cli(self, args):
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return main(args)

    def test_replay_uses_no_operational_engine_or_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "replay.json"
            with (
                patch("ftse_research_cli.open_engine", side_effect=AssertionError("operational ledger opened")),
                patch("ftse_research_cli.push_snapshot", side_effect=AssertionError("publication attempted")),
            ):
                self.assertEqual(self.run_cli(["replay", "--out", str(output)]), 0)
            bundle = json.loads(output.read_text())
            self.assertEqual(bundle["kind"], "paper-replay")
            self.assertEqual(len(bundle["scenarios"]), 3)

    def test_replay_rejects_live_destination_and_publication_flags(self):
        for arguments in (
            ["--out", "runtime/snapshot.json"],
            ["--out", "runtime/research.sqlite"],
            ["--out", "/tmp/unused-replay.json", "--push"],
            ["--out", "/tmp/unused-replay.json", "--database", "/tmp/unused.sqlite"],
            ["--out", "config/research.json"],
            ["--out", "ftse_outcomes.xlsx"],
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_cli(["replay", *arguments]), 2)

    def test_recorded_source_cannot_be_overwritten_and_bad_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "observations.json"
            source.write_text("not-json")
            self.assertEqual(self.run_cli(["replay", "--input", str(source), "--out", str(source)]), 2)
            self.assertEqual(
                self.run_cli(["replay", "--input", str(source), "--out", str(source.with_name("out.json"))]), 2
            )
            self.assertEqual(source.read_text(), "not-json")

    def test_replay_frame_cannot_be_published_as_current_snapshot(self):
        with patch("ftse_research_cli.urllib.request.build_opener", side_effect=AssertionError("network opened")):
            with self.assertRaisesRegex(ValueError, "Replay frames"):
                push_snapshot({"replay": {"is_replay": True}}, "https://example.invalid", "x" * 40)


if __name__ == "__main__":
    unittest.main()
