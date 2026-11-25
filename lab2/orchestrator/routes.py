"""Authenticated run control and immutable historical reports."""

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .config import ROOT
from .db import SessionLocal, get_db
from .engine import load_policy, markdown, orchestrate
from .models import Run, User
from .security import get_current_user

router = APIRouter(prefix="/api/tracker")
active_tasks: dict[int, asyncio.Task] = {}


class RunInput(BaseModel):
    topic: str | None = Field(default=None, min_length=3, max_length=255)
    k: int | None = Field(default=None, ge=3, le=10)


def run_view(run):
    duration = None
    if run.completed_at:
        duration = max(0, (run.completed_at - run.started_at).total_seconds())
    return dict(
        id=run.id,
        started_at=run.started_at.isoformat(),
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
        duration_seconds=duration,
        status=run.status,
        snapshot=run.snapshot,
        report_markdown=run.report_markdown,
    )


def write_report_artifacts(snapshot, report):
    directory = ROOT / ".state" / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    run_id = snapshot["run_id"]
    (directory / f"{run_id}.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    (directory / f"{run_id}.md").write_text(report, encoding="utf-8")


def settle_interrupted_runs():
    """A new orchestrator process cannot resume tasks from its predecessor."""
    completed_at = datetime.now(UTC)
    message = "The orchestrator restarted before this run finished. Start a new run to retry."
    with SessionLocal() as db:
        interrupted = db.query(Run).filter_by(status="running").all()
        for run in interrupted:
            # Retain any saved findings and accounting instead of erasing partial work.
            snapshot = dict(run.snapshot)
            snapshot.update(
                run_id=run.id,
                status="partial",
                message=message,
                completed_at=completed_at.isoformat(),
            )
            for key, default in (
                ("developments", []),
                ("dropped", []),
                ("ledger", {}),
                ("charged_usage", {}),
            ):
                snapshot.setdefault(key, default)
            snapshot["failed_workers"] = [
                *snapshot.get("failed_workers", []),
                {"worker": "orchestrator", "reason": "orchestrator_restart"},
            ]
            try:
                report = markdown(snapshot)
            except (KeyError, TypeError, ValueError):
                # Legacy/incomplete snapshots must not prevent the application starting.
                report = f"# Interrupted tracker run\n\nRun: {run.id}\nStatus: partial\n{message}\n"
            run.status = "partial"
            run.snapshot = snapshot
            run.report_markdown = report
            run.completed_at = completed_at
        db.commit()
        for run in interrupted:
            write_report_artifacts(run.snapshot, run.report_markdown)
    return len(interrupted)


async def execute_run(run_id, user_id, topic, k, policy):
    with SessionLocal() as db:
        older = (
            db.query(Run)
            .filter(
                Run.user_id == user_id, Run.id != run_id, Run.status.in_(["complete", "partial"])
            )
            .order_by(Run.started_at.desc())
            .all()
        )
        # Never treat stale fallback as fresh knowledge. Partial fresh research is still useful.
        previous = next(
            (
                r.snapshot["developments"]
                for r in older
                if r.snapshot.get("topic") == topic
                and r.snapshot.get("developments")
                and not r.snapshot["developments"][0].get("stale")
            ),
            [],
        )
    try:
        snapshot = await orchestrate(run_id, topic, k, previous, policy)
    except Exception:
        # Protect the UI and redact implementation/provider exception details.
        snapshot = dict(
            run_id=run_id,
            topic=topic,
            k=k,
            status="partial",
            message="Orchestration failed; inspect the trace.",
            failed_workers=[{"worker": "orchestrator", "reason": "internal_error"}],
            developments=[],
            dropped=[],
            ledger={},
            charged_usage={},
        )
    report = markdown(snapshot)
    with SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:  # User may have deleted the account while research was running.
            return
        run.status, run.snapshot, run.report_markdown = snapshot["status"], snapshot, report
        run.completed_at = datetime.now(UTC)
        db.commit()
    write_report_artifacts(snapshot, report)


@router.post("/run-now", status_code=202)
async def start_run(
    body: RunInput,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if any(not task.done() for task in active_tasks.values()):
        raise HTTPException(409, "A tracker run is already active")
    policy = load_policy()
    run_id = str(uuid4())
    request.state.run_id = run_id
    topic, k = body.topic or policy["topic"], body.k or policy["top_k"]
    run = Run(
        id=run_id,
        user_id=user.id,
        started_at=datetime.now(UTC),
        status="running",
        snapshot={"topic": topic, "k": k},
    )
    db.add(run)
    db.commit()
    active_tasks[user.id] = asyncio.create_task(execute_run(run_id, user.id, topic, k, policy))
    return run_view(run)


@router.get("/runs")
def runs(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return [
        run_view(run)
        for run in db.query(Run).filter_by(user_id=user.id).order_by(Run.started_at.desc()).all()
    ]


@router.get("/runs/{run_id}")
def report(
    run_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    request.state.run_id = run_id
    run = db.query(Run).filter_by(id=run_id, user_id=user.id).first()
    if run is None:
        raise HTTPException(404, "Not found")
    return run_view(run)
