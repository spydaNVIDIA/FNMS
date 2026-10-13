# FNMS Lab 1B — Robotics Foundation Model Tracker

A hand-written research agent that finds, ranks, and remembers developments in
open-source robotics foundation models. It extends the Lab 1A FastAPI/Postgres
authentication app with authenticated tracker storage and a protected dashboard.

The runtime is deliberately not built on an agent framework. Python code owns the
tool policy, network guardrails, retry classification, budgets, persistence,
deduplication, ranking, and report structure. The model chooses research queries,
sources, and evidence-backed candidate developments.

## Prerequisites

- Docker Desktop
- Python 3.11+
- A Tavily API key
- An API key for the OpenAI-compatible model configured in `config.yaml`

The default is Groq's `openai/gpt-oss-120b`. Provider/model names, prices, and hard
budgets are policy in `config.yaml`; secrets are environment variables only.

## Clean-clone setup and first run

Run these commands in order.

### 1. Start Postgres

```bash
cd FNMS/lab1b
docker compose up -d
```

### 2. Start the backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python seed.py
python -m uvicorn app.main:app --reload --port 8000
```

Leave that terminal running. `http://localhost:8000/healthz` should return
`{"status":"ok"}`.

### 3. Start the frontend

In a second terminal:

```bash
cd FNMS/lab1b/frontend
python3 -m http.server 5173
```

Open <http://localhost:5173>, log in, and use **Robotics tracker** in the navigation.

The grader account is created idempotently by `seed.py`:

- Username: `NYUgrader`
- Password: `Courant2026!`

The tracker screen also has an authenticated **Run tracker now** button. It starts
the same bounded CLI agent as `python -m tracker run`, using the signed-in user's
tracker state. The browser never receives provider keys, and only one UI-triggered
run can execute at a time. The button warns before making live Groq and Tavily calls.

### 4. Configure tracker secrets

In a third terminal:

```bash
cd FNMS/lab1b
cp .env.example .env
```

Edit `.env` locally and set `MODEL_API_KEY`, `TAVILY_API_KEY`, and either:

- `TRACKER_USERNAME` plus `TRACKER_PASSWORD`; or
- a short-lived `TRACKER_API_TOKEN` returned by `/api/auth/login`.

`.env` is ignored by Git. The committed `backend/.env` contains only the assignment's
throwaway database/JWT values; do not put model or search keys there.

### 5. Run the tracker for the first time

Reuse the backend virtual environment so the declared dependencies are available:

```bash
source backend/.venv/bin/activate
python -m tracker run
```

### 6. Run the tracker again

For an immediate recrawl test, use the same command:

```bash
source backend/.venv/bin/activate
python -m tracker run
```

Saved URLs and developments are loaded through the authenticated A1 API before
research begins. A source already fetched is logged as `skipped` and is not requested
again. An immediate rerun is diagnostic: it is saved in the database and receives a
timestamped report/log filename.

The first real run writes `reports/run1.md` and `logs/run1.jsonl`. `run2` is used only
when its start time is at least 24 hours after run 1; earlier additional runs receive
a timestamped filename instead. This prevents accidentally fabricating the required
day-apart evidence. For the submitted Run 2, wait at least 24 hours after the Run 1
start time and then run the same command again. UI-triggered same-day runs are useful
recrawl diagnostics, but do not satisfy the submission requirement for day-apart Run
1 and Run 2 artifacts.

## Call tools without the model

```bash
source backend/.venv/bin/activate
python -m tracker.tools search_web "OpenVLA model release"
python -m tracker.tools fetch_article https://github.com/openvla/openvla
python -m tracker.tools finish path/to/report.json
```

`fetch_article` accepts only configured HTTP(S) hosts, validates every DNS answer and
redirect target, rejects non-global IP addresses, and enforces time and byte limits.

## Reset tracker state

This removes tracker history while preserving Lab 1A users:

```bash
cd FNMS/lab1b
docker compose exec db psql -U lab1b -d lab1b -c 'TRUNCATE tracker_trace_events, tracker_run_developments, tracker_articles, tracker_developments, tracker_runs RESTART IDENTITY CASCADE;'
rm -f reports/run*.md logs/run*.jsonl
```

The SQL data can be recovered only from a database backup. Report and log files are
ordinary working-tree files and can be recovered from Git if previously committed.

## Policy and secrets

`config.yaml` holds:

- Topic and K
- Model endpoint/name and per-token price assumptions
- Tool list and schemas
- Maximum steps, searches, fetches, model calls, tokens, credits, retries, and cost
- Fetch time, size, content-type, and redirect limits
- Allowed schemes and hosts

`.env.example` lists tracker secrets. `backend/.env.example` lists backend settings.
No live provider key belongs in either committed file.

## Tracker schema

| Table | Purpose |
| --- | --- |
| `tracker_runs` | Start/completion times, topic, status, budget totals, and rendered report |
| `tracker_articles` | Per-run URL ledger, canonical URL, fetch time/status, rejection, and hash |
| `tracker_developments` | User-scoped remembered developments and supporting sources |
| `tracker_run_developments` | Rank, `new`/`still`/`dropped` bucket, score, and citations for one run |
| `tracker_trace_events` | Every model/tool call with step, arguments, status, latency, tokens/credits, and failure class |

All tracker rows are user-scoped. Database foreign keys cascade on account deletion.

## API

All tracker routes require `Authorization: Bearer <token>`. Missing, malformed, or
expired tokens return 401. A run owned by another user is returned as 404.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/tracker/state` | URLs, remembered developments/sources, and previous top K |
| `POST` | `/api/tracker/run-now` | Start one live bounded tracker run for the signed-in user |
| `GET` | `/api/tracker/run-now/status` | Poll the current user's UI-triggered run status |
| `POST` | `/api/tracker/runs` | Atomically store a completed, partial, or failed run |
| `GET` | `/api/tracker/reports/latest` | Latest structured report for the dashboard |
| `GET` | `/api/tracker/runs` | Run history |
| `GET` | `/api/tracker/runs/:id/report` | Selected run's New/Still/Dropped report |
| `GET` | `/api/tracker/runs/:id/articles` | Per-run fetch ledger |

The original Lab 1A auth and user endpoints remain unchanged.
Run summaries include a computed `duration_seconds` derived from the persisted start
and completion times; the dashboard formats it for the latest report and run history.

## Safety behavior

- Web text is wrapped in `BEGIN_UNTRUSTED_WEB_CONTENT` markers.
- The system prompt forbids following instructions found in tool output.
- The model cannot add tools, raise budgets, change the allowlist, or bypass URL checks.
- A development is rejected unless every citation is a fetched or previously saved URL.
- The frontend constructs DOM nodes with `textContent`; fetched text is never rendered as HTML.
- Per-minute 429s retry with capped exponential backoff and `Retry-After` support.
- Daily quota, bad-key, payment, and policy failures stop with a clear status.
- Budget exhaustion writes and saves a report marked `partial`.

## Development and verification

```bash
cd FNMS/lab1b
source backend/.venv/bin/activate
python -m pip install -r backend/requirements-dev.txt
python -m ruff check backend tracker tests
python -m pytest -q
```

The tests cover authentication/user isolation, historical reports, fixed-K output,
recrawl and cross-URL development matching, hard budgets, bogus credentials,
network cuts, 429 classification, DNS/redirect/size guardrails, prompt-injection
labeling, trace artifacts, and the frontend's no-`innerHTML` invariant.

## Provider references

- [Groq model list and pricing](https://console.groq.com/docs/models)
- [Groq deprecations](https://console.groq.com/docs/deprecations)
- [Tavily pricing and free credits](https://www.tavily.com/pricing)
- [Tavily Search API reference](https://docs.tavily.com/documentation/api-reference/endpoint/search)
