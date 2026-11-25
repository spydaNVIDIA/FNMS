"""Non-spending HTTP contract checks for the explicitly deployed Render verifier.

The only valid work contains forbidden source addresses and a zero model/token
allowance. No redirect is followed, and the token is sent only to the exact
authorized HTTPS origin. Stop traffic after this script to measure idle wakeup.
"""

import argparse
import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import dotenv_values


AUTHORIZED_ORIGIN = "https://fnms-lab2-verifier.onrender.com"
ROOT = Path(__file__).resolve().parents[1]


def response_digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


async def run(args):
    token = dotenv_values(Path(args.env))["VERIFIER_TOKEN"]
    if not token:
        raise RuntimeError("VERIFIER_TOKEN is missing")
    started = datetime.now(UTC)
    evidence = {
        "kind": "actual_render_non_spending_smoke",
        "origin": AUTHORIZED_ORIGIN,
        "started_at": started.isoformat(),
        "model_or_search_calls_requested": 0,
        "requests": [],
        "checks": {},
    }
    last_contact = None

    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:

        async def send(name, method, path, *, authenticated=False, **kwargs):
            nonlocal last_contact
            sent = time.monotonic()
            observation = {
                "name": name,
                "method": method,
                "path": path,
                "started_at": datetime.now(UTC).isoformat(),
            }
            try:
                response = await client.request(
                    method,
                    AUTHORIZED_ORIGIN + path,
                    headers={"Authorization": "Bearer " + token} if authenticated else None,
                    **kwargs,
                )
                observation["http_status"] = response.status_code
                return response
            except httpx.TransportError as exc:
                observation["error_type"] = type(exc).__name__
                return None
            finally:
                last_contact = datetime.now(UTC)
                observation["completed_at"] = last_contact.isoformat()
                observation["latency_ms"] = round((time.monotonic() - sent) * 1000, 3)
                evidence["requests"].append(observation)

        try:
            deadline = time.monotonic() + args.readiness_timeout
            attempt = 0
            while time.monotonic() < deadline:
                attempt += 1
                response = await send(
                    f"health-readiness-{attempt}",
                    "GET",
                    "/healthz",
                    timeout=min(90, max(1, deadline - time.monotonic())),
                )
                try:
                    ready = (
                        response is not None
                        and response.status_code == 200
                        and response.json() == {"status": "ok"}
                    )
                except ValueError:
                    ready = False
                if ready:
                    break
                await asyncio.sleep(min(10, max(0, deadline - time.monotonic())))
            else:
                raise RuntimeError("Render did not become healthy within the readiness window")
            evidence["checks"]["public_health"] = True

            response = await send(
                "unauthenticated-malformed", "POST", "/verify", content="{broken", timeout=30
            )
            evidence["checks"]["auth_before_body_validation"] = (
                response is not None and response.status_code == 401
            )
            response = await send(
                "authenticated-malformed",
                "POST",
                "/verify",
                authenticated=True,
                content="{broken",
                timeout=30,
            )
            evidence["checks"]["authenticated_bad_json_400"] = (
                response is not None and response.status_code == 400
            )

            urls = [
                "http://169.254.169.254/",
                "http://localhost/",
                "http://[::1]/",
                "http://[fc00::1]/",
                "http://[fe80::1]/",
                "http://0.0.0.0/",
            ]
            body = {
                "run_id": str(uuid4()),
                "request_id": str(uuid4()),
                "time_budget_ms": 5000,
                "budget": {"max_steps": 0, "max_fetches": 10, "max_tokens": 0},
                "payload": {
                    "claims": [
                        {
                            "claim_id": f"blocked-{index}",
                            "text": "This is an explicit blocked-address smoke fixture.",
                            "source_url": url,
                        }
                        for index, url in enumerate(urls, 1)
                    ]
                },
            }
            response = await send(
                "blocked-addresses", "POST", "/verify", authenticated=True, json=body, timeout=30
            )
            if response is None:
                raise RuntimeError("No response to blocked-address check")
            first = response.json()
            evidence["worker_request_id"] = body["request_id"]
            evidence["fixture_run_id"] = body["run_id"]
            evidence["blocked_urls"] = urls
            evidence["blocked_response"] = first
            verdicts = first.get("result", {}).get("verdicts", [])
            evidence["checks"]["blocked_addresses_unreachable_without_fetch"] = (
                response.status_code == 200
                and first.get("status") == "complete"
                and first.get("usage") == {"steps": 0, "fetches": 0, "tokens": 0}
                and len(verdicts) == len(urls)
                and all(
                    item["verdict"] == "unreachable" and item["evidence"] is None
                    for item in verdicts
                )
            )

            reordered = dict(reversed(list(body.items())))
            response = await send(
                "completed-replay-reordered-json",
                "POST",
                "/verify",
                authenticated=True,
                json=reordered,
                timeout=30,
            )
            cached = response.json() if response is not None else None
            evidence["checks"]["completed_replay_identical"] = (
                response is not None and response.status_code == 200 and cached == first
            )
            evidence["original_response_sha256"] = response_digest(first)
            evidence["cached_response_sha256"] = response_digest(cached)

            changed = {**body, "time_budget_ms": 5001}
            response = await send(
                "changed-valid-body-conflict",
                "POST",
                "/verify",
                authenticated=True,
                json=changed,
                timeout=30,
            )
            evidence["checks"]["changed_body_409"] = (
                response is not None and response.status_code == 409
            )
            evidence["passed"] = all(evidence["checks"].values())
        except Exception as exc:
            evidence.update(passed=False, error_type=type(exc).__name__)
        finally:
            # No subsequent network operation is performed by this script.
            evidence["last_contact_at"] = last_contact.isoformat() if last_contact else None
            evidence["cold_start_eligible_at"] = (
                (last_contact + timedelta(minutes=16)).isoformat() if last_contact else None
            )
            evidence["idle_measurement_condition"] = (
                "No other browser, health poll, dashboard visit, or client request may contact this service during the idle window."
            )
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
            print(
                json.dumps(
                    {
                        key: evidence[key]
                        for key in ("passed", "checks", "last_contact_at", "cold_start_eligible_at")
                    },
                    indent=2,
                ),
                flush=True,
            )
    return evidence["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="/Users/spyda/FNMS/lab2/.env")
    parser.add_argument("--output", default=str(ROOT / "evidence" / "render-smoke.json"))
    parser.add_argument("--readiness-timeout", type=float, default=300)
    return 0 if asyncio.run(run(parser.parse_args())) else 1


if __name__ == "__main__":
    raise SystemExit(main())
