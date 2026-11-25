"""Append-only JSONL events; never log request bodies, credentials or provider errors."""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()


def emit(
    service,
    *,
    run_id=None,
    request_id=None,
    step=0,
    direction="out",
    endpoint_or_tool,
    status,
    latency_ms=0,
    attempt=1,
    tokens=0,
):
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "run_id": str(run_id) if run_id else None,
        "request_id": str(request_id) if request_id else None,
        "step": step,
        "direction": direction,
        "endpoint_or_tool": endpoint_or_tool,
        "status": status,
        "latency_ms": round(latency_ms, 3),
        "attempt": attempt,
        "tokens": tokens,
    }
    serialized = json.dumps(event, ensure_ascii=False)
    directory = Path(os.getenv("LOG_DIR", "logs"))
    directory.mkdir(parents=True, exist_ok=True)
    with _lock:
        with (directory / f"{service}.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(serialized + "\n")
    # Render captures stdout; local JSONL remains useful for exporting trials.
    print(serialized, flush=True)
