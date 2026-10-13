# Agent design notes

## 1. Workflow vs. agent

The model decides what to search, which allowlisted source looks relevant, what facts
the fetched text supports, a normalized development key, and a preliminary 0-100
importance score. Python decides whether that request is legal and affordable. It
validates tool names and arguments, URL schemes/hosts/DNS/redirects, retry class,
step/call/token/credit/cost ceilings, citations, duplicate keys, final score ordering,
`new`/`still`/`dropped` buckets, storage, and filenames.

One decision deliberately moved out of the model is whether to fetch a known URL.
`TrackerAgent._tool_result` canonicalizes the requested URL and compares it to the
backend's saved `seen_urls` before any network request. A model can ask for a cached
page, but code records `skipped` and never calls the network.

## 2. The network

Live Run 1 recorded 19 trace events: 12 Groq attempts (7 successful and 5 rate-limit
errors), one Tavily search, five article fetches, and one local `finish` call. The
state client also made three untraced backend requests: login, state load, and state
save. Because `finish` is local, Run 1 therefore used 21 outbound HTTP round trips:
12 to Groq, one to Tavily, five to public sources, and three to the A1 backend.

The trace measured 15,090 ms across model and tool calls. Groq accounted for 10,866
ms (72.0%), article fetches for 3,886 ms (25.8%), and Tavily for 338 ms (2.2%). The
three local state-API calls were not timed in the JSONL trace. Retries are recorded as
separate model attempts and remain inside the same hard model-call and token budgets.
The maximum policy is 4 searches, 12 fetch attempts, and 18 model attempts; including
login/load/save, that is at most 37 outbound requests when every allowed call is used.

## 3. “New”

URL identity is not development identity. URLs are canonicalized by lowercasing the
host, removing fragments/default ports, and dropping common tracking parameters.
Content hashes catch byte-identical pages. The model must emit a stable key based on
project + event + version/date, and code normalizes it. Existing exact keys win; a
title similarity of at least 0.90 maps a new source back to a saved development.
Multiple source URLs are then unioned under that development.

Example: a GitHub release and an arXiv abstract both describing “Project V1 weights
released” should emit `project-v1-weights-release`. The second URL becomes additional
support, not another ranked item. The item is `still` when that key was in the prior
top K, `new` otherwise, and prior top-K keys absent from the new ranking are `dropped`.

One failure case is semantically identical releases described with dissimilar titles
and different model-generated keys. For example, “Project V2 weights published” and
“New checkpoint for the generalist policy” may fall below the 0.90 title-similarity
threshold and be treated as two developments even when both sources describe the
same release.

## 4. Failure

The relevant classification is in `tracker/errors.py`:

```python
if status_code == 429:
    if _DAILY_QUOTA.search(body):
        return Failure("daily_quota", False, ...)
    return Failure("rate_limit", True, ..., retry_after)
```

A per-minute 429 is transient: the runtime honors numeric `Retry-After` when present,
otherwise uses capped exponential backoff, and stops after the configured retry cap.
A body indicating daily quota, exhausted credits, or billing is terminal and stops
the run immediately. Bad credentials (401/403) and payment failures (402) are also
terminal. A retry still consumes model-call/search-credit budget, so retries cannot
silently multiply spend.

## 5. Budget

The configured maximum is 120,000 input tokens and 16,000 output tokens. At the
default Groq GPT-OSS 120B rates recorded on 2026-10-12 ($0.15/M input and $0.60/M
output), that token ceiling is at most `$0.018 + $0.0096 = $0.0276` for model usage.
Four Tavily advanced searches cost two credits each, or eight credits per run. At
the published pay-as-you-go rate of $0.008/credit, search is at most $0.064, making
the configured paid estimate at most $0.0916 per run. The independent `$0.50` model
cost cap remains a final fail-safe if token prices are changed.

Tavily's published free allocation is 1,000 credits/month. One maximal run per day
uses at most 248 credits in a 31-day month, so that allowance does not run out on a
daily schedule. Without a monthly reset, the 126th maximal run would exceed it, but
that is not how the stated monthly allowance works. Groq account limits vary by plan
and are expressed through account-specific rate limits rather than one known shared
monthly allocation. Therefore no defensible exhaustion day can be named from public
limits alone: Tavily lasts through every month at one run per day, and the Groq
console must be checked for the account in use. The runtime's local ceilings still
stop any individual run before it exceeds the configured budget.

Provider pricing and limits are external facts and should be rechecked before the
submission run:

- <https://console.groq.com/docs/models>
- <https://www.tavily.com/pricing>
