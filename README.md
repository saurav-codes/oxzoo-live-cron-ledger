# cron-ledger

> **Role in the zoo:** project `cron-ledger` of [oxzoo-live](https://github.com/saurav-codes/oxzoo-live-control/blob/main/zoo/README.md#projects), deployed with ox on server s2 at https://cron-ledger.s2.zoo.sorv.dev. The contract it follows is [DESIGN.md](https://github.com/saurav-codes/oxzoo-live-control/blob/main/zoo/DESIGN.md).

Flask + gunicorn over Postgres. Four ox cron jobs write a run ledger, and a
page shows each job's history and missed slots.

## What it proves

- ox `[cron]` runs four schedules on time: `tick` every minute, `rollup`
  every 5 minutes (hourly tick counts), `prune` hourly (keeps 7 days),
  `report` daily (yesterday's counts).
- Runs are idempotent: each run is keyed by job + scheduled slot (the UTC
  minute its schedule fired for, floored to the period). A retry, a manual
  "Run now" or an overlapping run for the same slot is a no-op. A failed
  slot may run again; its work rolls back with the failure recorded.
- Missed slots are counted from the first recorded run, over the last 24 h.

## ox features used

- Zero-config start: `uv run gunicorn app:app` is detected from `app.py`
  (worker count and access log come from `gunicorn.conf.py`; gunicorn's
  control socket goes to the unit's own `XDG_RUNTIME_DIR`, ox 94c9cada).
- `[cron]` with four jobs, `[build] migrate`, `[app] health`.
- `postgres = {}`: the shared Postgres, which provides `DATABASE_URL`.

## Variables

| Variable | From | Role |
| --- | --- | --- |
| `DATABASE_URL` | ox (shared postgres) | service |
| `PORT`, `PUBLIC_HOST`, `OX_ENV`, `OX_RELEASE` | ox | |
| `ZOO_PANEL_ORIGIN` | yours, `https://zoo-control.s1.zoo.sorv.dev` | plain |

No secrets.

## Zoo endpoints

- `GET /_zoo/health`, `GET /_zoo/probe` (CORS for `ZOO_PANEL_ORIGIN`).
- Probe checks: Postgres write/read/delete, `cron:tick` last ok slot under
  3 minutes old, `cron:rollup` under 11 minutes, and an idempotency
  self-test (claims the same slot twice in a transaction that rolls back).
  The freshness checks fail honestly when cron is not running.
- `GET /` history page, `GET /api/runs` the same as JSON.

## Run locally

```sh
uv sync
export DATABASE_URL=postgres://user@127.0.0.1:5432/cron
uv run python migrate.py
uv run python jobs.py tick
uv run gunicorn app:app --bind 127.0.0.1:8000
```

## Tests

```sh
uv run pytest                                                  # unit tests
TEST_DATABASE_URL=postgres://zoo@127.0.0.1:55432/cron uv run pytest   # plus Postgres
```

Recorded: unit only 32 passed, 2 skipped; with a local Postgres 18, 34 passed
(migrate, each job twice, a failing run then a retry, prune after 9 days,
probe all ok, page and JSON).

## ox check

```
ox check . (manifest: ox.toml)

  app.start                  uv run gunicorn app:app --bind 127.0.0.1:$PORT       detected:app.py
  app.health                 /_zoo/health                                         declared
  build.install              uv sync --frozen --no-dev                            detected:uv.lock
  build.migrate              uv run python migrate.py                             declared
  cron.prune                 @hourly  uv run python jobs.py prune                 declared
  cron.report                @daily  uv run python jobs.py report                 declared
  cron.rollup                */5 * * * *  uv run python jobs.py rollup            declared
  cron.tick                  * * * * *  uv run python jobs.py tick                declared
  tools.python               3.13                                                 detected:.python-version
  tools.uv                   0.11                                                 default
  services.postgres          postgres 18 (shared)                                 default

  Provided by ox: PORT, HOST, OX_ENV, OX_PROJECT, OX_RELEASE, OX_DATA_DIR, PUBLIC_URL, PUBLIC_HOST, DATABASE_URL
  Set on the dashboard before the first deploy: ZOO_PANEL_ORIGIN

Ready to deploy.
```

Not verified here: the jobs firing from ox's real scheduler (run by hand
locally), and gunicorn behind ox's proxy.
