"""SQLite research journal; immutable first observations and transactional ledger."""

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3

from .models import Candle, iso, key, serialise, stamp

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS universe (ticker TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candles (
 ticker TEXT NOT NULL, interval TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
 open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
 volume REAL NOT NULL, fetched_at TEXT NOT NULL, available_at TEXT NOT NULL,
 quote_unit TEXT NOT NULL, adjusted_close REAL, source TEXT NOT NULL,
 PRIMARY KEY(ticker,interval,start));
CREATE INDEX IF NOT EXISTS candles_available ON candles(available_at);
CREATE TABLE IF NOT EXISTS candle_revisions (
 id TEXT PRIMARY KEY, ticker TEXT NOT NULL, interval TEXT NOT NULL, start TEXT NOT NULL,
 observed_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS candle_revisions_identity ON candle_revisions(ticker,interval,start,observed_at);
CREATE INDEX IF NOT EXISTS candle_revisions_observed ON candle_revisions(ticker,interval,observed_at);
CREATE TABLE IF NOT EXISTS signals (
 id TEXT PRIMARY KEY, ticker TEXT NOT NULL, strategy TEXT NOT NULL, direction TEXT NOT NULL,
 session TEXT NOT NULL, signal_at TEXT NOT NULL, available_at TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
 id TEXT PRIMARY KEY, signal_id TEXT NOT NULL UNIQUE REFERENCES signals(id),
 ticker TEXT NOT NULL, strategy TEXT NOT NULL, direction TEXT NOT NULL,
 session TEXT NOT NULL, status TEXT NOT NULL, entry_at TEXT NOT NULL,
 exit_at TEXT, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS trades_state ON trades(status,entry_at);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT NOT NULL,
 status TEXT NOT NULL, universe_hash TEXT NOT NULL, config_hash TEXT NOT NULL,
 payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watches (
 id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 ticker TEXT NOT NULL, session TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluation_samples (
 id TEXT PRIMARY KEY, session TEXT NOT NULL, ticker TEXT NOT NULL,
 at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_records (
 id TEXT PRIMARY KEY, source TEXT NOT NULL, source_hash TEXT NOT NULL,
 row_number INTEGER NOT NULL, payload TEXT NOT NULL);
"""


class ResearchStore:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        if self.path != ":memory:":
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(SCHEMA)
        self.connection.execute("INSERT OR IGNORE INTO metadata VALUES('schema_version','1')")

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def rows(self, sql, args=()):
        return [dict(row) for row in self.connection.execute(sql, args)]

    def metadata(self, name, default=None):
        row = self.connection.execute("SELECT value FROM metadata WHERE key=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_metadata(self, name, value):
        self.connection.execute(
            "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (name, serialise(value)),
        )

    def universe(self):
        return [json.loads(row["payload"]) for row in self.rows("SELECT payload FROM universe ORDER BY ticker")]

    def set_universe(self, records):
        with self.transaction():
            self.connection.execute("DELETE FROM universe")
            for record in records:
                if not record.get("ticker") or record.get("quote_unit") not in {"GBp", "GBP"}:
                    raise ValueError("Universe requires ticker and explicit GBp/GBP quote_unit")
                self.connection.execute("INSERT INTO universe VALUES(?,?)", (record["ticker"], serialise(record)))

    def ingest_candles(self, records):
        counts = {"inserted": 0, "duplicates": 0, "revisions": 0, "incomplete": 0}
        with self.transaction():
            for record in records:
                candle = record if isinstance(record, Candle) else Candle(**record)
                candle.validate()
                if stamp(candle.end) > stamp(candle.fetched_at):
                    counts["incomplete"] += 1
                    continue
                data = candle.to_dict()
                existing = self.connection.execute(
                    "SELECT * FROM candles WHERE ticker=? AND interval=? AND start=?",
                    (data["ticker"], data["interval"], data["start"]),
                ).fetchone()
                if existing:
                    price_keys = ("open", "high", "low", "close", "volume", "adjusted_close", "quote_unit")
                    latest_revision = self.connection.execute(
                        "SELECT payload FROM candle_revisions WHERE ticker=? AND interval=? AND start=? ORDER BY observed_at DESC,rowid DESC LIMIT 1",
                        (data["ticker"], data["interval"], data["start"]),
                    ).fetchone()
                    latest = json.loads(latest_revision[0]) if latest_revision else dict(existing)
                    if stamp(existing["fetched_at"]) > stamp(latest["fetched_at"]):
                        latest = dict(existing)
                    changed = any(latest[name] != data[name] for name in price_keys)
                    # Daily adjustments are point-in-time observations. Keep
                    # later identical observations too: otherwise an imported
                    # older revision could erase a subsequently confirmed value.
                    preserve_observation = data["interval"] == "1d" and data["fetched_at"] != latest["fetched_at"]
                    if changed or preserve_observation:
                        revision_id = key(
                            "revision",
                            data["fetched_at"],
                            serialise({name: data[name] for name in price_keys}),
                            data["ticker"],
                            data["interval"],
                            data["start"],
                        )
                        cursor = self.connection.execute(
                            "INSERT OR IGNORE INTO candle_revisions VALUES(?,?,?,?,?,?)",
                            (
                                revision_id,
                                data["ticker"],
                                data["interval"],
                                data["start"],
                                data["fetched_at"],
                                serialise(data),
                            ),
                        )
                        if changed and cursor.rowcount:
                            counts["revisions"] += 1
                        else:
                            counts["duplicates"] += 1
                    else:
                        counts["duplicates"] += 1
                    continue
                names = list(data)
                self.connection.execute(
                    f"INSERT INTO candles ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                    tuple(data[name] for name in names),
                )
                counts["inserted"] += 1
        return counts

    def candles(self, ticker, interval="15m", as_of=None, since=None):
        clauses = []
        args = [ticker, interval]
        if as_of is not None:
            clauses.append("available_at<=?")
            args.append(iso(as_of))
        if since is not None:
            clauses.append("start>=?")
            args.append(iso(since))
        clause = (" AND " + " AND ".join(clauses)) if clauses else ""
        return self.rows(
            "SELECT * FROM candles WHERE ticker=? AND interval=?" + clause + " ORDER BY start", tuple(args)
        )

    def daily_as_of(self, ticker, as_of):
        """Reconcile adjustment revisions available by the observation time only.

        Daily split/dividend-adjusted values may be revised by the provider. Pairing
        fresh and old adjustment bases would manufacture overnight movers.
        Original rows and every changed observation remain in the journal.
        """
        result = {row["start"]: row for row in self.candles(ticker, "1d", as_of)}
        for row in self.rows(
            "SELECT payload FROM candle_revisions WHERE ticker=? AND interval='1d' AND observed_at<=? ORDER BY observed_at,rowid",
            (ticker, iso(as_of)),
        ):
            data = json.loads(row["payload"])
            current = result.get(data["start"])
            if current is None or stamp(data["fetched_at"]) >= stamp(current["fetched_at"]):
                result[data["start"]] = data
        return sorted(result.values(), key=lambda row: row["start"])

    def payloads(self, table, where="", args=()):
        if table not in {"signals", "trades", "runs", "watches", "events", "evaluation_samples"}:
            raise ValueError("Unsupported payload table")
        return [json.loads(row["payload"]) for row in self.rows(f"SELECT payload FROM {table} {where}", args)]

    def event(self, identifier, at, kind, payload):
        self.connection.execute(
            "INSERT OR IGNORE INTO events VALUES(?,?,?,?)",
            (identifier, iso(at), kind, serialise({"id": identifier, "at": iso(at), "kind": kind, **payload})),
        )

    def backup(self, destination):
        """SQLite backup API includes committed WAL contents, unlike copying the DB file."""
        target = sqlite3.connect(str(destination))
        try:
            self.connection.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError("Backup integrity check failed")
        finally:
            target.close()
        return str(destination)
