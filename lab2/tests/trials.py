"""Run non-spending trials against three actual running HTTP services.

Usage: python -m tests.trials
This never invokes a model/search provider: requests use zero model/fetch budgets
or forbidden URLs. It does not manufacture a successful research report.
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def envelope(payload, *, steps=0, fetches=0, milliseconds=1000):
    return {
        "request_id": str(uuid4()),
        "run_id": str(uuid4()),
        "time_budget_ms": milliseconds,
        "budget": {"max_steps": steps, "max_fetches": fetches, "max_tokens": 0},
        "payload": payload,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--output", type=Path, default=ROOT / ".state/trials/http.json")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    records = []

    def check(label, operation):
        started = time.monotonic()
        try:
            details = operation()
            entry = {"test": label, "passed": True, "details": details}
        except Exception as exc:
            # Do not serialize request headers, tokens, passwords, or raw bodies.
            entry = {"test": label, "passed": False, "error": type(exc).__name__}
        entry["elapsed_ms"] = round((time.monotonic() - started) * 1000, 2)
        records.append(entry)
        print(f"{'PASS' if entry['passed'] else 'FAIL'} {label}")

    with httpx.Client(timeout=20, trust_env=False) as client:

        def health(base):
            response = client.get(base.rstrip("/") + "/healthz")
            assert response.status_code == 200 and response.json() == {"status": "ok"}
            return {"http_status": response.status_code}

        check("orchestrator health", lambda: health(args.base_url))

        def user_auth():
            response = client.post(
                args.base_url + "/api/auth/login",
                json={
                    "username": os.getenv("TRACKER_USERNAME", "NYUgrader"),
                    "password": os.getenv("TRACKER_PASSWORD", "Courant2026!"),
                },
            )
            assert response.status_code == 200
            token = response.json()["token"]
            me = client.get(
                args.base_url + "/api/auth/me", headers={"Authorization": "Bearer " + token}
            )
            assert me.status_code == 200 and "password_hash" not in me.json()
            return {"authenticated": True, "password_hash_exposed": False}

        check("orchestrator user login and safe user response", user_auth)

        for service, path, payload in [
            (
                "researcher",
                "/research",
                {"topic": "open-source robotics foundation models", "k": 5},
            ),
            (
                "verifier",
                "/verify",
                {
                    "claims": [
                        {
                            "claim_id": "blocked",
                            "text": "A test claim",
                            "source_url": "http://127.0.0.1/",
                        }
                    ]
                },
            ),
        ]:
            base = os.getenv(
                service.upper() + "_URL",
                "http://127.0.0.1:" + ("8011" if service == "researcher" else "8012"),
            )
            endpoint = base.rstrip("/") + path
            token = os.getenv(service.upper() + "_TOKEN", "")
            headers = {"Authorization": "Bearer " + token}
            check(service + " public health", lambda base=base: health(base))

            def auth_order(endpoint=endpoint, headers=headers):
                absent = client.post(endpoint, content="{malformed")
                wrong = client.post(
                    endpoint, content="{malformed", headers={"Authorization": "Bearer wrong"}
                )
                valid = client.post(endpoint, content="{malformed", headers=headers)
                invalid_schema = client.post(endpoint, json={}, headers=headers)
                assert (
                    absent.status_code,
                    wrong.status_code,
                    valid.status_code,
                    invalid_schema.status_code,
                ) == (401, 401, 400, 400)
                return {
                    "unauthenticated_malformed": 401,
                    "wrong_token": 401,
                    "authenticated_malformed": 400,
                    "invalid_schema": 400,
                }

            check(service + " authentication precedes JSON parsing", auth_order)

            def replay(endpoint=endpoint, headers=headers, payload=payload, service=service):
                body = envelope(payload)
                first = client.post(endpoint, json=body, headers=headers)
                second = client.post(
                    endpoint,
                    content=json.dumps(dict(reversed(list(body.items()))), indent=2),
                    headers=headers,
                )
                assert first.status_code == second.status_code == 200
                assert first.json() == second.json()
                assert first.json()["usage"] == {"steps": 0, "fetches": 0, "tokens": 0}
                changed = client.post(
                    endpoint, json=body | {"time_budget_ms": 999}, headers=headers
                )
                assert changed.status_code == 409
                return {
                    "request_id": body["request_id"],
                    "run_id": body["run_id"],
                    "status": first.json()["status"],
                    "identical_replay": True,
                    "changed_body_status": 409,
                    "usage": first.json()["usage"],
                }

            check(service + " zero-spend replay and conflicting ID", replay)

            def tiny(endpoint=endpoint, headers=headers, payload=payload):
                body = envelope(payload, milliseconds=1)
                response = client.post(endpoint, json=body, headers=headers)
                assert response.status_code == 200
                result = response.json()
                assert result["status"] in {"partial", "complete"}
                assert result["usage"] == {"steps": 0, "fetches": 0, "tokens": 0}
                return {
                    "request_id": body["request_id"],
                    "status": result["status"],
                    "error": result["error"],
                    "note": "Forbidden-source verifier may finish before deadline.",
                }

            check(service + " tiny request budget", tiny)

        def forbidden():
            base = os.getenv("VERIFIER_URL", "http://127.0.0.1:8012")
            urls = [
                "http://169.254.169.254/",
                "http://localhost/",
                "http://[::1]/",
                "http://[fc00::1]/",
                "http://[fe80::1]/",
            ]
            body = envelope(
                {
                    "claims": [
                        {"claim_id": str(i), "text": "Must never be fetched", "source_url": url}
                        for i, url in enumerate(urls)
                    ]
                },
                fetches=10,
            )
            response = client.post(
                base.rstrip("/") + "/verify",
                json=body,
                headers={"Authorization": "Bearer " + os.getenv("VERIFIER_TOKEN", "")},
            )
            assert response.status_code == 200
            result = response.json()
            assert result["status"] == "complete"
            assert len(result["result"]["verdicts"]) == len(urls)
            assert all(item["verdict"] == "unreachable" for item in result["result"]["verdicts"])
            assert result["usage"] == {"steps": 0, "fetches": 0, "tokens": 0}
            return {
                "request_id": body["request_id"],
                "blocked_urls": urls,
                "usage": result["usage"],
            }

        check("verifier metadata, localhost and IPv6 blocked before HTTP", forbidden)

    evidence = {
        "kind": "actual_http_non_spending_trials",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "passed": sum(item["passed"] for item in records),
        "total": len(records),
        "trials": records,
        "limitations": "No successful live research or Render cold-start claim is made by these trials.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {evidence['passed']}/{evidence['total']} passing trials to {args.output}")
    raise SystemExit(0 if evidence["passed"] == evidence["total"] else 1)


if __name__ == "__main__":
    main()
