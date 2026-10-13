"""Write human-readable reports and JSONL traces without fabricating day-apart runs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .types import TraceEvent

_STARTED = re.compile(r"^- Run started: (.+)$", re.MULTILINE)


def _stem_for_run(root: Path, started_at: datetime, status: str) -> str:
    if status != "complete":
        return started_at.strftime(f"run-%Y%m%d-%H%M%S-{status}")
    reports = root / "reports"
    first = reports / "run1.md"
    second = reports / "run2.md"
    if not first.exists():
        return "run1"
    if not second.exists():
        match = _STARTED.search(first.read_text(encoding="utf-8"))
        if match:
            first_started = datetime.fromisoformat(match.group(1))
            if started_at - first_started >= timedelta(days=1):
                return "run2"
    return started_at.strftime("run-%Y%m%d-%H%M%S")


def write_artifacts(
    root: Path,
    started_at: datetime,
    status: str,
    report: str,
    trace: list[TraceEvent],
) -> tuple[Path, Path]:
    (root / "reports").mkdir(exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    stem = _stem_for_run(root, started_at, status)
    report_path = root / "reports" / f"{stem}.md"
    trace_path = root / "logs" / f"{stem}.jsonl"
    report_path.write_text(report, encoding="utf-8")
    trace_path.write_text(
        "".join(json.dumps(event.as_dict(), ensure_ascii=False) + "\n" for event in trace),
        encoding="utf-8",
    )
    return report_path, trace_path
