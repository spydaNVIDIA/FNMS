# Assignment 2 requirement verification

This ledger maps the fourteen numbered requirements and seven self-tests in
`FNMS Fall 2026 Assignments 2.pdf` to implementation and observed evidence. The
seven-page PDF has no full weighted rubric. It explicitly assigns three points
to a real top-K report; software tests alone do not establish that empirical
requirement or a 100% grade.

The local implementation and checks below are complete. At this evidence review,
the third live run is in progress, the deployed Render verifier's idle-start
measurement is pending, and the student's manual audit is unfinished.

## Evidence types and observed checks

- **Automated tests:** `tests/test_*.py` uses controlled model, source, transport,
  and database responses. The final full-suite result belongs in README; this
  ledger does not report an estimated test count.
- **Actual local worker HTTP:** [.state/trials/http.json](.state/trials/http.json)
  records **11/11 checks passed** against running services. These deliberately
  non-spending requests test both workers' authentication, validation, replay,
  conflict, and tiny deadlines. Reproduce with `tests/trials.py`.
- **Actual live duplicates:**
  [live-duplicates-6ff05126-d32d-4e07-8f3e-c084a4b0b47f.json](evidence/live-duplicates-6ff05126-d32d-4e07-8f3e-c084a4b0b47f.json)
  records in-flight `503` plus `Retry-After` and identical completed replays for
  **both workers**. Before/after trace counts confirm no new model calls/fetches.
- **Actual PostgreSQL 16:**
  [docker-postgresql-trial.json](evidence/docker-postgresql-trial.json) records
  **32 checks passed** against the isolated Docker database on port 5434:
  Argon2id/JWT authentication, ownership, JSON snapshots, timestamps, history,
  restart recovery, and deletion cascades. Its worker responses are synthetic;
  it made **zero provider calls**.
- **Actual browser:** [.state/trials/browser-trial.json](.state/trials/browser-trial.json)
  and `dashboard.png` / `dashboard-mobile.png` record Chromium login, password
  clearing, history, logout, populated verdicts/excerpts, and a 390-pixel mobile
  viewport without overflow. An injection fixture rendered literally and its
  `javascript:` link was omitted. Five rapid Refresh clicks produced only three
  polling requests in 6.5 seconds while running, and zero after completion; there
  were no page reloads or JavaScript errors. UTC-naive timestamps were checked in
  America/Los_Angeles. The latest active-run check uses the application served
  from `/Users/spyda/FNMS/lab2` with migrated real history.
- **Actual deployed Render HTTP:** [render-smoke.json](evidence/render-smoke.json)
  records **6/6 checks passed** at
  `https://fnms-lab2-verifier.onrender.com`: public health, auth-before-parsing,
  authenticated bad JSON, blocked destinations, cached replay, and changed-body
  conflict. This requested **zero model/search calls**. It proves deployment
  behavior, not cold-start latency or live model verification.
- **Controlled failures over real HTTP/TCP:**
  [offline-network-failures.json](evidence/offline-network-failures.json) tests
  both worker-down outcomes using real failed TCP requests and replayed research
  data. It preserves requested K=5, marks missing verification `unverified`, marks
  reused history `stale`, and charges unknown usage once. It contacts no live
  workers and adds no provider spend. `tests/test_common_socket.py` separately
  tests a lost reply using a real local socket and a controlled worker handler.

## The fourteen requirements

| # | Requirement | Implementation and verification |
|---|---|---|
| 1 | Write the orchestration yourself | `orchestrator/engine.py` implements fixed research → project claims → verify → persist routing. `researcher/agent.py` and `verifier/agent.py` implement bounded loops without an agent framework. Researcher tests exercise model-selected search/fetch/remember/finish; orchestrator tests cover routing and merging. |
| 2 | Three services, one network | Separate Uvicorn processes expose the app on port 8010, researcher on 8011, and local verifier on 8012; `VERIFIER_URL` selects Render. `orchestrator/client.py` uses HTTP. Workers do not import another service's implementation or receive DB credentials; shared `common` code is neutral infrastructure. Launcher tests, actual HTTP trials, and live three-service traces verify the boundaries. Only the copied A1 application's orchestrator persists state; original Lab 1A/Lab 1B databases remain untouched. |
| 3 | The contract | `common/schemas.py` defines UUIDs, allowances, strict payloads/results, usage, status, and errors. `common/worker.py` implements public health and required codes: 200 complete/partial, 400 bad body, 401 bad auth, 409 conflicting ID, 500 terminal failure, and 503 temporary/busy with `Retry-After`. Both-worker contract tests and actual HTTP trials cover the contract; provider 500/503 cases also have controlled tests. |
| 4 | Service authentication | Separate researcher/verifier tokens are checked before JSON parsing; wrong/missing tokens return 401. Health is public. Each worker receives only its own service token. Actual local and deployed Render HTTP checks verify authentication, while `tests/test_scripts.py` checks environment isolation. |
| 5 | Validate both directions | Workers reject bad JSON/schema. `orchestrator/client.py` checks correlation IDs, exact result keys, usage bounds, uniqueness, complete coverage, and verdict IDs belonging to sent claims. Invalid content becomes a worker failure without reaching the report. `tests/test_orchestrator.py` covers unknown IDs, malformed results/503s, missing usage, oversized replies, and invalid complete top-K results. |
| 6 | Deadlines and partial results | Worker admission starts the original deadline; finished findings/verdicts survive exhaustion. Current allowances are researcher 900s/caller 990s and verifier 360s/caller 450s, with a 2000s run limit. Tiny-deadline HTTP checks and unit tests verify partials. Live run 2 preserved two developments after research exhaustion and independently verified three claims. Controlled TCP outages verify unverified/stale fallbacks and named failures. Actual Render cold-start timing remains pending. |
| 7 | Retries and duplicates | The caller freezes the full body/UUID, caps attempts at three, applies exponential backoff, and honors `Retry-After`; 400/401/409/500 are never retried. Workers compare canonical parsed JSON, replay completed results, and return busy 503 without a second job. Both live workers passed in-flight/replay checks with unchanged outbound-call counts. Socket tests cover lost replies. `ORCHESTRATION.md` explains cache loss after restart; this is process-lifetime idempotence, not durable exactly-once execution. |
| 8 | One budget per run | `config.yaml` sets total and componentwise worker shares. `Context` enforces model attempts, page hops including redirects, and tokens. The caller charges each logical UUID once; a missing trustworthy answer retains its full reservation. Tests verify pre-network checks, token reconciliation, redirect accounting, replay accounting, and invalid allocations. Live reports export the allocation/reported/charged ledger; outage fixtures verify unknown-usage charging. |
| 9 | Verdicts checked by code | `verifier/evidence.py` applies HTML entity decoding → Unicode NFKC → whitespace collapse → lowercase. Evidence must be nonempty, at most 300 characters, and an exact normalized substring of the independently fetched page. Invalid supported evidence becomes unsupported; other verdicts have null evidence. Tests cover normalization order, fabricated/empty/long quotations, malformed output, and unknown IDs. Real run 2 contains two supported excerpts and one unsupported claim. |
| 10 | Fetch guardrails | `common/runtime.py` limits schemes/time/size/redirects, resolves and checks IPs, pins connections to checked destinations, and rechecks every redirect. Tests cover private DNS, DNS pinning, redirects, limits, IPv6, and metadata. Actual local and Render requests returned unreachable for blocked URLs with zero page fetches/model calls. |
| 11 | Untrusted text crosses agents | `orchestrator/engine.py` sends only `claim_id`, `text`, and `source_url` to the verifier. It sends no researcher summaries, reasoning, or fetched pages. The verifier fetches independently with fresh model messages; routing/budgets remain Python configuration. Tests establish independent fetches, request-local caching, injection boundaries, and unknown-ID rejection; browser checks verify literal DOM rendering. Semantic entailment still requires the manual audit. |
| 12 | Display in the A1 app | The copied A1 authentication/UI displays immutable run snapshots, verdict labels, supported excerpts, source links, complete/partial status, failed workers, stale labels, usage, and duration. It renders web/model content using `textContent` and permits only HTTP(S) links. Actual browser tests verify populated results, history, mobile layout, auth, and polling. PostgreSQL checks verify persistence and ownership. |
| 13 | Trace log | `common/tracing.py` and `orchestrator/trace.py` emit all required fields: ts, service, run_id, request_id, step, direction, endpoint_or_tool, status, latency_ms, attempt, tokens. Latency is measured by callers. The exported run 2 manifest lists all three correlated logs with none missing. Tests cover malformed/oversized/interrupted paths. Logs record counters and safe failure codes rather than credentials or raw provider error bodies. |
| 14 | Secrets | `.env.example` lists credentials; `.env` is ignored. Setup creates distinct secrets and can import only prior provider keys. Role-specific environment whitelists keep DB/JWT/researcher/search secrets out of the verifier and the verifier token out of the researcher. `tests/test_scripts.py` checks isolation and safe process ownership. Render secrets live in environment settings. Live provider calls require the configured approval gate. |

## The PDF's seven self-tests

All worker-general rows were tested against **both** local workers. The blocked
claim-URL row belongs to `/verify`: `/research` accepts topic/K, not claims, and
rejects a claim-shaped payload instead of accepting another endpoint's contract.

| PDF request | Expected answer | Observed evidence |
|---|---|---|
| Missing or wrong token | 401 | Both local workers in `http.json`; Render also passed auth-before-body-parsing. |
| Body fails schema | 400 | Both local workers: authenticated malformed JSON and invalid schema. Render authenticated malformed JSON also returned 400. |
| Same ID and body twice | Same response; no new model calls/fetches | Both workers' live duplicate records show identical hashes and unchanged outbound counts. Local zero-spend replay and Render reordered-JSON replay also passed. |
| Same ID, different body | 409 | Both local workers in `http.json`; valid changed-body conflict also passed on Render. |
| Same ID while first is processing | 503 plus `Retry-After`; no second job | Both workers' live in-flight duplicates passed. A socket test additionally covers a caller losing the first reply. |
| Metadata or localhost claim URL | unreachable, no fetch attempted | Local verifier and Render returned unreachable with zero model/fetch/token usage. IPv6/private/link-local/unspecified destinations were also checked. |
| Tiny time budget | partial with completed work | Both local workers returned HTTP 200 partial in actual HTTP tests. Controlled tests additionally prove finished findings/verdicts survive exhaustion. |

## Empirical submission work and remaining gates

1. **Prior tracker readiness:** [lab1b-readiness.md](evidence/lab1b-readiness.md)
   records 44 passing tests, lint, and CLI startup from a source-only scratch copy
   with no inherited secrets/state, plus a historical complete top-five baseline.
   This was not a fresh dependency installation or new live Lab 1B run. Original
   files were hash-checked and preserved.
2. **Real Lab 2 research:**
   [run 1](evidence/72b388a4-8d9b-45a9-8e8b-ee2de80d42cb/report.json) honestly ended
   partial with no developments.
   [run 2](evidence/6ff05126-d32d-4e07-8f3e-c084a4b0b47f/report.json) honestly ended
   partial with two developments/three claims: two supported, one unsupported,
   none unreachable. Its [manifest](evidence/6ff05126-d32d-4e07-8f3e-c084a4b0b47f/manifest.json)
   includes all three logs. Run 3 `c91e5bbc-5372-48e6-9dc3-b40e56a27e81` remains
   active at this review. A real completed top-K result is not yet certified;
   successful controlled fixtures do not fill that gap.
3. **Render cold start:** deployment/smoke passed. The artifact records last
   contact at `2026-11-25T08:27:10.847145+00:00` and earliest planned idle-start
   check at `08:43:10.847145+00:00`, conditional on no intervening requests.
   No cold-start latency is claimed until that measurement is saved.
4. **Student hand-check:** [source-review-notes.md](evidence/source-review-notes.md)
   contains an explicitly AI-authored preliminary review of all three run 2
   claims, finding zero wrong verdicts among those three. It also identifies
   source-selection and model-categorization weaknesses. This is **not the
   student's hand-check** or the required ten-supported-claim sample.
   `scripts/evaluate.sh` selects every unsupported claim and ten random supported
   instances with seed 42 when enough exist; it leaves judgments empty. The
   student must inspect sources, fill judgments, and discuss measured cost,
   latency, and wrong-verdict counts in `ORCHESTRATION.md`.
5. **Grading readiness:** keep the isolated A1-derived PostgreSQL database awake,
   configure matching Render/local credentials, and follow README setup/start
   instructions. Only free-tier use was authorized. If a paid account is added,
   configure the prompt's provider-side hard spend cap before requests;
   application token limits do not establish a billing-console cap.

The required contract explanation, cross-service latency table, duplicate code
excerpts, restart limitations, injection discussion, and measured comparison
belong in [ORCHESTRATION.md](ORCHESTRATION.md). This ledger does not replace that
document or label pending evidence as completed work.
