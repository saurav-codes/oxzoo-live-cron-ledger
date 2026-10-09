import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import ledger

UTC = timezone.utc


def at(*args):
    return datetime(*args, tzinfo=UTC)


def test_slots_floor_to_the_schedule():
    now = at(2026, 10, 9, 13, 47, 31)
    assert ledger.slot_for(ledger.JOBS["tick"], now) == at(2026, 10, 9, 13, 47)
    assert ledger.slot_for(ledger.JOBS["rollup"], now) == at(2026, 10, 9, 13, 45)
    assert ledger.slot_for(ledger.JOBS["prune"], now) == at(2026, 10, 9, 13)
    assert ledger.slot_for(ledger.JOBS["report"], now) == at(2026, 10, 9)
    # a late start within the same minute keeps the slot
    assert ledger.slot_for(ledger.JOBS["tick"], at(2026, 10, 9, 13, 47, 59)) == at(2026, 10, 9, 13, 47)


def test_missed_slots_count_from_first_run():
    job = ledger.JOBS["rollup"]
    now = at(2026, 10, 9, 13, 2)
    first = at(2026, 10, 9, 12, 30)
    recorded = {at(2026, 10, 9, 12, m) for m in (30, 35, 45, 50, 55)}
    assert ledger.missed(job, recorded, first, now, timedelta(hours=24)) == [at(2026, 10, 9, 12, 40)]
    assert ledger.missed(job, set(), None, now, timedelta(hours=24)) == []
    # the current slot (13:00) is not counted until it has passed
    assert at(2026, 10, 9, 13) not in ledger.expected_slots(job, first, now)


def test_window_caps_old_misses():
    job = ledger.JOBS["tick"]
    now = at(2026, 10, 9, 12)
    assert len(ledger.missed(job, set(), at(2026, 10, 1), now, timedelta(hours=1))) == 60


needs_pg = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL")


@needs_pg
def test_runs_are_idempotent_and_failures_retry(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    subprocess.run([sys.executable, "migrate.py"], check=True)
    with ledger.connect() as conn:
        conn.execute("TRUNCATE runs, ticks, rollups, reports")
    now = at(2026, 10, 9, 12, 5, 10)
    assert "ok, 1 rows" in ledger.run(ledger.JOBS["tick"], now)
    assert "already recorded" in ledger.run(ledger.JOBS["tick"], now + timedelta(seconds=30))
    assert "ok, 1 rows" in ledger.run(ledger.JOBS["rollup"], now)

    def broken(conn, slot):
        conn.execute("INSERT INTO ticks (slot) VALUES (%s)", (slot,))
        raise RuntimeError("boom")

    job = ledger.Job("tick", timedelta(minutes=1), broken)
    later = now + timedelta(minutes=1)
    with pytest.raises(RuntimeError):
        ledger.run(job, later)
    with ledger.connect() as conn:
        assert conn.execute("SELECT status FROM runs WHERE job='tick' AND slot=%s", (ledger.slot_for(job, later),)).fetchone()[0] == "failed"
        assert conn.execute("SELECT count(*) FROM ticks").fetchone()[0] == 1  # the failed work rolled back
    assert "ok, 1 rows" in ledger.run(ledger.JOBS["tick"], later)  # a failed slot runs again
    assert "ok" in ledger.run(ledger.JOBS["prune"], now + timedelta(days=9))
    with ledger.connect() as conn:
        assert conn.execute("SELECT count(*) FROM ticks").fetchone()[0] == 0
