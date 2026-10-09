"""The run ledger: schedule slots, idempotent job runs, missed slots.

Every run is keyed by (job, slot), the UTC minute its schedule fired for,
so a second run for the same slot (a retry, a Run now, two hosts) is a
no-op."""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import psycopg

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    job text NOT NULL,
    slot timestamptz NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running',
    rows_affected integer NOT NULL DEFAULT 0,
    error text NOT NULL DEFAULT '',
    PRIMARY KEY (job, slot)
);
CREATE TABLE IF NOT EXISTS ticks (slot timestamptz PRIMARY KEY, at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS rollups (hour timestamptz PRIMARY KEY, ticks integer NOT NULL, updated_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS reports (
    day date PRIMARY KEY, ticks integer NOT NULL, runs_ok integer NOT NULL,
    runs_failed integer NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS zoo_probe (id bigserial PRIMARY KEY, token text NOT NULL);
"""

KEEP_DAYS = 7


@dataclass(frozen=True)
class Job:
    name: str
    period: timedelta  # matches its [cron] schedule in ox.toml
    work: Callable[[psycopg.Connection, datetime], int]


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, connect_timeout=5,
                           options="-c statement_timeout=5000 -c timezone=UTC")


def slot_for(job: Job, now: datetime) -> datetime:
    """The slot a run at `now` belongs to: now floored to the job's period."""
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return epoch + ((now.astimezone(timezone.utc) - epoch) // job.period) * job.period


def tick(conn: psycopg.Connection, slot: datetime) -> int:
    return conn.execute("INSERT INTO ticks (slot) VALUES (%s) ON CONFLICT DO NOTHING", (slot,)).rowcount


def rollup(conn: psycopg.Connection, slot: datetime) -> int:
    # Recount the last two hours of ticks into hourly rows.
    return conn.execute(
        """INSERT INTO rollups (hour, ticks, updated_at)
           SELECT date_trunc('hour', slot), count(*), now() FROM ticks
           WHERE slot >= date_trunc('hour', %s::timestamptz) - interval '1 hour'
           GROUP BY 1
           ON CONFLICT (hour) DO UPDATE SET ticks = EXCLUDED.ticks, updated_at = EXCLUDED.updated_at""",
        (slot,)).rowcount


def prune(conn: psycopg.Connection, slot: datetime) -> int:
    cutoff = slot - timedelta(days=KEEP_DAYS)
    n = 0
    for table, col in (("ticks", "slot"), ("rollups", "hour"), ("runs", "slot")):
        n += conn.execute(f"DELETE FROM {table} WHERE {col} < %s", (cutoff,)).rowcount
    return n


def report(conn: psycopg.Connection, slot: datetime) -> int:
    # The day that just ended.
    day_start = slot - timedelta(days=1)
    return conn.execute(
        """INSERT INTO reports (day, ticks, runs_ok, runs_failed)
           SELECT %(d)s::date,
                  (SELECT count(*) FROM ticks WHERE slot >= %(s)s AND slot < %(e)s),
                  (SELECT count(*) FROM runs WHERE status = 'ok' AND slot >= %(s)s AND slot < %(e)s),
                  (SELECT count(*) FROM runs WHERE status = 'failed' AND slot >= %(s)s AND slot < %(e)s)
           ON CONFLICT (day) DO NOTHING""",
        {"d": day_start.date(), "s": day_start, "e": slot}).rowcount


JOBS = {j.name: j for j in [
    Job("tick", timedelta(minutes=1), tick),
    Job("rollup", timedelta(minutes=5), rollup),
    Job("prune", timedelta(hours=1), prune),
    Job("report", timedelta(days=1), report),
]}


def claim(conn: psycopg.Connection, job: str, slot: datetime) -> bool:
    """Insert the run row; False when the slot already has one that did not
    fail (a failed slot may be run again)."""
    return conn.execute("""INSERT INTO runs (job, slot) VALUES (%s, %s)
                           ON CONFLICT (job, slot) DO UPDATE SET status = 'running', started_at = now(),
                           finished_at = NULL, error = '' WHERE runs.status = 'failed'""",
                        (job, slot)).rowcount == 1


def run(job: Job, now: datetime | None = None) -> str:
    """Runs one job for its current slot, once. The claim and the work commit
    together; a failure rolls the work back and records the failed run."""
    slot = slot_for(job, now or datetime.now(timezone.utc))
    with connect() as conn:
        try:
            with conn.transaction():
                if not claim(conn, job.name, slot):
                    return f"{job.name} {slot:%Y-%m-%dT%H:%MZ}: already recorded, nothing to do"
                n = job.work(conn, slot)
                conn.execute("UPDATE runs SET finished_at = now(), status = 'ok', rows_affected = %s "
                             "WHERE job = %s AND slot = %s", (n, job.name, slot))
        except Exception as e:
            with conn.transaction():
                conn.execute("""INSERT INTO runs (job, slot, finished_at, status, error) VALUES (%s, %s, now(), 'failed', %s)
                                ON CONFLICT (job, slot) DO UPDATE SET finished_at = now(), status = 'failed',
                                error = EXCLUDED.error WHERE runs.status <> 'ok'""",
                             (job.name, slot, f"{type(e).__name__}: {e}"[:300]))
            raise
    return f"{job.name} {slot:%Y-%m-%dT%H:%MZ}: ok, {n} rows"


def expected_slots(job: Job, since: datetime, until: datetime) -> list[datetime]:
    """Slots that should have run in [since, until), excluding the current one."""
    out, s = [], slot_for(job, since)
    if s < since:
        s += job.period
    last = slot_for(job, until)
    while s < last:
        out.append(s)
        s += job.period
    return out


def missed(job: Job, recorded: set[datetime], first: datetime | None, now: datetime, window: timedelta) -> list[datetime]:
    """Slots in the window with no run row, counted from the first run (the
    deploy), so a fresh project shows no misses for before it existed."""
    if first is None:
        return []
    since = max(first, now - window)
    return [s for s in expected_slots(job, since, now) if s not in recorded]
