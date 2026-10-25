#!/usr/bin/env python3
"""Publish existing scanner CSVs only; never collect prices or simulate trades."""

import argparse
from collections import deque
import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
from zoneinfo import ZoneInfo

REPOSITORY = "philwright210458-blip/ftse-trader"
FILES = {
    "runs.csv": 100,
    "setups.csv": 3000,
    "outcomes.csv": 2000,
    "outcomes.v2.csv": 2000,
    "outcomes.v3.csv": 2000,
}


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def run_id(row):
    fields = [row.get(k, "") for k in ("day", "as_of", "mode", "run_time", "data_time")]
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()[:32]


def build_snapshot(ref="HEAD", now=None):
    commit = git("rev-parse", "--verify", ref + "^{commit}")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Expected a full source commit")
    tree = set(
        git("ls-tree", "-r", "--name-only", commit, "--", "scan_data").splitlines()
    )
    tables, sources = {}, []
    for name, limit in FILES.items():
        if name == "outcomes.v2.csv" and "scan_data/outcomes.v3.csv" in tree:
            continue  # preserve v2 in Git, publish only the newest execution version
        location = "scan_data/" + name
        if location not in tree:
            if name in ("runs.csv", "setups.csv"):
                raise ValueError("Required scanner archive missing: " + name)
            continue
        raw = subprocess.check_output(["git", "show", commit + ":" + location])
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        required = (
            {
                "day",
                "as_of",
                "data_time",
                "run_time",
                "mode",
                "scanned",
                "failed",
                "flagged",
            }
            if name == "runs.csv"
            else (
                {
                    "day",
                    "as_of",
                    "data_time",
                    "run_time",
                    "mode",
                    "company",
                    "ticker",
                    "setup",
                    "direction",
                    "score",
                    "entry",
                    "stop",
                    "target",
                }
                if name == "setups.csv"
                else {"day", "ticker", "direction", "horizon", "status", "outcome"}
            )
        )
        if (
            not reader.fieldnames
            or len(reader.fieldnames) > 80
            or len(set(reader.fieldnames)) != len(reader.fieldnames)
            or not required.issubset(reader.fieldnames)
        ):
            raise ValueError("Invalid archive header: " + name)
        rows, total = deque(maxlen=limit), 0
        for row in reader:
            if None in row or any(
                value is None or len(value) > 16000 for value in row.values()
            ):
                raise ValueError("Invalid archive row: " + name)
            if any(
                "synthetic" in row.get(k, "").lower()
                or "demo" in row.get(k, "").lower()
                for k in ("mode", "source", "source_kind")
            ):
                raise ValueError(
                    "Fictional records cannot be published as collected scans"
                )
            total += 1
            rows.append(
                {
                    **row,
                    "_source_file": name,
                    **(
                        {"_run_id": run_id(row)}
                        if name in ("runs.csv", "setups.csv")
                        else {}
                    ),
                }
            )
        tables[name] = list(rows)
        sources.append(
            {
                "name": name,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "total_rows": total,
                "included_rows": len(rows),
                "truncated": total > len(rows),
                "validation": (
                    f"corrected-cache-{name.split('.')[1]}; separate scanner model"
                    if name in ("outcomes.v2.csv", "outcomes.v3.csv")
                    else (
                        "legacy-unverified"
                        if name == "outcomes.csv"
                        else "scanner-observations"
                    )
                ),
            }
        )
    runs = tables["runs.csv"]
    for row in runs:
        try:
            row["_collected_at"] = (
                dt.datetime.strptime(row["run_time"], "%Y-%m-%d %H:%M:%S")
                .replace(tzinfo=ZoneInfo("Europe/London"))
                .astimezone(dt.timezone.utc)
                .isoformat()
            )
            data_time = dt.datetime.fromisoformat(row["data_time"])
            if data_time.tzinfo is None:
                raise ValueError("Candle timestamp needs an offset")
            for key in ("scanned", "failed", "flagged"):
                if int(row[key]) < 0:
                    raise ValueError("Negative count")
        except (KeyError, ValueError) as error:
            raise ValueError("Invalid scanner run timestamp or counts") from error
    runs.sort(key=lambda row: row["_collected_at"])
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Publication clock must be timezone-aware")
    snapshot = {
        "schema_version": 1,
        "kind": "scanner-archive",
        "synthetic": False,
        "published_at": now.astimezone(dt.timezone.utc).isoformat(),
        "source": {
            "repository": REPOSITORY,
            "branch": "main",
            "commit_sha": commit,
            "commit_sequence": int(git("rev-list", "--count", commit)),
            "committed_at": git("show", "-s", "--format=%cI", commit),
            "last_scan_at": runs[-1]["_collected_at"] if runs else None,
            "last_data_time": runs[-1]["data_time"] if runs else None,
            "files": sources,
        },
        "upstream": {
            "workflow": os.environ.get(
                "UPSTREAM_WORKFLOW", "Manual archive publication"
            ),
            "conclusion": os.environ.get("UPSTREAM_CONCLUSION", "not-observed"),
            "run_id": os.environ.get("UPSTREAM_RUN_ID") or None,
        },
        "runs": runs,
        "setups": tables["setups.csv"],
        "outcomes": tables.get("outcomes.csv", []) + tables.get("outcomes.v3.csv", tables.get("outcomes.v2.csv", [])),
        "limitations": [
            "Actual stored scanner records, not live share quotes.",
            "Publication time is separate from scan time and source candle time.",
            "CSV price levels have no declared currency unit; values are displayed exactly as recorded.",
            "Scanner outcomes are separate from the corrected automatic paper-trading ledger.",
            "Missing candles cannot be reconstructed from setup CSVs.",
        ],
    }
    for name in ("setups", "outcomes"):
        while (
            len(json.dumps(snapshot, ensure_ascii=False).encode()) > 1_800_000
            and snapshot[name]
        ):
            snapshot[name].pop(0)
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > 1_800_000:
        raise ValueError("Scanner snapshot exceeds the transfer budget")
    for source in sources:
        collection = (
            runs
            if source["name"] == "runs.csv"
            else (
                snapshot["setups"]
                if source["name"] == "setups.csv"
                else snapshot["outcomes"]
            )
        )
        source["included_rows"] = sum(
            row["_source_file"] == source["name"] for row in collection
        )
        source["truncated"] = source["included_rows"] < source["total_rows"]
    return snapshot


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def publish(snapshot):
    from urllib.parse import urlsplit

    origin = os.environ.get("FTSE_SCANS_SITE_URL", "")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("Configure the HTTPS FTSE_SCANS_SITE_URL origin")
    token, service = os.environ.get("FTSE_SCANS_WRITE_TOKEN", ""), os.environ.get(
        "FTSE_SCANS_SITE_ACCESS", ""
    )
    if len(token) < 32 or not service:
        raise ValueError("Both dedicated scanner publication credentials are required")
    headers = {
        "Authorization": "Bearer " + token,
        "OAI-Sites-Authorization": "Bearer " + service,
        "Content-Type": "application/json",
    }
    opener = urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(
        origin.rstrip("/") + "/api/scans/ingest",
        data=json.dumps(snapshot, allow_nan=False).encode(),
        headers=headers,
        method="POST",
    )
    with opener.open(request, timeout=30) as response:
        ack = json.load(response)
    if ack.get("status") not in ("accepted", "duplicate") or not re.fullmatch(
        r"[a-f0-9]{64}", ack.get("revision", "")
    ):
        raise ValueError("Publication was not acknowledged")
    request = urllib.request.Request(
        origin.rstrip("/") + "/api/scans/status", headers=headers
    )
    with opener.open(request, timeout=30) as response:
        stored = json.load(response)
    if (
        stored.get("revision") != ack["revision"]
        or stored.get("commit_sha") != snapshot["source"]["commit_sha"]
    ):
        raise ValueError("Publication did not match persisted readback")
    return {
        "status": ack["status"],
        "revision": ack["revision"],
        "commit_sha": stored["commit_sha"],
        "readback_verified": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--out")
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    try:
        snapshot = build_snapshot(args.ref)
        if args.out:
            path = Path(args.out)
            if (
                path.resolve().is_relative_to(Path("scan_data").resolve())
                or path.suffix.lower() != ".json"
            ):
                raise ValueError("Use a separate JSON output outside scan_data")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(snapshot, indent=2, allow_nan=False))
        result = (
            publish(snapshot)
            if args.push
            else {
                "status": "prepared",
                "commit_sha": snapshot["source"]["commit_sha"],
                "runs": len(snapshot["runs"]),
                "setups": len(snapshot["setups"]),
                "outcomes": len(snapshot["outcomes"]),
            }
        )
        print(json.dumps(result))
        return 0
    except Exception as error:
        print(
            "Scanner publication failed ("
            + type(error).__name__
            + "). Check source validation, credentials and endpoint; no scan was run.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
