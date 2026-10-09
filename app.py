"""cron-ledger: a Flask page over the run ledger the four cron jobs write."""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, render_template, request

import ledger
import zoo

NAME = "cron-ledger"
STACK = "Flask + gunicorn"
WINDOW = timedelta(hours=24)
TICK_MAX_AGE_S = 180
ROLLUP_MAX_AGE_S = 11 * 60

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)


CORS_PATHS = ("/_zoo/health", "/_zoo/probe")


@app.before_request
def preflight():
    if request.method == "OPTIONS" and request.path in CORS_PATHS:
        return Response(status=204, headers=zoo.preflight_headers(request.headers.get("Origin")))


@app.after_request
def cors(resp: Response) -> Response:
    if request.path in CORS_PATHS and request.method != "OPTIONS":
        resp.headers.update(zoo.cors_headers(request.headers.get("Origin")))
    return resp


def job_summaries(conn, now: datetime) -> list[dict]:
    rows = conn.execute("SELECT job, slot, started_at, finished_at, status, rows_affected, error FROM runs "
                        "WHERE slot >= %s ORDER BY slot DESC", (now - WINDOW - timedelta(days=1),)).fetchall()
    firsts = dict(conn.execute("SELECT job, min(slot) FROM runs GROUP BY job").fetchall())
    out = []
    for job in ledger.JOBS.values():
        runs = [r for r in rows if r[0] == job.name]
        miss = ledger.missed(job, {r[1] for r in runs}, firsts.get(job.name), now, WINDOW)
        last = runs[0] if runs else None
        out.append({
            "job": job.name, "every": str(job.period), "missed": len(miss),
            "missed_recent": [f"{s:%Y-%m-%d %H:%M}Z" for s in miss[-5:]],
            "last_age_s": int((now - last[1]).total_seconds()) if last else None,
            "last_status": last[4] if last else "never",
            "runs": [{"slot": f"{r[1]:%Y-%m-%d %H:%M}Z", "status": r[4], "rows": r[5],
                      "ms": int((r[3] - r[2]).total_seconds() * 1000) if r[3] else None, "error": r[6]}
                     for r in runs[:10]],
        })
    return out


@app.get("/")
def index():
    now = datetime.now(timezone.utc)
    with ledger.connect() as conn:
        jobs = job_summaries(conn, now)
        rollups = conn.execute("SELECT hour, ticks FROM rollups ORDER BY hour DESC LIMIT 12").fetchall()
        reports = conn.execute("SELECT day, ticks, runs_ok, runs_failed FROM reports ORDER BY day DESC LIMIT 7").fetchall()
    return render_template("index.html", jobs=jobs, rollups=rollups, reports=reports, now=now)


@app.get("/api/runs")
def api_runs():
    with ledger.connect() as conn:
        return jsonify({"jobs": job_summaries(conn, datetime.now(timezone.utc))})


@app.get("/_zoo/health")
def health():
    return jsonify(zoo.health(NAME, STACK))


def check_postgres() -> str:
    token = secrets.token_hex(8)
    with ledger.connect() as conn, conn.transaction():
        row_id = conn.execute("INSERT INTO zoo_probe (token) VALUES (%s) RETURNING id", (token,)).fetchone()[0]
        got = conn.execute("SELECT token FROM zoo_probe WHERE id = %s", (row_id,)).fetchone()[0]
        conn.execute("DELETE FROM zoo_probe WHERE id = %s", (row_id,))
        version = conn.execute("SHOW server_version").fetchone()[0]
    if got != token:
        raise RuntimeError("read back a different token")
    return f"zoo_probe row round trip, server {version.split()[0]}"


def last_ok_age(job: str) -> float | None:
    with ledger.connect() as conn:
        row = conn.execute("SELECT max(slot) FROM runs WHERE job = %s AND status = 'ok'", (job,)).fetchone()
    return None if row[0] is None else (datetime.now(timezone.utc) - row[0]).total_seconds()


def fresh(job: str, max_age_s: int) -> str:
    age = last_ok_age(job)
    if age is None:
        raise RuntimeError(f"no ok {job} run recorded yet; is cron running?")
    if age > max_age_s:
        raise RuntimeError(f"last ok {job} slot is {int(age)}s old (limit {max_age_s}s); is cron running?")
    return f"last ok {job} slot {int(age)}s ago"


def check_idempotency() -> str:
    slot = datetime(2000, 1, 1, tzinfo=timezone.utc)
    with ledger.connect() as conn:
        tx = conn.transaction(force_rollback=True)
        with tx:
            first = ledger.claim(conn, "zoo-selftest", slot)
            second = ledger.claim(conn, "zoo-selftest", slot)
    if (first, second) != (True, False):
        raise RuntimeError(f"claims returned {first}, {second}; want True, False")
    return "same slot claimed twice in a rolled-back transaction: first wrote, second was a no-op"


prober = zoo.Prober(
    NAME, STACK,
    lambda: [
        zoo.Check("postgres", "Postgres write, read, delete", check_postgres, ["DATABASE_URL"]),
        zoo.Check("cron:tick", "tick ran in the last 3 minutes", lambda: fresh("tick", TICK_MAX_AGE_S), ["DATABASE_URL"]),
        zoo.Check("cron:rollup", "rollup ran in the last 11 minutes", lambda: fresh("rollup", ROLLUP_MAX_AGE_S), ["DATABASE_URL"]),
        zoo.Check("idempotency", "Same slot twice is a no-op", check_idempotency, ["DATABASE_URL"]),
    ],
    lambda: [zoo.var("DATABASE_URL", "service"), zoo.var("ZOO_PANEL_ORIGIN", "plain")],
)


@app.get("/_zoo/probe")
def probe():
    status, body = prober.run()
    return jsonify(body), status
