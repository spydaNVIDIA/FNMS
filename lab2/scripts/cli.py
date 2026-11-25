"""Run the application through its authenticated HTTP API and export evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import shutil
import sys
import time

import httpx

from manage import ROOT, STATE, configuration


def run(args):
    config = configuration()
    with httpx.Client(base_url=args.base_url, timeout=20, trust_env=False) as client:
        login = client.post(
            "/api/auth/login",
            json={
                "username": config.get("TRACKER_USERNAME", "NYUgrader"),
                "password": config.get("TRACKER_PASSWORD", "Courant2026!"),
            },
        )
        login.raise_for_status()
        client.headers["Authorization"] = f"Bearer {login.json()['token']}"
        body = {}
        if args.topic:
            body["topic"] = args.topic
        if args.k is not None:
            body["k"] = args.k
        response = client.post("/api/tracker/run-now", json=body)
        response.raise_for_status()
        result = response.json()
        run_id = result["id"]
        print(f"Started run {run_id}", flush=True)
        deadline = time.monotonic() + args.wait_seconds
        while result["status"] == "running" and time.monotonic() < deadline:
            time.sleep(2)
            response = client.get(f"/api/tracker/runs/{run_id}")
            response.raise_for_status()
            result = response.json()
        if result["status"] == "running":
            print(f"Run is still active; inspect http://localhost:8010. Run ID: {run_id}")
            return
        snapshot = result["snapshot"]
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": result["status"],
                    "duration_seconds": result["duration_seconds"],
                    "failed_workers": snapshot.get("failed_workers", []),
                    "charged_usage": snapshot.get("charged_usage", {}),
                    "report_path": str(STATE / "reports" / f"{run_id}.md"),
                },
                indent=2,
            )
        )


def choose_report(run_id: str | None) -> Path:
    reports = STATE / "reports"
    if run_id:
        # UUIDs cannot contain paths; prevent an accidental file outside reports.
        from uuid import UUID

        return reports / f"{UUID(run_id)}.json"
    candidates = sorted(reports.glob("*.json"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise RuntimeError("No saved runs to export")
    return candidates[-1]


def export(args):
    source = choose_report(args.run_id)
    snapshot = json.loads(source.read_text())
    run_id = snapshot["run_id"]
    destination = Path(args.output).resolve() if args.output else ROOT / "evidence" / run_id
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("Export destination is not empty; choose another --output directory")
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination / "report.json")
    markdown = source.with_suffix(".md")
    if markdown.exists():
        shutil.copy2(markdown, destination / "report.md")
    sources = list((STATE / "traces").glob("*.jsonl"))
    if args.verifier_log:
        sources.append(Path(args.verifier_log).resolve())
    per_service: dict[str, list[dict]] = {}
    for path in sources:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("run_id") == run_id:
                per_service.setdefault(event["service"], []).append(event)
    for service, events in per_service.items():
        if service not in {"orchestrator", "researcher", "verifier"}:
            raise RuntimeError("Unexpected service name in trace")
        (destination / f"{service}.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
    missing = sorted({"orchestrator", "researcher", "verifier"} - per_service.keys())
    (destination / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": snapshot["status"],
                "services_exported": sorted(per_service),
                "missing_service_logs": missing,
                "note": "Exported application records. This manifest does not certify live or Render execution.",
            },
            indent=2,
        )
    )
    print(f"Exported run to {destination}")
    if missing:
        print(
            f"Missing trace logs: {', '.join(missing)}. Use --verifier-log for downloaded Render JSONL."
        )


def evaluate(args):
    paths = (
        [Path(path) for path in args.reports]
        if args.reports
        else sorted((STATE / "reports").glob("*.json"))
    )
    claims = []
    for path in paths:
        snapshot = json.loads(path.read_text())
        for development in snapshot.get("developments", []):
            if development.get("stale"):
                continue
            for claim in development.get("claims", []):
                claims.append({"run_id": snapshot["run_id"], **claim})
    counts = {
        verdict: sum(claim.get("verdict") == verdict for claim in claims)
        for verdict in ["supported", "unsupported", "unreachable", "unverified"]
    }
    supported = [claim for claim in claims if claim.get("verdict") == "supported"]
    unsupported = [claim for claim in claims if claim.get("verdict") == "unsupported"]
    sample = random.Random(args.seed).sample(supported, min(10, len(supported)))
    audit = {
        "sampling_seed": args.seed,
        "source_reports": [str(path.resolve()) for path in paths],
        "counts": counts,
        "enough_supported_claims": len(supported) >= 10,
        "instructions": "Hand-check every listed claim against its URL. Fill verdict_correct and rationale; do not infer correctness from the model's verdict.",
        "claims_to_hand_check": [
            {**claim, "verdict_correct": None, "rationale": ""} for claim in unsupported + sample
        ],
    }
    destination = (
        Path(args.output).resolve() if args.output else ROOT / "evidence" / "claim-audit.json"
    )
    if destination.exists():
        raise RuntimeError(
            "Audit file already exists; use --output to avoid overwriting hand checks"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(audit, indent=2))
    print(json.dumps(counts, indent=2))
    print(f"Manual audit checklist: {destination}")
    if len(supported) < 10:
        print(
            f"Only {len(supported)} supported claims; more real runs are needed for the required 10-claim sample."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    run_parser.add_argument("--topic")
    run_parser.add_argument("--k", type=int, choices=range(3, 11))
    run_parser.add_argument("--wait-seconds", type=int, default=1000)
    export_parser = sub.add_parser("export")
    export_parser.add_argument("--run-id")
    export_parser.add_argument("--output")
    export_parser.add_argument("--verifier-log")
    evaluate_parser = sub.add_parser("evaluate")
    evaluate_parser.add_argument("--reports", nargs="*")
    evaluate_parser.add_argument("--output")
    evaluate_parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    try:
        {"run": run, "export": export, "evaluate": evaluate}[args.action](args)
    except httpx.HTTPStatusError as exc:
        print(
            f"Application returned HTTP {exc.response.status_code}; inspect the dashboard or service logs.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
        # httpx error strings contain URLs, not authorization headers or request bodies.
        print(f"Lab 2: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
