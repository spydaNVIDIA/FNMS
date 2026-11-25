# Docker PostgreSQL integration trial

Result: **32 application integration checks passed** against the project's Docker
database, with **zero model or search provider calls**. The complete results are in
`docker-postgresql-trial.json` beside this document.

The trial ran on 2026-11-25 from 08:12:15.870232 to 08:12:16.431094 UTC. The
application checks took 0.560869 seconds using Python 3.13.3 and **PostgreSQL 16.15**
from the Compose `postgres:16` image. The connection used host port **5434** mapped
to PostgreSQL's container port 5432, with role `lab2` and database `lab2`.
Passwords are redacted in the recorded connection URL.

## Reproduce

The reusable harness is `tests/postgresql_trial.py`. From the Lab 2 project root,
with its declared dependencies installed:

```bash
python tests/postgresql_trial.py \
  --env-file .env.example \
  --output evidence/docker-postgresql-trial.json
```

This invocation reads the documented local Compose database settings. Omitting
`--env-file` reads `.env` instead; `--database-url` can explicitly override the
selected file's `DATABASE_URL`. The script checks the target before importing the
application or connecting: only `postgresql+psycopg`, `localhost`/`127.0.0.1`, ports
5434/5435, role `lab2`, and database `lab2` are accepted. URL query parameters are
rejected because they can override the destination. It refuses to run against a
database containing a `running` tracker row.

Fourteen offline target-validation tests also passed, covering permitted ports and
hosts, rejected unrelated databases/users/hosts/ports, and connection-query
overrides. Ruff passed for the harness and its guard tests.

## Coverage and isolation

The harness uses the real FastAPI application, SQLAlchemy ORM, PostgreSQL driver,
Argon2id hashing, and JWT authentication. It checks idempotent seeding, login and
registration, missing/malformed-token rejection, password-hash omission, account
and run ownership, background run persistence, nested JSON and nulls, stored
Markdown, timezone-aware timestamps, run duration, and historical report reads
through separate sessions and application lifespans. It also verifies interrupted
run recovery and actual PostgreSQL foreign-key cascades on account deletion.

Worker orchestration is replaced with clearly labeled offline fixture results.
Synthetic reports and traces are segregated under `.state/postgresql-trial-5434`;
they are not live research evidence. Cleanup removes only accounts created by this
trial and their owned rows, then verifies pre-existing user IDs and run snapshots
remain. At this trial's completion, the database held the seeded grader account
and no synthetic runs, ready for legitimate history migration.

The live application on port 8010 was not contacted or restarted. The separate
PostgreSQL instance on port 5435 was not accessed or modified by this trial.
No Lab 1A or Lab 1B files or databases were changed.
