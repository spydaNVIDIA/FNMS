# Lab 1B preparation check and measured baseline

Checked on 2026-11-25. Original project: `/Users/spyda/FNMS/lab1b`.
The original Lab 1B files were not modified, and no live provider calls were made.

## Source-only readiness trial

Copied 46 source/configuration/test/documentation files into
`/private/tmp/fnms-lab1b-cleanclone-q2otzqhe/lab1b`. The copy excluded all `.env`
files, virtual environments, Git metadata, caches, bytecode, logs, reports, runtime
state, and database files. It retained the `.env.example` templates. SHA-256 checks
confirmed that all 46 copied files matched the originals before testing and that
the originals were unchanged afterward. This was a source-only copy, not a new
Git checkout or a new dependency installation.

The test interpreter was
`/Users/spyda/Documents/ChatGPT/fnms/lab1b-work/.venv/bin/python`, Python **3.13.3**
on macOS. All packages listed below matched the pinned requirements in the copied
`backend/requirements.txt` and `backend/requirements-dev.txt`:

| Package | Installed version |
| --- | --- |
| fastapi | 0.115.6 |
| uvicorn | 0.34.0 |
| sqlalchemy | 2.0.36 |
| psycopg | 3.2.3 |
| pydantic | 2.10.4 |
| pydantic-settings | 2.7.1 |
| argon2-cffi | 23.1.0 |
| pyjwt | 2.10.1 |
| python-dotenv | 1.0.1 |
| httpx | 0.28.1 |
| PyYAML | 6.0.2 |
| beautifulsoup4 | 4.12.3 |
| ruff | 0.8.4 |
| pytest | 8.3.4 |

Commands ran from the scratch copy, with `PYTHONDONTWRITEBYTECODE=1`:

```bash
python -m pytest -q -p no:cacheprovider
python -m ruff check --no-cache backend tracker tests
python -m tracker --help
```

Results: **44 tests passed in 1.00 seconds**, Ruff reported **All checks passed**,
and the tracker CLI displayed its usage successfully. Pytest reported one existing
Starlette/AnyIO `BlockingPortal` deprecation warning, with no failures.

The tests exercise an empty-state tracker run that produces a ranked top five using
mocked model/search/fetch responses; authenticated backend persistence and user
isolation against an isolated SQLite database; historical reports and run duration;
recrawl skipping and new/still/dropped matching; partial/failed run artifacts;
budgets and retry classification; fetched-source provenance; URL, DNS, redirect,
and response-size checks; and UI security/control invariants. The initial source
copy contained no saved tracker state or reports.

Scope: this demonstrates that the copied application imports, its CLI starts, and
its offline behavior passes with the declared dependencies without inherited
secrets or saved state. It does **not** constitute a new live run, a fresh package
installation trial, a PostgreSQL/Docker startup trial, or a browser interaction
trial. Historical live Run 1 supplies the prior live baseline below; its claims
were not re-verified by this readiness test.

## Historical live Run 1 baseline

Sources read directly from the unchanged original project:

- `reports/run1.md`, SHA-256
  `117001ab06e2e26d939a007a815f4830ebd111ca7942c96febf5ce0cd397dfaa`
- `logs/run1.jsonl`, SHA-256
  `a2099bf760fa71417f17850940c4fb3ecf353c1249c67ca71f6eedb3e56acce6`

The report records status `complete` with five ranked developments.

| Measurement | Value |
| --- | ---: |
| Report start | 2026-10-12T21:55:23.148762+00:00 |
| Final trace event, local `finish` | 2026-10-12T21:58:05.331921+00:00 |
| Elapsed from report start to final trace | **162.183159 seconds** |
| Trace events | 19 |
| Groq model attempts | 12 |
| Successful model attempts | 7 |
| Model errors classified `rate_limit` | **4** |
| Model errors classified `provider_tool_validation` | **1** |
| Tavily `search_web` calls | 1 |
| `fetch_article` attempts | 5 |
| Local `finish` calls | 1 |
| Recorded input tokens | **22,282** |
| Recorded output tokens | **2,047** |
| Total recorded tokens | 24,329 |
| Search credits | 2 |
| Sum of recorded model latency | 10,866 ms |
| Sum of recorded article-fetch latency | 3,886 ms |
| Sum of recorded search latency | 338 ms |
| Sum of recorded local finish latency | 0 ms |
| Sum of all recorded call latency | **15,090 ms** |

Counts are computed from the JSONL entries, grouping by `kind`, `tool`, `status`,
and non-null `error_class`; token, credit, and latency totals are sums of their
recorded fields. Elapsed time is the final trace timestamp minus the report's
`Run started` timestamp. The 15.090-second sum is **not** total run duration:
162.183159 seconds includes time between calls, including retry waits and local
work. The final trace precedes state saving and report-file writing, so it is also
not a measurement of the entire CLI process lifetime.

The original `AGENT.md` describes all five model errors as rate limits. The trace
instead records **four rate limits plus one provider tool-validation error**; use
the trace counts above for comparisons. That original file was left untouched.

The trace represents 18 top-level remote operations: 12 model attempts, one
search, and five page-fetch attempts. `finish` is local. Backend login/state
load/state save are not included in the trace, and page redirects are not logged
as separate wire requests. The original `AGENT.md`'s 21-round-trip estimate adds
three backend requests; that figure is not independently proven by the trace.
Code always loads and saves state and additionally logs in when using credentials
rather than an existing bearer token. Distinguish top-level operations from
measured HTTP wire round trips when comparing Lab 1B with Lab 2.

For a like-for-like **estimated cost**, the historical policy in `config.yaml`
uses $0.15 per million input tokens and $0.60 per million output tokens:

```text
22,282 × $0.15 / 1,000,000 + 2,047 × $0.60 / 1,000,000 = $0.0045705
```

Using the historical `AGENT.md` search assumption of $0.008 per credit adds
`2 × $0.008 = $0.016`, for a total pay-as-you-go equivalent of **$0.0205705**.
These are estimates using recorded usage and historical policy assumptions, not
an account billing statement or a claim about current provider prices. Free-tier
credits may make the actual charge zero, and the trace does not report token use
for rejected model attempts.
