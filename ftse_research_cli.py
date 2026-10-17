#!/usr/bin/env python3
"""Research-only runner. No broker access and no scheduler is installed automatically."""

from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from ftse_scheduler import JobClaims, aware, due_jobs

ROOT = Path(__file__).resolve().parent


def load_config(path: str | Path) -> dict:
    path = Path(path)
    config = json.loads(path.read_text())
    if type(config.get("enabled")) is not bool:
        raise ValueError("Configuration must contain an explicit enabled boolean")
    if int(config.get("watch_top", 15)) < 1:
        raise ValueError("watch_top must be positive")
    schedule = config.get("schedule", {})
    for name, default in [
        ("pre_open_minutes", 15),
        ("bar_grace_seconds", 90),
        ("close_grace_minutes", 15),
        ("poll_seconds", 30),
        ("max_lateness_seconds", 180),
        ("lease_seconds", 1200),
    ]:
        value = schedule.get(name, default)
        if type(value) not in (int, float) or not 0 < value <= 86400:
            raise ValueError(f"Invalid schedule setting: {name}")
    return config


def write_json(path: str | Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, allow_nan=False, indent=2).encode()
    handle, temporary = tempfile.mkstemp(prefix="snapshot-", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def push_snapshot(snapshot: dict, site_url: str | None = None, token: str | None = None) -> dict:
    site_url = site_url or os.environ.get("FTSE_SITE_URL", "")
    token = token or os.environ.get("FTSE_INGEST_TOKEN", "")
    parsed = urllib.parse.urlsplit(site_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Set FTSE_SITE_URL to the Site origin without credentials, query or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("FTSE_SITE_URL must be an origin without a path")
    if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Background updates require HTTPS outside localhost")
    if len(token) < 32:
        raise ValueError("FTSE_INGEST_TOKEN must contain at least 32 characters")
    payload = json.dumps(snapshot, allow_nan=False, separators=(",", ":")).encode()
    request = urllib.request.Request(
        site_url.rstrip("/") + "/api/ingest",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "Idempotency-Key": hashlib.sha256(payload).hexdigest(),
        },
    )
    service = os.environ.get("FTSE_SITES_SERVICE_TOKEN")
    if service:
        request.add_header("OAI-Sites-Authorization", "Bearer " + service)
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        result = json.load(response)
    revision = result.get("revision")
    if (
        result.get("status") not in ("accepted", "duplicate")
        or not isinstance(revision, str)
        or len(revision) != 64
        or any(c not in "0123456789abcdef" for c in revision)
    ):
        raise ValueError("Publication did not return an accepted snapshot acknowledgement")
    expected = dt.datetime.fromisoformat(snapshot["generated_at"].replace("Z", "+00:00"))
    try:
        received = dt.datetime.fromisoformat(result["generated_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        raise ValueError("Publication acknowledgement lacks a valid timestamp")
    if abs((expected - received).total_seconds()) > 0.001:
        raise ValueError("Publication acknowledgement refers to another snapshot")
    verification = urllib.request.Request(
        site_url.rstrip("/") + "/api/ingest-status",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    if service:
        verification.add_header("OAI-Sites-Authorization", "Bearer " + service)
    with urllib.request.build_opener(NoRedirect).open(verification, timeout=30) as response:
        stored = json.load(response)
    if stored.get("revision") != revision or stored.get("generated_at") != result["generated_at"]:
        raise ValueError("Published snapshot did not match durable readback")
    return {**result, "readback_verified": True}


def universe(config: dict) -> list[dict]:
    path = Path(config.get("universe", "ftse_universe.csv"))
    with path.open(newline="", encoding="utf-8") as source:
        records = []
        for row in csv.DictReader(source):
            kind = (row.get("type") or "company").strip().lower()
            if config.get("exclude_trusts", True) and kind == "trust":
                continue
            records.append(
                {
                    "ticker": row["yahoo_ticker"],
                    "company": row["company"],
                    "index": row["index"],
                    "kind": kind,
                    "quote_unit": row.get("quote_unit") or "GBp",
                }
            )
    return records


def open_engine(config: dict, database: str | None = None, demo: bool = False, readonly: bool = False):
    from ftse_research import ResearchConfig, ResearchStore, ResearchEngine

    values = dict(config.get("research", {}))
    if demo:
        values["mode"] = "synthetic-demo"
    store = ResearchStore(database or config.get("database", "runtime/research.sqlite"))
    if readonly:
        values = store.metadata("config", values)
    settings = ResearchConfig.from_dict(values)
    return store, ResearchEngine(store, settings, persist_config=not readonly)


def selected_universe(engine, records: list[dict], top: int, now: dt.datetime) -> list[dict]:
    snapshot = engine.snapshot(now)
    session = snapshot.get("market", {}).get("session")
    tickers = {
        str(x.get("ticker"))
        for x in snapshot.get("watchlist", [])
        if x.get("is_mover")
        or abs(float(x.get("previous_change_pct", x.get("yesterday_pct", 0)) or 0))
        >= float(engine.config.mover_threshold_pct)
    }
    ranked = sorted(
        (
            x
            for x in snapshot.get("candidates", [])
            if x.get("session", session) == session
            and x.get("status", "pending") in ("pending", "eligible", "qualified")
        ),
        key=lambda x: float(x.get("score", 0)),
        reverse=True,
    )
    tickers.update(x.get("ticker") for x in ranked[:top])
    tickers.update(x.get("ticker") for x in snapshot.get("paper", {}).get("positions", []))
    # Empty first snapshot still needs a full collection to establish candidates.
    return [r for r in records if r["ticker"] in tickers] if tickers else records


@contextmanager
def runner_lock(path):
    # SQLite's durable files and this lock must share one local persistent volume.
    import fcntl

    with open(str(path) + ".collector.lock", "a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def tick(config: dict, database: str | None = None, push: bool = False) -> dict:
    if not config["enabled"]:
        return {"state": "disabled", "message": "No collection or schedule activation performed", "jobs": []}
    now = aware()
    path = database or config.get("database", "runtime/research.sqlite")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with runner_lock(path) as acquired:
        if not acquired:
            return {"state": "busy", "jobs": []}
        return _tick_locked(config, path, push, now)


def _tick_locked(config, path, push, now):
    claims = JobClaims(path)
    store, engine = open_engine(config, path)
    records = universe(config)
    if not store.universe():
        engine.set_universe(records)
    performed = []
    try:
        for job in due_jobs(now, config):
            if not claims.claim(
                job,
                aware(),
                int(config.get("schedule", {}).get("lease_seconds", 1200)),
                int(config.get("schedule", {}).get("max_lateness_seconds", 180)),
            ):
                continue
            try:
                seed_key = "first-watch-collected:" + job.session
                full_universe = job.kind in ("prepare", "opening") or (
                    job.kind == "watch" and not store.metadata(seed_key)
                )
                selected = (
                    records
                    if full_universe
                    else selected_universe(engine, records, int(config.get("watch_top", 15)), aware())
                )
                engine.collect_and_run(selected)
                snapshot = engine.snapshot()
                if (
                    job.kind == "watch"
                    and full_universe
                    and snapshot.get("health", {}).get("state") not in ("failed", "empty", "not-collected")
                ):
                    store.set_metadata(seed_key, True)
                write_json(config.get("snapshot", "runtime/snapshot.json"), snapshot)
                if push:
                    push_snapshot(snapshot)
                claims.finish(job, aware())
                performed.append({"kind": job.kind, "session": job.session, "status": "done"})
            except Exception as exc:
                # Preserve a failure marker independently of any successful previous snapshot.
                claims.finish(job, aware(), type(exc).__name__)
                try:
                    snapshot = engine.snapshot()
                    snapshot["health"] = dict(
                        snapshot.get("health", {}),
                        state="failed",
                        last_error="Collector or publication failed; inspect runner logs",
                    )
                    write_json(config.get("snapshot", "runtime/snapshot.json"), snapshot)
                    if push:
                        push_snapshot(snapshot)
                except Exception:
                    pass
                performed.append(
                    {"kind": job.kind, "session": job.session, "status": "failed", "error": type(exc).__name__}
                )
                break
        return {"state": "failed" if any(x["status"] == "failed" for x in performed) else "ok", "jobs": performed}
    finally:
        store.close()
        claims.close()


def backup(database: str, destination: str) -> dict:
    if Path(database).resolve() == Path(destination).resolve():
        raise ValueError("Backup must be a separate file")
    if Path(destination).exists():
        raise ValueError("Backup destination already exists")
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect("file:" + urllib.parse.quote(str(Path(database).resolve())) + "?mode=ro", uri=True) as source,
        sqlite3.connect(destination) as target,
    ):
        source.execute("BEGIN")
        source.execute("SELECT count(*) FROM sqlite_master").fetchone()
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity check failed")
        tables = [
            r[0]
            for r in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ]
        for table in tables:
            safe = '"' + table.replace('"', '""') + '"'
            a = source.execute("SELECT count(*) FROM " + safe).fetchone()[0]
            b = target.execute("SELECT count(*) FROM " + safe).fetchone()[0]
            if a != b:
                raise ValueError("Backup row reconciliation failed")
    return {"state": "verified", "tables": len(tables), "backup": destination}


def export_records(database, destination, table):
    allowed = {
        "candles",
        "candle_revisions",
        "signals",
        "trades",
        "events",
        "runs",
        "watches",
        "evaluation_samples",
        "universe",
        "metadata",
        "legacy_records",
    }
    if table not in allowed:
        raise ValueError("Unsupported journal export table")
    path = Path(destination)
    if path.exists():
        raise ValueError("Export destination already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect("file:" + urllib.parse.quote(str(Path(database).resolve())) + "?mode=ro", uri=True) as source:
        cursor = source.execute("SELECT * FROM " + table)
        with path.open("x", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow([x[0] for x in cursor.description])
            count = 0
            for row in cursor:
                writer.writerow(
                    [
                        (
                            "'" + value
                            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                            else value
                        )
                        for value in row
                    ]
                )
                count += 1
    return {"state": "exported", "table": table, "records": count, "path": str(path)}


def import_legacy(store, directory):
    """Append a content-addressed raw archive, never reinterpret an old outcome."""
    imported, duplicates = 0, 0
    with store.transaction():
        for name in ("runs.csv", "setups.csv", "outcomes.csv"):
            path = Path(directory) / name
            if not path.exists():
                continue
            payload = path.read_bytes()
            source_hash = hashlib.sha256(payload).hexdigest()
            with path.open(newline="", encoding="utf-8") as source:
                for number, row in enumerate(csv.DictReader(source), 1):
                    record = dict(row, validation="legacy-unverified")
                    identifier = hashlib.sha256(f"{name}|{source_hash}|{number}".encode()).hexdigest()
                    changed = store.connection.execute(
                        "INSERT OR IGNORE INTO legacy_records VALUES(?,?,?,?,?)",
                        (identifier, name, source_hash, number, json.dumps(record, sort_keys=True, allow_nan=False)),
                    ).rowcount
                    imported += changed
                    duplicates += 1 - changed
    return {
        "state": "imported",
        "inserted": imported,
        "duplicates": duplicates,
        "validation": "legacy-unverified; excluded from corrected paper results",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "init",
            "demo",
            "collect",
            "run",
            "snapshot",
            "evaluate",
            "plan",
            "tick",
            "service",
            "push",
            "backup",
            "export",
            "import-legacy",
        ],
    )
    parser.add_argument("--config", default=str(ROOT / "config/research.json"))
    parser.add_argument("--database")
    parser.add_argument("--out")
    parser.add_argument("--table", default="trades", help="Journal table for complete CSV export")
    parser.add_argument("--snapshot-file", help="Push a previously generated snapshot without modifying its journal")
    parser.add_argument("--legacy-dir", default=str(ROOT / "scan_data"), help="Original CSV archive directory")
    parser.add_argument("--at", help="Timezone-aware timestamp for schedule planning or synthetic demo only")
    parser.add_argument("--push", action="store_true", help="Send snapshots using environment-only credentials")
    parser.add_argument("--once", action="store_true", help="Service: run one tick and exit")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.at and args.command not in ("plan", "demo"):
            raise ValueError("--at is restricted to schedule planning and synthetic demos")
        if args.command == "plan":
            now = dt.datetime.fromisoformat(args.at) if args.at else aware()
            result = {
                "enabled": config["enabled"],
                "timezone": "Europe/London",
                "jobs": [vars(x) for x in due_jobs(now, config)],
            }
        elif args.command in ("tick", "service"):
            if args.command == "service" and not config["enabled"]:
                raise ValueError(
                    "Collector service is disabled in configuration; activation is a separate deployment step"
                )
            while True:
                result = tick(config, args.database, args.push)
                print(json.dumps(result, allow_nan=False), flush=True)
                if args.command == "tick" or args.once:
                    return 1 if result["state"] == "failed" else 0
                time.sleep(float(config.get("schedule", {}).get("poll_seconds", 30)))
        elif args.command == "backup":
            if not args.out:
                raise ValueError("--out is required for a separate backup file")
            result = backup(args.database or config["database"], args.out)
        elif args.command == "export":
            if not args.out:
                raise ValueError("--out is required for CSV export")
            result = export_records(args.database or config["database"], args.out, args.table)
        elif args.command == "push" and args.snapshot_file:
            result = push_snapshot(json.loads(Path(args.snapshot_file).read_text()))
        else:
            if args.command == "collect" and not config["enabled"]:
                raise ValueError("Collection is disabled; no live source requests made")
            if args.command == "demo" and not args.database:
                raise ValueError("Demo requires an explicit --database separate from the research ledger")
            store, engine = open_engine(
                config, args.database, args.command == "demo", args.command in ("snapshot", "evaluate", "push")
            )
            try:
                if args.command == "init":
                    engine.set_universe(universe(config))
                    result = {"state": "initialised", "mode": "paper"}
                elif args.command == "import-legacy":
                    result = import_legacy(store, args.legacy_dir)
                elif args.command == "demo":
                    result = engine.seed_demo(args.at)
                elif args.command == "collect":
                    result = engine.collect_and_run(universe(config))
                elif args.command == "run":
                    result = engine.run()
                elif args.command == "evaluate":
                    result = engine.evaluate()
                else:
                    result = engine.snapshot()
                    if args.command == "push":
                        result = push_snapshot(result)
                if args.command not in ("evaluate", "push") and args.out:
                    snapshot = result if args.command == "demo" else engine.snapshot()
                    write_json(args.out, snapshot)
                if args.push:
                    push_snapshot(engine.snapshot())
            finally:
                store.close()
        if args.out and args.command in ("plan", "evaluate"):
            write_json(args.out, result)
        print(json.dumps(result, allow_nan=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, sqlite3.Error, urllib.error.URLError) as exc:
        # Do not print exception text from a remote request that may contain credentials.
        if isinstance(exc, urllib.error.URLError):
            message = "Snapshot publication failed; verify endpoint, permissions and runner network"
        else:
            message = str(exc)
        print(f"FTSE research: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
