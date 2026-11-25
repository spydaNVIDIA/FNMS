"""Actual loopback-HTTP outage trials using an explicitly labeled saved-result fixture.

No application servers or databases are contacted. This script never calls providers.
"""

import argparse
import asyncio
import copy
from datetime import UTC, datetime
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class FixtureServer(ThreadingHTTPServer):
    """Serve a historical research result under fresh correlation IDs, only on loopback."""

    def __init__(self, saved_response, token):
        self.saved_response = saved_response
        self.token = token
        self.receipts = []
        super().__init__(("127.0.0.1", 0), FixtureHandler)


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        request = json.loads(raw)
        self.server.receipts.append(
            {
                "path": self.path,
                "request_id": request["request_id"],
                "run_id": request["run_id"],
                "body_sha256": hashlib.sha256(raw).hexdigest(),
                "k": request["payload"].get("k"),
            }
        )
        if not secrets.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + self.server.token
        ):
            self.send_error(401)
            return
        if self.path != "/research":
            self.send_error(404)
            return
        response = copy.deepcopy(self.server.saved_response)
        response["request_id"] = request["request_id"]
        response["run_id"] = request["run_id"]
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def trial_policy(baseline):
    """Preserve historical budget shares; shorten only these offline transport deadlines."""
    workers = {}
    for entry in baseline["ledger"].values():
        workers[entry["worker"]] = {
            "time_budget_ms": 200,
            "budget": {"max_" + name: value for name, value in entry["allocated"].items()},
        }
    if set(workers) != {"researcher", "verifier"}:
        raise ValueError("Baseline must include both worker allocations")
    return {
        "budget": copy.deepcopy(baseline["total_budget"]),
        "workers": workers,
        "network": {
            "cold_start_allowance_seconds": 0.3,
            "max_attempts": 3,
            "backoff_seconds": 0.01,
            "run_timeout_seconds": 5,
        },
    }


def claims(snapshot):
    return [claim for item in snapshot["developments"] for claim in item["claims"]]


def read_events(directory, run_id):
    return [
        event
        for line in (directory / "orchestrator.jsonl").read_text().splitlines()
        if (event := json.loads(line))["run_id"] == run_id
    ]


def run_trials(saved_response, baseline):
    sys.path.insert(0, str(ROOT))
    trace_dir = ROOT / ".state" / "offline-network-failures" / str(uuid4())
    os.environ["TRACE_DIR"] = str(trace_dir)
    os.environ["JWT_SECRET"] = secrets.token_urlsafe(32)
    os.environ["LIVE_CALLS_APPROVED"] = "false"
    for key in ("MODEL_API_KEY", "TAVILY_API_KEY"):
        os.environ.pop(key, None)
    from orchestrator.engine import orchestrate

    token = secrets.token_urlsafe(24)
    os.environ["RESEARCHER_TOKEN"] = token
    os.environ["VERIFIER_TOKEN"] = token
    previous = copy.deepcopy(baseline["developments"])
    policy = trial_policy(baseline)
    k = baseline["k"]  # Preserve the actual requested K even when the saved run is partial.
    topic = baseline["topic"]
    cases = []
    server = FixtureServer(saved_response, token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Reserve, but do not listen on, a distinct ephemeral port. TCP connections fail
    # without a port-reuse race or any contact with the running application's workers.
    blocked = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocked.bind(("127.0.0.1", 0))
    live_url = f"http://127.0.0.1:{server.server_port}"
    down_url = f"http://127.0.0.1:{blocked.getsockname()[1]}"
    try:
        os.environ["RESEARCHER_URL"] = live_url
        os.environ["VERIFIER_URL"] = down_url
        run_id = str(uuid4())
        started = time.monotonic()
        snapshot = asyncio.run(orchestrate(run_id, topic, k, previous, policy))
        events = read_events(trace_dir, run_id)
        assert snapshot["status"] == "partial" and snapshot["k"] == k
        assert len(snapshot["developments"]) == len(saved_response["result"]["developments"])
        assert all(not item["stale"] for item in snapshot["developments"])
        assert all(c["verdict"] == "unverified" and c["evidence"] is None for c in claims(snapshot))
        assert any(item["worker"] == "verifier" for item in snapshot["failed_workers"])
        verifier_entries = [
            item for item in snapshot["ledger"].values() if item["worker"] == "verifier"
        ]
        assert len(verifier_entries) == 1
        assert verifier_entries[0]["charged"] == verifier_entries[0]["allocated"]
        attempts = [event for event in events if event["endpoint_or_tool"] == "/verify"]
        assert [event["attempt"] for event in attempts] == [1, 2, 3]
        assert all(event["status"] == "transport_error" for event in attempts)
        assert len({event["request_id"] for event in attempts}) == 1
        cases.append(
            {
                "name": "verifier_down",
                "status": "passed",
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "assertions": [
                    "partial",
                    "all claims unverified",
                    "evidence cleared",
                    "fresh findings retained",
                    "three real TCP failures",
                    "one UUID charge at full verifier allocation",
                ],
                "snapshot": snapshot,
                "http_events": events,
            }
        )

        os.environ["RESEARCHER_URL"] = down_url
        os.environ["VERIFIER_URL"] = live_url
        run_id = str(uuid4())
        started = time.monotonic()
        snapshot = asyncio.run(orchestrate(run_id, topic, k, previous, policy))
        events = read_events(trace_dir, run_id)
        assert snapshot["status"] == "partial" and snapshot["k"] == k
        assert [item["id"] for item in snapshot["developments"]] == [
            item["id"] for item in previous[:k]
        ]
        assert all(item["stale"] for item in snapshot["developments"])
        assert all(c["verdict"] == "unverified" and c["evidence"] is None for c in claims(snapshot))
        assert "No new results" in snapshot["message"]
        assert len(snapshot["ledger"]) == 1
        entry = next(iter(snapshot["ledger"].values()))
        assert entry["worker"] == "researcher" and entry["charged"] == entry["allocated"]
        assert len(events) == 3 and all(
            event["endpoint_or_tool"] == "/research" for event in events
        )
        assert all(event["status"] == "transport_error" for event in events)
        assert len({event["request_id"] for event in events}) == 1
        assert previous == baseline["developments"]
        assert len(server.receipts) == 1 and server.receipts[0]["k"] == k
        cases.append(
            {
                "name": "researcher_down",
                "status": "passed",
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "assertions": [
                    "partial",
                    "original K retained",
                    "prior findings stale and unverified",
                    "no new results message",
                    "verifier never called",
                    "three real TCP failures",
                    "full researcher allocation charged once",
                    "baseline unchanged",
                ],
                "snapshot": snapshot,
                "http_events": events,
            }
        )
        return cases, server.receipts
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        blocked.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-response", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "evidence" / "offline-network-failures.json"
    )
    args = parser.parse_args()
    saved_response = json.loads(args.research_response.read_text())
    baseline = json.loads(args.baseline.read_text())
    cases, receipts = run_trials(saved_response, baseline)
    result = {
        "evidence_kind": "offline_actual_http_failure_trial",
        "status": "passed",
        "created_at": datetime.now(UTC).isoformat(),
        "fixture_source_run_id": baseline["run_id"],
        "fixture_response_sha256": hashlib.sha256(args.research_response.read_bytes()).hexdigest(),
        "baseline_sha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
        "original_k": baseline["k"],
        "saved_development_count": len(baseline["developments"]),
        "new_provider_calls": 0,
        "new_provider_tokens": 0,
        "database_access": False,
        "live_workers_contacted": False,
        "usage_note": "Worker usage in fixture snapshots is historical replay data, not new provider spend.",
        "fixture_http_receipts": receipts,
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"cases", "fixture_http_receipts"}
            },
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "cases": [
                    {
                        "name": item["name"],
                        "status": item["status"],
                        "elapsed_seconds": item["elapsed_seconds"],
                    }
                    for item in cases
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
