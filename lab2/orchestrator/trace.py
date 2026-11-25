"""Append-only, secret-free trace events. Latencies use caller monotonic clocks."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def trace(
    *,
    run_id=None,
    request_id=None,
    direction="out",
    endpoint_or_tool,
    status,
    latency_ms=0,
    attempt=1,
    tokens=0,
    step=0,
):
    directory = Path(os.getenv("TRACE_DIR", ".state/traces"))
    directory.mkdir(parents=True, exist_ok=True)
    event = dict(
        ts=datetime.now(UTC).isoformat(),
        service="orchestrator",
        run_id=str(run_id) if run_id else None,
        request_id=str(request_id) if request_id else None,
        step=step,
        direction=direction,
        endpoint_or_tool=endpoint_or_tool,
        status=status,
        latency_ms=round(latency_ms, 3),
        attempt=attempt,
        tokens=tokens,
    )
    with (directory / "orchestrator.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")
