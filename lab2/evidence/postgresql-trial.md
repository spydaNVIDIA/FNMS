# Isolated PostgreSQL integration trial

Result: **32 checks passed**, with **zero model or search provider calls**.
Machine-readable results are in `postgresql-trial.json` beside this document.

The trial ran on 2026-11-25 at 08:08:44 UTC, using Python 3.13.3 and
**PostgreSQL 14.15 (Homebrew)** on macOS/Apple Silicon. The application integration
checks took 0.583498 seconds after initializing and starting the database.

## Isolation and setup

A new PostgreSQL cluster was initialized under
`/Users/spyda/Documents/ChatGPT/fnms/lab2-work/.state/pg-test`, an ignored runtime
directory. It listens only on `127.0.0.1:5435`, using ephemeral local trust
authentication. Its role and database are both `lab2`; no password was created or
printed. The SQLAlchemy URL is:

```text
postgresql+psycopg://lab2@127.0.0.1:5435/lab2
```

No existing database or service on port 5432 was accessed or modified. The running
Lab 2 application was not restarted during this trial. The cluster was initialized
using `initdb`, started using `pg_ctl`, and its database created using `createdb`.
PostgreSQL shared-memory creation and local socket access required approved
execution outside the filesystem sandbox.

The integration harness is retained locally at `.state/postgresql_trial.py`.
It sets its database URL explicitly before importing the app and checks the server
database name, role, and port before creating tables. It uses FastAPI TestClient
with the real application routes, SQLAlchemy models, seed routine, password
hashing, JWT handling, and PostgreSQL driver. Only the worker orchestration
function is substituted with clearly marked synthetic fixture results; no provider
or source website is contacted.

## Checks performed

- Real SQLAlchemy table creation and idempotent seeding; the seeded grader password
  is stored as an Argon2id hash.
- Health endpoint, missing and malformed bearer rejection, grader login,
  registration/login for two isolated trial users, and authenticated `/auth/me`.
- Password hashes omitted from registration responses and another user's account
  or run returned as 404.
- Authenticated run launch and background completion persisted through PostgreSQL.
  Nested JSON arrays/objects/nulls, the usage ledger, Markdown, and timezone-aware
  timestamps survived a separate database-session read.
- Duration calculation, user-specific history, and immutable completed snapshots
  after application lifespan restart.
- A synthetic interrupted `running` row became `partial` on startup, with an
  explicit restart reason and persisted completion time/duration.
- Real PostgreSQL foreign-key cascades deleted owned runs when the trial account
  was deleted; its previously issued token then returned 401.
- Cleanup removed both trial users and every synthetic run. At trial completion,
  only the seeded `NYUgrader` account remained, ready for subsequent legitimate
  application trials.

This validates PostgreSQL-backed application behavior. It is separate from live
research/verification evidence and does not claim to test the Docker Compose
PostgreSQL 16 image. Fixture reports and traces are segregated under
`.state/postgresql-trial` and labeled as offline tests.

The parent integration session owns subsequent use and shutdown of this cluster:

```bash
/opt/homebrew/bin/pg_ctl \
  -D /Users/spyda/Documents/ChatGPT/fnms/lab2-work/.state/pg-test \
  -m fast -w stop
```
