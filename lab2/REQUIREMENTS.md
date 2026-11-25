# Requirement verification ledger

This ledger separates implementation/tests from external deployment evidence.
The seven-page prompt has fourteen requirements and no complete weighted rubric.

| # | Requirement | Implementation / verification |
|---|---|---|
| 1 | Hand-written orchestration | `orchestrator/engine.py`, `researcher/agent.py`; no framework |
| 2 | Three independent HTTP services | Three launch commands; explicit worker HTTP calls; only orchestrator DB credentials |
| 3 | Contract/status codes | `common/schemas.py`, `common/worker.py`; both-worker contract tests and HTTP trials |
| 4 | Separate service authentication | Auth middleware before parsing; cross-worker-token tests; public health |
| 5 | Validate both directions | Worker payload types and `orchestrator/client.py`; malformed/unknown-ID/usage tests |
| 6 | Deadlines and partials | Context elapsed deadline; longer caller allowance; partial/stale/unverified tests |
| 7 | Retries/idempotency | Managed job registry, canonical JSON, cached outcomes; concurrency/disconnect/replay tests |
| 8 | One run budget | Fixed shares, UUID ledger, full unknown charge; model-attempt and redirect accounting tests |
| 9 | Evidence validation | `verifier/evidence.py`; exact normalization, empty/oversize/fabricated evidence tests |
| 10 | Fetch guardrails | IP-pinned public targets, per-hop redirects, streamed limits; metadata/IPv6/DNS tests |
| 11 | Untrusted text | Explicit data projection, immutable routing/config, injection fixtures and text DOM |
| 12 | A1 UI | Authenticated history, immutable claim cards, verdict/evidence/failure/stale labels |
| 13 | Traces | Every service emits inbound/outbound JSONL with correlation IDs and caller latency |
| 14 | Secrets | Ignored .env, complete example, role-specific child environments, no raw provider errors |

## External evidence gates

- A real researcher run must finish with K distinct findings (fixtures do not count).
- Independently verify real sources and record actual verdict/usage/latency counts.
- Deploy verifier to Render and measure an actual cold start; local simulations do not count.
- Capture three correlated service logs and complete the manual claim audit.
- Keep the database available for grading and provide clean-clone setup instructions.

Observed trial results are recorded in `ORCHESTRATION.md` and exported evidence.
