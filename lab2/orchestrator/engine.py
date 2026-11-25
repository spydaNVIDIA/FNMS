"""Deterministic routing. Worker data never controls destinations or budgets."""

import copy
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from .client import call_worker
from .config import ROOT


def load_policy():
    path = Path(os.getenv("LAB2_CONFIG", str(ROOT / "config.yaml")))
    policy = yaml.safe_load(path.read_text())
    for field in ("max_steps", "max_fetches", "max_tokens"):
        total = policy["budget"][field]
        shares = [
            policy["workers"][worker]["budget"][field] for worker in ("researcher", "verifier")
        ]
        if total < 0 or any(share < 0 for share in shares) or sum(shares) > total:
            raise ValueError("Worker allocations must fit inside the total run budget")
    if policy["network"]["cold_start_allowance_seconds"] <= 0:
        raise ValueError("Caller timeout must exceed worker allowance")
    return policy


def envelope(run_id, allocation, payload):
    return dict(
        request_id=str(uuid4()),
        run_id=run_id,
        time_budget_ms=allocation["time_budget_ms"],
        budget=copy.deepcopy(allocation["budget"]),
        payload=payload,
    )


async def orchestrate(run_id, topic, k, previous, policy=None, caller=call_worker):
    policy = policy or load_policy()
    deadline = time.monotonic() + policy["network"]["run_timeout_seconds"]
    ledger = {}
    failures = []

    async def send(worker, payload):
        request = envelope(run_id, policy["workers"][worker], payload)
        # Reserve before dispatch; a timeout does not release this reservation.
        reservation = {
            key: request["budget"]["max_" + key] for key in ("steps", "fetches", "tokens")
        }
        for key, value in reservation.items():
            if (
                value + sum(item["charged"][key] for item in ledger.values())
                > policy["budget"]["max_" + key]
            ):
                raise ValueError("Run budget would be exceeded")
        ledger[request["request_id"]] = {
            "worker": worker,
            "allocated": reservation,
            "charged": reservation.copy(),
            "reported": None,
        }
        default_url = "http://127.0.0.1:8011" if worker == "researcher" else "http://127.0.0.1:8012"
        result = await caller(
            worker,
            os.getenv(worker.upper() + "_URL", default_url),
            os.getenv(worker.upper() + "_TOKEN", ""),
            request,
            policy["network"],
            deadline,
        )
        entry = ledger[request["request_id"]]
        entry["charged"] = result.charged
        entry["reported"] = result.response["usage"] if result.response else None
        if result.error or not result.response or result.response["status"] != "complete":
            failures.append({"worker": worker, "reason": result.error or "partial work"})
        return result

    research = await send("researcher", {"topic": topic, "k": k})
    fresh = (
        research.response["result"]["developments"]
        if research.response and research.response["status"] != "failed"
        else []
    )
    stale = not fresh
    developments = copy.deepcopy(fresh if fresh else previous[:k])
    verdicts = {}
    if fresh:
        # Explicit projection prevents summaries, reasoning, or pages crossing the boundary.
        claims = [
            {key: claim[key] for key in ("claim_id", "text", "source_url")}
            for item in fresh
            for claim in item["claims"]
        ]
        verification = await send("verifier", {"claims": claims})
        if verification.response and verification.response["status"] != "failed":
            verdicts = {
                item["claim_id"]: item for item in verification.response["result"]["verdicts"]
            }
    previous_ids = {item["id"] for item in previous}
    for rank, item in enumerate(developments, 1):
        item.update(rank=rank, stale=stale, bucket="still" if item["id"] in previous_ids else "new")
        for claim in item["claims"]:
            verdict = verdicts.get(claim["claim_id"])
            # Old evidence may remain in the previous snapshot, but no current verification occurred.
            claim["verdict"] = verdict["verdict"] if verdict else "unverified"
            claim["evidence"] = verdict["evidence"] if verdict else None
    current_ids = {item["id"] for item in developments}
    snapshot = dict(
        run_id=run_id,
        topic=topic,
        k=k,
        status="partial" if failures or stale else "complete",
        message=(
            "No new results; previous ranking is stale."
            if previous
            else "No new results; no previous ranking is available."
        )
        if stale
        else "Research and verification results.",
        failed_workers=failures,
        developments=developments,
        dropped=[item["title"] for item in previous if item["id"] not in current_ids]
        if not stale
        else [],
        ledger=ledger,
        total_budget=policy["budget"],
        charged_usage={
            key: sum(item["charged"][key] for item in ledger.values())
            for key in ("steps", "fetches", "tokens")
        },
        completed_at=datetime.now(UTC).isoformat(),
    )
    return snapshot


def markdown(snapshot):
    lines = [
        "# Lab 2 robotics tracker",
        "",
        f"Run: {snapshot['run_id']}",
        f"Status: {snapshot['status']}",
        snapshot["message"],
        "",
    ]
    for failure in snapshot["failed_workers"]:
        lines.append(f"Worker {failure['worker']}: {failure['reason']}")
    for item in snapshot["developments"]:
        lines += [
            "",
            f"## {item['rank']}. {item['title']} ({'stale' if item['stale'] else item['bucket']})",
            item["summary"],
        ]
        for claim in item["claims"]:
            lines += [f"- [{claim['verdict']}] {claim['text']}", f"  Source: {claim['source_url']}"]
            if claim["evidence"]:
                lines.append(f"  Evidence: {claim['evidence']}")
    lines += ["", "Dropped: " + (", ".join(snapshot["dropped"]) or "None"), ""]
    return "\n".join(lines)
