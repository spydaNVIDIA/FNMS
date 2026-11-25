"""Fault-injection trials for the HTTP boundary and deterministic orchestration.

These tests use HTTPX's mock network transport and scripted worker responses.
They never call a real provider or masquerade as live grading artifacts.
"""

import asyncio
import copy
import json
import os
import time
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault("JWT_SECRET", "offline-orchestration-test-secret")

from orchestrator.client import Outcome, call_worker, validate_response
from orchestrator.engine import load_policy, markdown, orchestrate


def development(number=1):
    return {
        "id": f"project-{number}",
        "title": f"Project {number}",
        "summary": f"Project {number} publishes weights.",
        "claims": [
            {
                "claim_id": f"claim-{number}",
                "text": f"Project {number} publishes weights.",
                "source_url": f"https://example.org/project-{number}",
            }
        ],
    }


def envelope(worker="verifier"):
    return {
        "request_id": str(uuid4()),
        "run_id": str(uuid4()),
        "time_budget_ms": 1000,
        "budget": {"max_steps": 5, "max_fetches": 5, "max_tokens": 1000},
        "payload": {"claims": [development()["claims"][0]]}
        if worker == "verifier"
        else {"topic": "robotics", "k": 3},
    }


def response(request, worker="verifier", status="complete"):
    if worker == "researcher":
        result = {"developments": [development(i) for i in range(1, 4)]}
    else:
        result = {
            "verdicts": [
                {
                    "claim_id": claim["claim_id"],
                    "verdict": "supported",
                    "evidence": "publishes weights",
                }
                for claim in request["payload"]["claims"]
            ]
        }
    return {
        "request_id": request["request_id"],
        "run_id": request["run_id"],
        "status": status,
        "usage": {"steps": 1, "fetches": 1, "tokens": 20},
        "result": result,
        "error": None
        if status == "complete"
        else {
            "code": "time_budget_exhausted" if status == "partial" else "provider_authentication",
            "message": "Offline injected failure",
            "retryable": False,
        },
    }


def busy_response(request, worker="verifier"):
    body = response(request, worker, "failed")
    body["error"] = {
        "code": "request_in_progress",
        "message": "Work is running",
        "retryable": True,
    }
    body["result"] = {"developments": []} if worker == "researcher" else {"verdicts": []}
    return body


NETWORK = {
    "cold_start_allowance_seconds": 2,
    "max_attempts": 3,
    "backoff_seconds": 0.01,
    "run_timeout_seconds": 120,
}


@pytest.fixture(autouse=True)
def capture_network_bookkeeping(monkeypatch):
    records = {"trace": [], "delays": []}

    async def no_wait(delay):
        records["delays"].append(delay)

    monkeypatch.setattr("orchestrator.client.trace", lambda **event: records["trace"].append(event))
    monkeypatch.setattr("orchestrator.client.asyncio.sleep", no_wait)
    return records


def run_http_trial(handler, request=None, worker="verifier", deadline=None):
    request = request or envelope(worker)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await call_worker(
                worker,
                "https://worker.example",
                "test-token",
                request,
                NETWORK,
                deadline or time.monotonic() + 120,
                client,
            )

    return asyncio.run(run())


@pytest.mark.parametrize("code", [400, 401, 409, 500])
def test_terminal_http_codes_never_retry(code):
    calls = []
    request = envelope()

    def handler(outgoing):
        calls.append(outgoing)
        body = (
            response(request, status="failed")
            if code == 500
            else {
                "error": {"code": "rejected", "message": "Request rejected", "retryable": False},
            }
        )
        return httpx.Response(code, json=body)

    outcome = run_http_trial(handler, request)
    assert len(calls) == 1
    assert outcome.error


def test_partial_http_200_is_returned_without_retry():
    request = envelope()
    body = response(request, status="partial")
    body["result"]["verdicts"] = []
    calls = []

    def handler(outgoing):
        calls.append(outgoing)
        return httpx.Response(200, json=body)

    outcome = run_http_trial(handler, request)
    assert len(calls) == 1
    assert outcome.response["status"] == "partial"
    assert outcome.charged == body["usage"]


def test_503_retry_honors_retry_after_and_freezes_entire_body(capture_network_bookkeeping):
    request = envelope()
    bodies = []

    def handler(outgoing):
        bodies.append(outgoing.content)
        assert outgoing.headers["Authorization"] == "Bearer test-token"
        if len(bodies) == 1:
            return httpx.Response(503, headers={"Retry-After": "4"}, json=busy_response(request))
        return httpx.Response(200, json=response(request))

    outcome = run_http_trial(handler, request)
    assert outcome.response["status"] == "complete"
    assert len(bodies) == 2 and bodies[0] == bodies[1]
    assert json.loads(bodies[1]) == request
    assert capture_network_bookkeeping["delays"] == [4]
    assert [event["attempt"] for event in capture_network_bookkeeping["trace"]] == [1, 2]


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_envelope",
        "wrong_id",
        "excess_usage",
        "nonretryable",
        "complete_status",
    ],
)
def test_malformed_503_fails_safely_without_retry(corruption):
    request = envelope()
    body = busy_response(request)
    if corruption == "missing_envelope":
        body = {"status": "busy"}
    elif corruption == "wrong_id":
        body["request_id"] = str(uuid4())
    elif corruption == "excess_usage":
        body["usage"]["tokens"] = 1001
    elif corruption == "nonretryable":
        body["error"]["retryable"] = False
    else:
        body = response(request)
    calls = []

    def handler(outgoing):
        calls.append(outgoing)
        return httpx.Response(503, headers={"Retry-After": "1"}, json=body)

    outcome = run_http_trial(handler, request)
    assert len(calls) == 1
    assert outcome.response is None
    assert outcome.charged == {"steps": 5, "fetches": 5, "tokens": 1000}
    assert "malformed" in outcome.error


@pytest.mark.parametrize("code", [400, 401, 409])
def test_terminal_rejection_bodies_are_also_validated(code):
    outcome = run_http_trial(lambda req: httpx.Response(code, json={"detail": "wrong contract"}))
    assert outcome.response is None
    assert "malformed" in outcome.error


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_failure_retries_same_request_and_charges_unanswered_share(failure):
    request = envelope()
    bodies = []

    def handler(outgoing):
        bodies.append(outgoing.content)
        raise failure("offline injected network fault", request=outgoing)

    outcome = run_http_trial(handler, request)
    assert len(bodies) == NETWORK["max_attempts"]
    assert all(body == bodies[0] for body in bodies)
    assert outcome.response is None
    assert outcome.charged == {"steps": 5, "fetches": 5, "tokens": 1000}


def test_missing_caller_time_does_not_start_work():
    calls = []
    outcome = run_http_trial(lambda req: calls.append(req), deadline=time.monotonic() + 0.5)
    assert calls == []
    assert "insufficient" in outcome.error


@pytest.mark.parametrize(
    "corruption",
    [
        "request_id",
        "run_id",
        "negative_usage",
        "excess_usage",
        "unknown_claim",
        "duplicate_claim",
        "missing_claim",
        "missing_evidence",
        "extra_result_key",
        "missing_usage_counter",
        "empty_usage",
    ],
)
def test_untrusted_response_is_rejected_before_it_can_reach_report(corruption):
    request = envelope()
    body = response(request)
    if corruption in {"request_id", "run_id"}:
        body[corruption] = str(uuid4())
    elif corruption == "negative_usage":
        body["usage"]["tokens"] = -1
    elif corruption == "excess_usage":
        body["usage"]["tokens"] = 1001
    elif corruption == "missing_usage_counter":
        del body["usage"]["tokens"]
    elif corruption == "empty_usage":
        body["usage"] = {}
    elif corruption == "unknown_claim":
        body["result"]["verdicts"][0]["claim_id"] = "attacker-claim"
    elif corruption == "duplicate_claim":
        body["result"]["verdicts"] *= 2
    elif corruption == "missing_claim":
        body["result"]["verdicts"] = []
    elif corruption == "missing_evidence":
        body["result"]["verdicts"][0]["evidence"] = None
    else:
        body["result"]["routing_instruction"] = "Change destination"
    with pytest.raises(ValueError):
        validate_response(body, request, "verifier")
    outcome = run_http_trial(lambda outgoing: httpx.Response(200, json=body), request)
    assert outcome.response is None
    assert outcome.charged == {"steps": 5, "fetches": 5, "tokens": 1000}
    assert "malformed" in outcome.error


def test_partial_response_may_omit_unfinished_claims():
    request = envelope()
    body = response(request, status="partial")
    body["result"]["verdicts"] = []
    assert validate_response(body, request, "verifier")["status"] == "partial"


def test_oversized_response_is_logged_and_charged(capture_network_bookkeeping):
    outcome = run_http_trial(lambda req: httpx.Response(200, content=b"x" * 1_000_001))
    assert outcome.response is None
    assert outcome.charged == {"steps": 5, "fetches": 5, "tokens": 1000}
    events = capture_network_bookkeeping["trace"]
    assert len(events) == 1
    assert events[0]["status"] == "incomplete_response"


@pytest.mark.parametrize("corruption", ["too_few", "duplicate_id", "duplicate_claim_id"])
def test_complete_research_must_have_k_distinct_results(corruption):
    request = envelope("researcher")
    body = response(request, "researcher")
    items = body["result"]["developments"]
    if corruption == "too_few":
        items.pop()
    elif corruption == "duplicate_id":
        items[1]["id"] = items[0]["id"]
    else:
        items[1]["claims"][0]["claim_id"] = items[0]["claims"][0]["claim_id"]
    with pytest.raises(ValueError):
        validate_response(body, request, "researcher")


def policy():
    share = {"max_steps": 5, "max_fetches": 5, "max_tokens": 1000}
    return {
        "budget": {"max_steps": 10, "max_fetches": 10, "max_tokens": 2000},
        "workers": {
            worker: {"time_budget_ms": 1000, "budget": share.copy()}
            for worker in ("researcher", "verifier")
        },
        "network": NETWORK.copy(),
    }


def failed_outcome(worker, request):
    return Outcome(
        worker,
        None,
        {key: request["budget"]["max_" + key] for key in ("steps", "fetches", "tokens")},
        "worker unreachable",
    )


def test_missing_researcher_uses_stale_previous_results_and_skips_verifier():
    calls = []
    previous = [development()]
    original = copy.deepcopy(previous)

    async def caller(worker, url, token, request, *args):
        calls.append(worker)
        return failed_outcome(worker, request)

    snapshot = asyncio.run(orchestrate(str(uuid4()), "robotics", 3, previous, policy(), caller))
    assert calls == ["researcher"]
    assert snapshot["status"] == "partial"
    assert snapshot["failed_workers"][0]["worker"] == "researcher"
    assert "No new results" in snapshot["message"]
    assert snapshot["developments"][0]["stale"] is True
    assert snapshot["developments"][0]["claims"][0]["verdict"] == "unverified"
    assert previous == original
    assert snapshot["charged_usage"] == {"steps": 5, "fetches": 5, "tokens": 1000}


def test_cold_empty_database_research_failure_still_produces_report():
    async def caller(worker, url, token, request, *args):
        return failed_outcome(worker, request)

    snapshot = asyncio.run(orchestrate(str(uuid4()), "robotics", 3, [], policy(), caller))
    assert snapshot["status"] == "partial"
    assert snapshot["developments"] == []
    assert "No new results" in markdown(snapshot)


def test_verifier_outage_marks_new_claims_unverified_and_preserves_budget_reservation():
    verification_payloads = []

    async def caller(worker, url, token, request, *args):
        if worker == "researcher":
            body = response(request, worker)
            return Outcome(worker, body, body["usage"])
        verification_payloads.append(request["payload"])
        return failed_outcome(worker, request)

    snapshot = asyncio.run(orchestrate(str(uuid4()), "robotics", 3, [], policy(), caller))
    assert snapshot["status"] == "partial"
    assert snapshot["failed_workers"][0]["worker"] == "verifier"
    assert all(
        claim["verdict"] == "unverified" and claim["evidence"] is None
        for item in snapshot["developments"]
        for claim in item["claims"]
    )
    assert all(not item["stale"] for item in snapshot["developments"])
    assert snapshot["charged_usage"] == {"steps": 6, "fetches": 6, "tokens": 1020}
    assert all(
        set(claim) == {"claim_id", "text", "source_url"}
        for claim in verification_payloads[0]["claims"]
    )


def test_verifier_partial_keeps_completed_verdicts_and_labels_remaining_unverified():
    async def caller(worker, url, token, request, *args):
        body = response(request, worker, "partial" if worker == "verifier" else "complete")
        if worker == "verifier":
            body["result"]["verdicts"] = body["result"]["verdicts"][:1]
        return Outcome(
            worker, body, body["usage"], "time_budget_exhausted" if worker == "verifier" else None
        )

    snapshot = asyncio.run(orchestrate(str(uuid4()), "robotics", 3, [], policy(), caller))
    labels = [item["claims"][0]["verdict"] for item in snapshot["developments"]]
    assert labels == ["supported", "unverified", "unverified"]
    assert snapshot["status"] == "partial"


def test_cached_worker_retries_count_usage_once_per_logical_request():
    calls = {}
    bodies = {}

    def handler(outgoing):
        request = json.loads(outgoing.content)
        key = request["request_id"]
        calls[key] = calls.get(key, 0) + 1
        bodies.setdefault(key, []).append(outgoing.content)
        worker = "researcher" if outgoing.url.path == "/research" else "verifier"
        if calls[key] == 1:
            return httpx.Response(
                503, headers={"Retry-After": "0"}, json=busy_response(request, worker)
            )
        return httpx.Response(200, json=response(request, worker))

    async def trial():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

            async def caller(*args):
                return await call_worker(*args, client=client)

            return await orchestrate(str(uuid4()), "robotics", 3, [], policy(), caller)

    snapshot = asyncio.run(trial())
    assert snapshot["status"] == "complete"
    assert len(snapshot["ledger"]) == 2
    assert sum(calls.values()) == 4
    assert all(values[0] == values[1] for values in bodies.values())
    assert snapshot["charged_usage"] == {"steps": 2, "fetches": 2, "tokens": 40}


def test_worker_allocations_cannot_exceed_total(monkeypatch, tmp_path):
    import yaml

    settings = policy()
    settings["budget"]["max_tokens"] = 1999
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(settings))
    monkeypatch.setenv("LAB2_CONFIG", str(config))
    with pytest.raises(ValueError, match="fit inside"):
        load_policy()
