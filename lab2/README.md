# Lab 2 — Agentic orchestration

Three small services extend the A1 authenticated app and the Lab 1B robotics tracker.
The orchestrator calls a real researcher and an independent verifier over HTTP.
Only the orchestrator has database access. Lab 1A and Lab 1B are separate submissions
and are not modified by these scripts.

The assignment says to persist history in the A1 database. To honor the request to
leave both earlier labs untouched, this copy retains A1's authentication/user
schema and adds `orchestration_runs` in an isolated database. It does not write to
the earlier labs' databases. The database URL can be changed if grading requires
a specific hosted instance; only the orchestrator receives that setting.

## Quick start

Prerequisites: Python 3.11+ and Docker Desktop. From this directory:

```bash
./scripts/setup.sh
# Edit .env: set MODEL_API_KEY and TAVILY_API_KEY.
# For free-tier-only accounts, or AFTER setting hard paid-provider spending caps:
# set LIVE_CALLS_APPROVED=true.
./scripts/start.sh
./scripts/status.sh
```

Open **http://localhost:8010**. Login with the seeded local account:
`NYUgrader` / `Courant2026!`, or create an account. The UI and API share one origin.
Click **Run research and verification**. This makes live provider calls; startup,
page visits, and refreshes do not. Runs are deliberately sequential and paced to
respect free-tier rate limits, so a run can take several minutes.

To trigger the same workflow from a terminal:

```bash
./scripts/run.sh
```

To stop only the processes owned by this Lab 2 launcher:

```bash
./scripts/stop.sh
```

The database volume is preserved. Ports are 8010 (orchestrator/UI), 8011
(researcher), 8012 (local verifier), and 5434 (isolated PostgreSQL). Existing labs'
ports and database containers are not touched. A local SQLite URL can be used for
isolated offline tests; the normal launcher uses the copied A1 schema in Postgres.
Shutdown gives in-flight requests five seconds to finish. On the next startup,
any interrupted run is marked partial with an explicit restart reason.
The prompt's “A1 database” is implemented as the database of this copied A1 app;
it holds both users and Lab 2 runs. The original submissions' databases are not shared.

Setup can reuse ONLY provider credentials from an existing ignored environment file:

```bash
./scripts/setup.sh --reuse-provider-env ../lab1b/.env --approve-free-tier
```

Use `--approve-free-tier` only for free-tier accounts with no paid usage. The script
never prints secrets. It creates distinct random worker tokens and a JWT secret.

## Code map

| Directory | Responsibility |
| --- | --- |
| `orchestrator/` | A1 auth, HTTP routing/retries, run budget ledger, immutable DB snapshots, UI API |
| `researcher/` | Hand-written search/fetch/remember/finish loop; no database access |
| `verifier/` | Independent fetch and per-claim semantic check; evidence normalization in code |
| `common/schemas.py` | Strict request/response wire contract |
| `common/worker.py` | Auth before JSON parsing; in-flight/cached/conflicting request state machine |
| `common/runtime.py` | Request-local deadlines, model-call/token/page-hop accounting, provider and fetch I/O |
| `frontend/` | Small same-origin dashboard; web content rendered with textContent |
| `scripts/` | Setup, owned-process lifecycle, CLI runs, evidence export and audit preparation |
| `tests/` | Offline unit/integration tests and actual-HTTP contract trials |

Shared code is neutral transport/schema/I/O infrastructure. No service imports or
directly calls another service's application/agent implementation. The verifier
does not receive researcher summaries, reasoning, fetched text, or saved state.

## The flow

1. Authenticate the user, load the most recent non-stale ranking, allocate fixed shares.
2. Send one logical `/research` request. The real model chooses research tools.
3. Project its claims into only `claim_id`, `text`, and `source_url`.
4. Send one logical `/verify` request. Independently fetch sources, then check claims.
5. Validate the workers' responses, including IDs, coverage, evidence fields and usage.
6. Combine findings/verdicts and persist a complete or partial immutable snapshot.

The researcher requires K distinct developments at successful finish. A fetch count
does not determine readiness. `remember_development` preserves useful partial work.
The verifier may reuse a page within one request, but never a researcher page or a
cross-run source cache. Unsupported claims remain visible, clearly labeled.

## Contract and failure semantics

Both workers expose public `GET /healthz`. Work endpoints require separate Bearer
tokens: `POST /research` uses RESEARCHER_TOKEN, `/verify` uses VERIFIER_TOKEN.

The request is exactly the assignment envelope:

```json
{
  "request_id": "<UUID per logical request>",
  "run_id": "<UUID shared by a run>",
  "time_budget_ms": 60000,
  "budget": {"max_steps": 10, "max_fetches": 8, "max_tokens": 40000},
  "payload": {"topic": "open-source robotics foundation models", "k": 5}
}
```

`/verify` replaces payload with `{"claims":[{"claim_id":"...","text":"...","source_url":"..."}]}`.
The prompt leaves the complete response envelope unspecified. Our documented
extension includes `request_id`, `run_id`, `status`, `usage` (`steps`, `fetches`,
`tokens`), `result`, and nullable `error` (`code`, `message`, `retryable`).

| HTTP | Meaning | Orchestrator retry |
| --- | --- | --- |
| 200 | Complete or partial result | No |
| 400 | Malformed JSON or schema | No |
| 401 | Missing/wrong service token, even with a malformed body | No |
| 409 | Same request ID, different parsed JSON | No |
| 500 | Terminal worker failure | No |
| 503 | Work in progress or temporary provider unavailability; Retry-After included | Bounded retry |

Retries reuse identical serialized bodies, IDs, and time_budget_ms. A worker keeps
one managed job per ID. In-flight duplicates get 503; finished duplicates receive
the original response without new model calls or fetches. One Uvicorn process per
service is intentional: the in-memory registry is not shared across processes.
A worker restart clears it; this does not provide durable exactly-once execution.

Verifier failure means **unverified**, never unsupported. A blocked/unfetchable URL
means **unreachable**. Researcher failure carries the prior ranking forward as
**stale** and explicitly says there are no new results. Empty history yields an
honest empty partial report. Partial fresh research remains usable.

## Budgets, deadlines, and source safety

`config.yaml` defines total and fixed worker shares. Each model attempt is a step;
each outgoing page request, including redirects, is a fetch. Workers enforce the
shares and combined token usage. Token reservations include tools and bounded output;
unknown failed-call usage stays charged conservatively. These estimates are not an
account billing meter. Search attempts have a separate small local ceiling.

The orchestrator charges each UUID once. Missing or malformed responses retain the
full allocation. Uncertain spend is never reallocated to another worker. Runtime
deadlines use monotonic elapsed time; caller timeouts include additional cold-start
allowance. Provider waiting stays inside the original worker deadline.

Fetches require HTTP(S), reject credentials and nonpublic resolved IPv4/IPv6
addresses, pin connections to checked IPs while preserving hostname/TLS validation,
and check/count every redirect. Reads are bounded by size and remaining time.

Supported evidence must be nonempty and <=300 characters. Code decodes HTML
entities, applies Unicode NFKC, collapses whitespace, lowercases, and checks substring
presence in independently fetched text. This proves quotation presence, not semantic
entailment; the required human audit evaluates the latter.

## Deploy the verifier to Render

1. Make this lab2 directory available in your GitHub repository. Keep `.env` excluded.
2. In Render choose **New Web Service**, connect that repo, and choose the **Free** plan.
3. Set root directory to `lab2`.
4. Build command: `pip install -r requirements.txt`.
5. Start command: `uvicorn verifier.app:app --host 0.0.0.0 --port $PORT --workers 1`.
6. Health-check path: `/healthz`.
7. Add environment variables `VERIFIER_TOKEN` (same value as local .env),
   `MODEL_API_KEY`, `MODEL_BASE_URL`, `MODEL_NAME`, `MODEL_MIN_INTERVAL_SECONDS`,
   and `LIVE_CALLS_APPROVED=true` after confirming free-tier/spending-cap settings.
   Do not configure DATABASE_URL, JWT_SECRET, RESEARCHER_TOKEN, or TAVILY_API_KEY there.
8. Locally set `VERIFIER_URL=https://<your-service>.onrender.com`, then stop/start.
   The launcher will omit the local verifier and use the remote URL.

`render.yaml` also describes the verifier deployment. Rendering stays in the local
authenticated UI. Render's service logs contain JSONL traces on stdout; export the
relevant verifier entries before log retention expires. Local disk and idempotency
cache do not survive Render restarts/spin-down.

To measure a real cold start, let the deployed verifier idle for >15 minutes without
health polling, then run again. Compare caller latency against worker processing
latency using the same request ID; record the result rather than substituting a
simulated delay. The local database must remain available during grading.

## Verification and evidence

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
# With all services running; uses no live model/search calls:
.venv/bin/python -m tests.trials
./scripts/export.sh
./scripts/evaluate.sh
```

Tests use explicitly marked fixtures. They are not submitted as live research.
Runtime reports/traces live under `.state/`; export creates an evidence bundle for
intentional submission. Every service trace includes the prompt's eleven fields;
`tokens` is a numeric usage count, never an authentication token.

See `REQUIREMENTS.md` for coverage and `ORCHESTRATION.md` for design reasoning and
measured results. Generate enough actual runs to hand-check every unsupported claim
and ten randomly sampled supported claims. The audit helper selects the sample but
does not invent human judgments. No Lab 2 day-apart naming rule is imposed.
