"""Worker HTTP boundary: frozen retries, strict responses, conservative accounting."""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from common.schemas import Development, Error, Verdict, WorkerResponse
from .trace import trace


@dataclass
class Outcome:
    worker: str
    response: dict | None
    charged: dict
    error: str | None = None


def validate_response(body: dict, request: dict, worker: str) -> dict:
    """Validate relationships as well as types; nothing unvalidated reaches a report."""
    # Usage() has convenient zero defaults for a new worker context, but all
    # counters are required on the wire. Missing counters cannot mean free work.
    if not isinstance(body, dict) or not isinstance(body.get("usage"), dict):
        raise ValueError("Missing worker usage")
    if set(body["usage"]) != {"steps", "fetches", "tokens"}:
        raise ValueError("Worker usage must include every counter")
    response = WorkerResponse.model_validate(body).model_dump(mode="json")
    if any(response[key] != request[key] for key in ("run_id", "request_id")):
        raise ValueError("Worker correlation IDs do not match")
    for counter in ("steps", "fetches", "tokens"):
        used = response["usage"][counter]
        if used < 0 or used > request["budget"]["max_" + counter]:
            raise ValueError("Worker exceeded its allocation")
    result = response["result"]
    complete = response["status"] == "complete"
    if response["status"] != "complete" and not response.get("error"):
        raise ValueError("Missing reason for incomplete work")
    if worker == "researcher":
        if set(result) != {"developments"}:
            raise ValueError("Invalid research result keys")
        developments = [Development.model_validate(item) for item in result["developments"]]
        ids = [item.id for item in developments]
        claims = [claim.claim_id for item in developments for claim in item.claims]
        if len(set(ids)) != len(ids) or len(set(claims)) != len(claims):
            raise ValueError("Duplicate development or claim ID")
        if len(developments) > request["payload"]["k"] or (
            complete and len(developments) != request["payload"]["k"]
        ):
            raise ValueError("Complete research must contain exactly K distinct developments")
    else:
        if set(result) != {"verdicts"}:
            raise ValueError("Invalid verification result keys")
        verdicts = [Verdict.model_validate(item) for item in result["verdicts"]]
        sent = {claim["claim_id"] for claim in request["payload"]["claims"]}
        received = [item.claim_id for item in verdicts]
        if len(received) != len(set(received)) or not set(received) <= sent:
            raise ValueError("Unknown or duplicate verdict IDs")
        if complete and set(received) != sent:
            raise ValueError("Complete verification omitted claims")
        for verdict in verdicts:
            if verdict.verdict == "supported":
                if (
                    not verdict.evidence
                    or not verdict.evidence.strip()
                    or len(verdict.evidence) > 300
                ):
                    raise ValueError("Invalid supported evidence")
            elif verdict.evidence is not None:
                raise ValueError("Evidence must be null for negative verdicts")
    return response


def retry_delay(header: str | None, fallback: float) -> float:
    if not header:
        return fallback
    try:
        return max(fallback, float(header))
    except ValueError:
        try:
            return max(
                fallback, (parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds()
            )
        except (ValueError, TypeError):
            return fallback


def validate_rejection(body: dict) -> None:
    """Pre-admission errors cannot echo IDs from an unparsed or invalid request."""
    if not isinstance(body, dict) or set(body) != {"error"}:
        raise ValueError("Invalid rejection envelope")
    error = Error.model_validate(body["error"])
    if error.retryable:
        raise ValueError("Terminal worker rejection cannot be retryable")


async def call_worker(worker, base_url, token, request, policy, run_deadline, client=None):
    """Reserve once. A missing/invalid answer retains the full reserved allocation."""
    full_share = {name: request["budget"]["max_" + name] for name in ("steps", "fetches", "tokens")}
    outcome = Outcome(worker, None, full_share, "worker did not answer")
    endpoint = "/research" if worker == "researcher" else "/verify"
    # Serialize once, outside the retry loop. Never edit time_budget_ms on retry.
    frozen_body = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
    owns_client = client is None
    client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
    timeout = request["time_budget_ms"] / 1000 + policy["cold_start_allowance_seconds"]
    try:
        for attempt in range(1, policy["max_attempts"] + 1):
            remaining = run_deadline - time.monotonic()
            # Never begin a request with a caller allowance shorter than the worker's.
            if remaining <= timeout:
                outcome.error = "orchestration deadline leaves insufficient worker allowance"
                break
            started = time.monotonic()
            traced = False
            delay = policy["backoff_seconds"] * 2 ** (attempt - 1)
            try:
                async with asyncio.timeout(timeout):
                    async with client.stream(
                        "POST",
                        base_url.rstrip("/") + endpoint,
                        content=frozen_body,
                        headers={
                            "Authorization": "Bearer " + token,
                            "Content-Type": "application/json",
                        },
                        timeout=timeout,
                    ) as response:
                        content = bytearray()
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 1_000_000:
                                raise ValueError("Worker response exceeds size limit")
                        code = response.status_code
                        trace(
                            run_id=request["run_id"],
                            request_id=request["request_id"],
                            endpoint_or_tool=endpoint,
                            status=code,
                            attempt=attempt,
                            latency_ms=(time.monotonic() - started) * 1000,
                        )
                        traced = True
                        if code == 503:
                            parsed = validate_response(json.loads(content), request, worker)
                            if parsed["status"] != "failed" or not parsed["error"]["retryable"]:
                                raise ValueError("Worker 503 requires a retryable failure envelope")
                            delay = retry_delay(response.headers.get("Retry-After"), delay)
                            outcome.error = f"{worker} unavailable (503)"
                        elif code == 200 or code == 500:
                            parsed = validate_response(json.loads(content), request, worker)
                            if (code == 500) != (parsed["status"] == "failed"):
                                raise ValueError("HTTP and worker status disagree")
                            if code == 500 and parsed["error"]["retryable"]:
                                raise ValueError("Worker 500 cannot be retryable")
                            outcome.response = parsed
                            outcome.charged = parsed["usage"]
                            outcome.error = parsed["error"]["code"] if parsed.get("error") else None
                            return outcome
                        else:
                            if code in {400, 401, 409}:
                                validate_rejection(json.loads(content))
                            outcome.error = f"terminal worker HTTP {code}"
                            return outcome  # In particular: never retry 400/401/409/500.
            except (httpx.TransportError, TimeoutError):
                trace(
                    run_id=request["run_id"],
                    request_id=request["request_id"],
                    endpoint_or_tool=endpoint,
                    status="transport_error",
                    attempt=attempt,
                    latency_ms=(time.monotonic() - started) * 1000,
                )
                traced = True
                outcome.error = f"{worker} connection failure or timeout"
            except (ValueError, TypeError, KeyError):
                outcome.error = f"{worker} returned a malformed response"
                return outcome
            finally:
                # Oversized responses and cancellation can happen before the
                # normal response event, but the outgoing call still happened.
                if not traced:
                    trace(
                        run_id=request["run_id"],
                        request_id=request["request_id"],
                        endpoint_or_tool=endpoint,
                        status="incomplete_response",
                        attempt=attempt,
                        latency_ms=(time.monotonic() - started) * 1000,
                    )
            if attempt < policy["max_attempts"]:
                if time.monotonic() + delay + timeout >= run_deadline:
                    break
                await asyncio.sleep(delay)
        return outcome
    finally:
        if owns_client:
            await client.aclose()
