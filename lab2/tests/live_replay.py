"""Observe an already-authorized live run's duplicate handling without starting work.

Run this while a normal UI/CLI run is active. It only posts request IDs already
observed in that worker's current-process trace, and stops if the local service
has restarted. Request data must match that run's unchanged policy/topic/K.
These checks are actual HTTP observations, not synthetic report artifacts.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import yaml
from dotenv import dotenv_values

from common.schemas import ResearchPayload, VerifyPayload, WorkerRequest
from orchestrator.client import validate_response
from scripts import manage


ROOT = Path(__file__).resolve().parents[1]


def trace_events(worker, run_id):
    path = ROOT / ".state" / "traces" / f"{worker}.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # Another process may be appending its final line.
        if event.get("run_id") == run_id and event.get("service") == worker:
            events.append(event)
    return events


def current_process(worker):
    record = manage.load_record(worker)
    actual = manage.process_identity(record["pid"]) if record else None
    # macOS can resolve a venv's executable after the launcher's first PID
    # snapshot. Require the same birth timestamp and every non-executable arg.
    if (
        not record
        or not actual
        or record["started"] != actual["started"]
        or (shlex.split(record["command"])[1:] != shlex.split(actual["command"])[1:])
        or not manage.owns_process(actual, worker, actual)
    ):
        raise RuntimeError("The current local worker process cannot be established")
    return actual


def outgoing_counts(events):
    counts = {}
    for event in events:
        if event.get("direction") == "out":
            tool = event["endpoint_or_tool"]
            counts[tool] = counts.get(tool, 0) + 1
    return counts


def digest(body):
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class LiveReplayTrial:
    def __init__(self, args):
        self.args = args
        self.run_id = str(UUID(args.run_id))
        self.policy = yaml.safe_load(Path(args.config).read_text())
        self.topic = args.topic or self.policy["topic"]
        self.k = args.k or self.policy["top_k"]
        self.deadline = time.monotonic() + args.timeout
        self.identities = {worker: current_process(worker) for worker in ("researcher", "verifier")}
        values = {key: value for key, value in dotenv_values(ROOT / ".env").items() if value}
        names = ("RESEARCHER_TOKEN", "VERIFIER_TOKEN", "RESEARCHER_URL", "VERIFIER_URL")
        values.update({key: os.environ[key] for key in names if key in os.environ})
        self.urls = {
            worker: values.get(f"{worker.upper()}_URL", f"http://127.0.0.1:{port}")
            for worker, port in (("researcher", 8011), ("verifier", 8012))
        }
        self.tokens = {worker: values[f"{worker.upper()}_TOKEN"] for worker in self.urls}
        for url in self.urls.values():
            if urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise RuntimeError("This trial requires locally identifiable worker processes")
        self.evidence = {
            "kind": "actual_http_live_duplicate_checks",
            "run_id": self.run_id,
            "started_at": datetime.now(UTC).isoformat(),
            "workers": {},
        }

    def save(self):
        target = Path(self.args.output or ROOT / ".state" / "trials" / f"replay-{self.run_id}.json")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.evidence, indent=2) + "\n")

    def assert_same_process(self, worker):
        if current_process(worker) != self.identities[worker]:
            raise RuntimeError("Worker restarted; replay could perform new work")

    async def wait_until(self, predicate):
        while time.monotonic() < self.deadline:
            result = predicate()
            if result:
                return result
            await asyncio.sleep(0.25)
        raise TimeoutError("Live replay observation timed out")

    async def known_request(self, worker, payload):
        def find_id():
            self.assert_same_process(worker)
            ids = {
                event.get("request_id")
                for event in trace_events(worker, self.run_id)
                if event.get("request_id")
            }
            if len(ids) > 1:
                raise RuntimeError("Multiple logical IDs make reconstruction ambiguous")
            return next(iter(ids), None)

        request_id = await self.wait_until(find_id)
        allocation = self.policy["workers"][worker]
        request = {
            "request_id": request_id,
            "run_id": self.run_id,
            "time_budget_ms": allocation["time_budget_ms"],
            "budget": allocation["budget"],
            "payload": payload,
        }
        WorkerRequest.model_validate(request)
        (ResearchPayload if worker == "researcher" else VerifyPayload).model_validate(payload)
        return request

    async def post(self, client, worker, request):
        self.assert_same_process(worker)
        if request["request_id"] not in {
            event.get("request_id") for event in trace_events(worker, self.run_id)
        }:
            raise RuntimeError("Refusing to submit an unobserved request ID")
        endpoint = "/research" if worker == "researcher" else "/verify"
        started = time.monotonic()
        response = await client.post(
            self.urls[worker].rstrip("/") + endpoint,
            json=request,
            headers={"Authorization": "Bearer " + self.tokens[worker]},
            timeout=10,
        )
        self.assert_same_process(worker)
        body = response.json()
        observation = {
            "http_status": response.status_code,
            "retry_after": response.headers.get("Retry-After"),
            "latency_ms": round((time.monotonic() - started) * 1000, 3),
            "body_sha256": digest(body),
        }
        if response.status_code == 409:
            raise RuntimeError("Original request reconstruction conflicted; no further POSTs")
        if response.status_code not in {200, 500, 503}:
            raise RuntimeError("Unexpected worker HTTP status")
        validated = validate_response(body, request, worker)
        observation.update(status=validated["status"], usage=validated["usage"])
        return response, body, observation

    async def worker_trial(self, client, worker, payload):
        request = await self.known_request(worker, payload)
        observations = {"request_id": request["request_id"]}
        self.evidence["workers"][worker] = observations
        response, body, first = await self.post(client, worker, request)
        observations["in_flight"] = first
        observations["in_flight_passed"] = response.status_code == 503 and bool(
            response.headers.get("Retry-After")
        )
        self.save()
        print(f"{worker}: observed duplicate HTTP {response.status_code}", flush=True)

        if response.status_code == 503:
            # A finish tool event can precede the original response; the verifier
            # starts only after that response. Otherwise await a final inbound event.
            observed_at = time.monotonic()

            def finished():
                self.assert_same_process(worker)
                events = trace_events(worker, self.run_id)
                terminal = any(
                    (event.get("direction") == "in" and str(event.get("status")) in {"200", "500"})
                    or (
                        worker == "researcher"
                        and event.get("endpoint_or_tool") == "tool/finish"
                        and event.get("status") == "ok"
                    )
                    for event in events
                )
                # Even a detached task must finish by its original elapsed deadline.
                return (
                    terminal
                    or time.monotonic() - observed_at > request["time_budget_ms"] / 1000 + 1
                )

            await self.wait_until(finished)
            await asyncio.sleep(0.1)  # Let the managed task finalize its response cache.

        before = outgoing_counts(trace_events(worker, self.run_id))
        cached = []
        bodies = []
        for _ in range(2):
            response, body, observation = await self.post(client, worker, request)
            cached.append(observation)
            bodies.append(body)
            if response.status_code not in {200, 500}:
                raise RuntimeError("Worker was not yet complete for cached replay check")
        after = outgoing_counts(trace_events(worker, self.run_id))
        observations.update(
            cached=cached,
            same_response=bodies[0] == bodies[1],
            outgoing_before=before,
            outgoing_after=after,
            no_new_calls=before == after,
        )
        observations["passed"] = (
            observations["in_flight_passed"]
            and observations["same_response"]
            and observations["no_new_calls"]
        )
        self.save()
        print(
            f"{worker}: cached replay checks {'PASS' if observations['passed'] else 'FAIL'}",
            flush=True,
        )
        return bodies[-1]

    async def run(self):
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                research = await self.worker_trial(
                    client, "researcher", {"topic": self.topic, "k": self.k}
                )
                if research["status"] == "failed" or not research["result"]["developments"]:
                    raise RuntimeError("Research produced no claims for verifier replay")
                claims = [
                    {key: claim[key] for key in ("claim_id", "text", "source_url")}
                    for item in research["result"]["developments"]
                    for claim in item["claims"]
                ]
                await self.worker_trial(client, "verifier", {"claims": claims})
            self.evidence["passed"] = all(
                item["passed"] for item in self.evidence["workers"].values()
            )
        except Exception as exc:
            self.evidence.update(passed=False, error_type=type(exc).__name__, error=str(exc))
            print(f"Live duplicate trial stopped: {type(exc).__name__}", flush=True)
        finally:
            self.evidence["completed_at"] = datetime.now(UTC).isoformat()
            self.save()
        return self.evidence["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--topic")
    parser.add_argument("--k", type=int)
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--output")
    args = parser.parse_args()
    return 0 if asyncio.run(LiveReplayTrial(args).run()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
