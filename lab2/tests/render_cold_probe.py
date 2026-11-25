"""Measure an authorized Render idle wakeup through the real orchestrator caller.

Preparation does not contact Render. Execution requires an explicit confirmation
flag and waits for no one: it refuses an early probe. Reusing the smoke test's
IDs with a changed deadline distinguishes a surviving cache (409) from a lost
cache (200), while forbidden addresses and zero model tokens prevent paid work.
"""

import argparse
import asyncio
import copy
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values


AUTHORIZED_ORIGIN = "https://fnms-lab2-verifier.onrender.com"
ROOT = Path(__file__).resolve().parents[1]


def classify_probe(cold_status, cold_seconds, warm_status, warm_seconds):
    """Require observable cache loss and slow-versus-warm timing to claim wakeup."""
    if cold_status == 409:
        return "registry_retained_no_cold_restart_demonstrated"
    if cold_status != 200 or warm_status != 200:
        return "inconclusive_transport_or_contract_failure"
    if cold_seconds >= 10 and cold_seconds - warm_seconds >= 10:
        return "cache_loss_with_slow_wakeup_observed"
    return "registry_lost_without_slow_wakeup"


def original_request(smoke):
    return {
        "request_id": smoke["worker_request_id"],
        "run_id": smoke["fixture_run_id"],
        "time_budget_ms": 5000,
        "budget": {"max_steps": 0, "max_fetches": 10, "max_tokens": 0},
        "payload": {
            "claims": [
                {
                    "claim_id": f"blocked-{index}",
                    "text": "This is an explicit blocked-address smoke fixture.",
                    "source_url": url,
                }
                for index, url in enumerate(smoke["blocked_urls"], 1)
            ]
        },
    }


def ensure_authorized_window(args, smoke, now):
    if not args.confirm_root_go_and_idle:
        raise RuntimeError("Execution requires root GO and confirmation of uninterrupted idle time")
    eligible = datetime.fromisoformat(smoke["cold_start_eligible_at"])
    if now < eligible:
        raise RuntimeError(f"Probe is too early; eligible at {eligible.isoformat()}")
    if not smoke.get("passed") or smoke["origin"] != AUTHORIZED_ORIGIN:
        raise RuntimeError("A passing smoke record for the exact authorized origin is required")


async def run(args):
    smoke = json.loads(Path(args.smoke_evidence).read_text())
    ensure_authorized_window(args, smoke, datetime.now(UTC))
    application_root = Path(args.application_root).resolve()
    sys.path.insert(0, str(application_root))
    # Import the actual submitted application, not a fixture or replacement caller.
    from orchestrator.client import call_worker

    trace_dir = ROOT / "evidence" / "cold-start-trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TRACE_DIR"] = str(trace_dir)
    token = dotenv_values(application_root / ".env")["VERIFIER_TOKEN"]
    if not token:
        raise RuntimeError("VERIFIER_TOKEN is missing")

    original = original_request(smoke)
    changed = copy.deepcopy(original)
    changed["time_budget_ms"] = 6000
    policy = {"cold_start_allowance_seconds": 90, "max_attempts": 1, "backoff_seconds": 1}
    evidence = {
        "kind": "actual_render_idle_probe_via_orchestrator",
        "origin": AUTHORIZED_ORIGIN,
        "application_root": str(application_root),
        "smoke_last_contact_at": smoke["last_contact_at"],
        "earliest_eligible_at": smoke["cold_start_eligible_at"],
        "original_request": original,
        "probe_request": changed,
        "request_change": "Only time_budget_ms changes, from 5000 to 6000; both original UUIDs are reused.",
        "caller_policy": policy,
        "cold_caller_timeout_seconds": 96,
        "model_or_search_calls_requested": 0,
        "observations": [],
        "interpretation_limit": "Cache loss plus a slow first response and fast replay is evidence of process loss/wakeup. Render logs are needed to distinguish idle spin-down conclusively from deployment or another concurrent restart.",
    }

    async def invoke(label, request):
        statuses = []

        async def observe_response(response):
            statuses.append(response.status_code)

        started_at = datetime.now(UTC)
        started = time.monotonic()
        allowance = request["time_budget_ms"] / 1000 + 90
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            event_hooks={"response": [observe_response]},
        ) as client:
            outcome = await call_worker(
                "verifier",
                AUTHORIZED_ORIGIN,
                token,
                request,
                policy,
                time.monotonic() + allowance + 5,
                client,
            )
        observation = {
            "label": label,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "caller_elapsed_seconds": round(time.monotonic() - started, 6),
            "caller_timeout_seconds": allowance,
            "http_statuses": statuses,
            "charged_usage": outcome.charged,
            "outcome_error": outcome.error,
            "response": outcome.response,
        }
        evidence["observations"].append(observation)
        return observation

    try:
        cold = await invoke("first_request_after_idle_changed_body", changed)
        cold_status = cold["http_statuses"][-1] if cold["http_statuses"] else None
        # If the original cache survived, replay its original body for a valid
        # warm baseline. Otherwise replay the just-completed changed request.
        warm_request = original if cold_status == 409 else changed
        warm = await invoke("immediate_exact_cached_replay", warm_request)
        warm_status = warm["http_statuses"][-1] if warm["http_statuses"] else None
        # A platform's HTML 200 is not a worker result. Validate the application
        # envelope before using either status as evidence about its registry.
        valid_cold = cold["response"] is not None or (
            cold_status == 409 and cold["outcome_error"] == "terminal worker HTTP 409"
        )
        valid_warm = warm["response"] is not None
        evidence["classification"] = classify_probe(
            cold_status if valid_cold else None,
            cold["caller_elapsed_seconds"],
            warm_status if valid_warm else None,
            warm["caller_elapsed_seconds"],
        )
        evidence["registry_lost"] = valid_cold and cold_status == 200
        evidence["registry_retained"] = valid_cold and cold_status == 409
        evidence["idle_seconds_before_first_request"] = (
            datetime.fromisoformat(cold["started_at"])
            - datetime.fromisoformat(smoke["last_contact_at"])
        ).total_seconds()
        evidence["cold_minus_warm_seconds"] = round(
            cold["caller_elapsed_seconds"] - warm["caller_elapsed_seconds"], 6
        )
        evidence["identical_cached_response"] = (
            valid_cold
            and valid_warm
            and (
                cold["response"] == warm["response"]
                if cold_status == 200
                else warm["response"] == smoke["blocked_response"]
            )
        )
        reported = [item["response"] for item in evidence["observations"] if item["response"]]
        evidence["zero_reported_usage"] = bool(reported) and all(
            item["usage"] == {"steps": 0, "fetches": 0, "tokens": 0} for item in reported
        )
        evidence["zero_charged_usage"] = all(
            item["charged_usage"] == {"steps": 0, "fetches": 0, "tokens": 0}
            for item in evidence["observations"]
        )
    except Exception as exc:
        evidence.update(classification="probe_error", error_type=type(exc).__name__)
    finally:
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        print(
            json.dumps(
                {
                    key: evidence.get(key)
                    for key in (
                        "classification",
                        "registry_lost",
                        "registry_retained",
                        "idle_seconds_before_first_request",
                        "cold_minus_warm_seconds",
                        "identical_cached_response",
                        "zero_reported_usage",
                        "finished_at",
                    )
                },
                indent=2,
            ),
            flush=True,
        )
    return evidence["classification"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-root", default="/Users/spyda/FNMS/lab2")
    parser.add_argument("--smoke-evidence", default=str(ROOT / "evidence" / "render-smoke.json"))
    parser.add_argument("--output", default=str(ROOT / "evidence" / "render-cold-start.json"))
    parser.add_argument("--confirm-root-go-and-idle", action="store_true")
    args = parser.parse_args()
    try:
        classification = asyncio.run(run(args))
    except RuntimeError as exc:
        parser.error(str(exc))
    return 0 if classification == "cache_loss_with_slow_wakeup_observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
