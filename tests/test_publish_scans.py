"""Archive publication cannot collect prices, mix demos or mutate scanner inputs."""

import datetime as dt
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import publish_scans as publisher


class PublisherTests(unittest.TestCase):
    def test_snapshot_preserves_rows_provenance_and_separate_clocks(self):
        files = list(Path("scan_data").glob("*.csv"))
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        s = publisher.build_snapshot(
            "HEAD", now=dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc)
        )
        self.assertFalse(s["synthetic"])
        self.assertEqual(s["source"]["commit_sha"], publisher.git("rev-parse", "HEAD"))
        self.assertEqual(s["source"]["last_scan_at"], s["runs"][-1]["_collected_at"])
        self.assertNotEqual(s["published_at"], s["source"]["last_scan_at"])
        self.assertEqual(s["setups"][0]["_run_id"], publisher.run_id(s["setups"][0]))
        for f in s["source"]["files"]:
            self.assertEqual(
                f["sha256"],
                hashlib.sha256(
                    subprocess.check_output(
                        ["git", "show", "HEAD:scan_data/" + f["name"]]
                    )
                ).hexdigest(),
            )
        self.assertEqual(
            before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        )

    def test_unsafe_destination_or_missing_credentials_fails_before_network(self):
        for url in [
            "http://example.com",
            "https://user:password@example.com",
            "https://example.com/path",
            "https://example.com?token=private",
        ]:
            with patch.dict(os.environ, {"FTSE_SCANS_SITE_URL": url}, clear=True):
                with self.assertRaises(ValueError):
                    publisher.publish({})
        with patch.dict(
            os.environ, {"FTSE_SCANS_SITE_URL": "https://example.com"}, clear=True
        ):
            with self.assertRaises(ValueError):
                publisher.publish({})

    def test_demo_archive_rejected(self):
        original = subprocess.check_output

        def output(args, **kwargs):
            raw = original(args, **kwargs)
            if args[:2] == ["git", "show"] and args[2].endswith(":scan_data/runs.csv"):
                return raw.replace(b",full,", b",synthetic-demo,")
            return raw

        with patch.object(subprocess, "check_output", side_effect=output):
            with self.assertRaises(ValueError):
                publisher.build_snapshot()

    def test_naive_publication_clock_rejected(self):
        with self.assertRaises(ValueError):
            publisher.build_snapshot(now=dt.datetime(2026, 10, 17))
