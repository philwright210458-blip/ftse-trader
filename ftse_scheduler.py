"""Session-aware scheduling with persistent job claims; never installs a cron job."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from ftse_sessions import is_session, session_open_close

UK = ZoneInfo("Europe/London")
UTC = dt.timezone.utc


@dataclass(frozen=True)
class DueJob:
    key: str
    kind: str
    session: str
    scheduled_at: str
    missed: bool = False


def aware(value: dt.datetime | None = None) -> dt.datetime:
    value = value or dt.datetime.now(UTC)
    if value.tzinfo is None:
        raise ValueError("An explicit timezone is required")
    return value.astimezone(UTC)


def due_jobs(now: dt.datetime, config: dict[str, Any]) -> list[DueJob]:
    now = aware(now)
    day = now.astimezone(UK).date()
    if not is_session(day):
        return []
    settings = config.get("schedule", {})
    interval = int(config.get("research", {}).get("interval_minutes", 15))
    if interval != 15:
        raise ValueError("The research collector currently supports fifteen-minute bars only")
    opening, closing = [x.to_pydatetime().astimezone(UTC) for x in session_open_close(day)]
    pre = opening - dt.timedelta(minutes=int(settings.get("pre_open_minutes", 15)))
    grace = dt.timedelta(seconds=int(settings.get("bar_grace_seconds", 90)))
    max_late = dt.timedelta(seconds=int(settings.get("max_lateness_seconds", 180)))
    entries = [("prepare", pre), ("opening", opening)]
    boundary = opening + dt.timedelta(minutes=interval)
    while boundary <= closing:
        entries.append(("watch", boundary + grace))
        boundary += dt.timedelta(minutes=interval)
    entries.append(("resolve", closing + dt.timedelta(minutes=int(settings.get("close_grace_minutes", 15)))))
    result = []
    fingerprint = hashlib.sha256(json.dumps(config.get("research", {}), sort_keys=True).encode()).hexdigest()[:12]
    for kind, stamp in entries:
        if stamp <= now:
            key = f"{day}|{kind}|{stamp.isoformat()}|{fingerprint}"
            result.append(DueJob(key, kind, day.isoformat(), stamp.isoformat(), now - stamp > max_late))
    return result


class JobClaims:
    """SQLite claims serialize overlapping collectors across processes on one data volume."""

    def __init__(self, path: str):
        self.owner = uuid.uuid4().hex
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS scheduler_jobs(
            job_key TEXT PRIMARY KEY,kind TEXT NOT NULL,session TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,status TEXT NOT NULL,claimed_at TEXT,
            finished_at TEXT,error TEXT,owner TEXT)""")
        if "owner" not in {r[1] for r in self.db.execute("PRAGMA table_info(scheduler_jobs)")}:
            self.db.execute("ALTER TABLE scheduler_jobs ADD COLUMN owner TEXT")
        self.db.commit()

    def claim(self, job: DueJob, now: dt.datetime, lease_seconds: int = 1200, max_lateness_seconds: int = 180) -> bool:
        now = aware(now)
        missed = job.missed or now - dt.datetime.fromisoformat(job.scheduled_at) > dt.timedelta(
            seconds=max_lateness_seconds
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            previous = self.db.execute(
                "SELECT status,claimed_at FROM scheduler_jobs WHERE job_key=?", (job.key,)
            ).fetchone()
            if previous:
                status, claimed_at = previous
                if status in ("done", "missed", "failed"):
                    self.db.rollback()
                    return False
                if (
                    status == "running"
                    and claimed_at
                    and now - dt.datetime.fromisoformat(claimed_at) < dt.timedelta(seconds=lease_seconds)
                ):
                    self.db.rollback()
                    return False
            state = "missed" if missed else "running"
            self.db.execute(
                """INSERT INTO scheduler_jobs(job_key,kind,session,scheduled_at,status,claimed_at,error,owner)
                 VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_key) DO UPDATE SET status=excluded.status,
                 claimed_at=excluded.claimed_at,error=excluded.error,owner=excluded.owner""",
                (
                    job.key,
                    job.kind,
                    job.session,
                    job.scheduled_at,
                    state,
                    now.isoformat(),
                    "Missed scheduled window; no retrospective paper entries" if missed else None,
                    self.owner,
                ),
            )
            self.db.commit()
            return not missed
        except Exception:
            self.db.rollback()
            raise

    def finish(self, job: DueJob, now: dt.datetime, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE scheduler_jobs SET status=?,finished_at=?,error=? WHERE job_key=? AND owner=? AND status='running'",
            ("failed" if error else "done", aware(now).isoformat(), error, job.key, self.owner),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()
