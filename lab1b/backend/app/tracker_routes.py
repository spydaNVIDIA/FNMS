"""Authenticated persistence and display endpoints for the tracker."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import (
    TrackerArticle,
    TrackerDevelopment,
    TrackerRun,
    TrackerRunDevelopment,
    TrackerTraceEvent,
    User,
)
from .run_service import run_coordinator
from .schemas import (
    TrackerArticleOut,
    TrackerDevelopmentOut,
    TrackerReportOut,
    TrackerRunCreate,
    TrackerRunCreated,
    TrackerRunLaunchOut,
    TrackerRunSummaryOut,
    TrackerStateDevelopment,
    TrackerStateOut,
)
from .security import create_token, get_current_user

router = APIRouter(prefix="/api/tracker", tags=["tracker"])


@router.post(
    "/run-now",
    response_model=TrackerRunLaunchOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_tracker_now(current_user: User = Depends(get_current_user)) -> dict:
    """Start a local live run scoped to the authenticated user's tracker state."""
    try:
        return run_coordinator.start(current_user.id, create_token(current_user.id))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.get("/run-now/status", response_model=TrackerRunLaunchOut)
def tracker_run_status(current_user: User = Depends(get_current_user)) -> dict:
    return run_coordinator.status(current_user.id)


def _run_summary(run: TrackerRun) -> TrackerRunSummaryOut:
    duration_seconds = None
    if run.completed_at is not None:
        duration_seconds = max(0.0, (run.completed_at - run.started_at).total_seconds())
    return TrackerRunSummaryOut(
        id=run.id,
        started_at=run.started_at,
        completed_at=run.completed_at,
        duration_seconds=duration_seconds,
        status=run.status,
        topic=run.topic,
        top_k=run.top_k,
    )


@router.get("/state", response_model=TrackerStateOut)
def tracker_state(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> TrackerStateOut:
    seen_urls = [
        value
        for (value,) in db.query(TrackerArticle.canonical_url)
        .filter(
            TrackerArticle.user_id == current_user.id,
            TrackerArticle.status == "fetched",
        )
        .distinct()
        .all()
    ]
    developments = (
        db.query(TrackerDevelopment)
        .filter(TrackerDevelopment.user_id == current_user.id)
        .order_by(TrackerDevelopment.last_seen_at.desc())
        .all()
    )
    latest_run = (
        db.query(TrackerRun)
        .filter(TrackerRun.user_id == current_user.id)
        .order_by(TrackerRun.started_at.desc())
        .first()
    )
    previous_top_k: list[str] = []
    if latest_run is not None:
        previous_top_k = [
            item.development_key
            for item in db.query(TrackerDevelopment)
            .join(
                TrackerRunDevelopment,
                TrackerRunDevelopment.development_id == TrackerDevelopment.id,
            )
            .filter(
                TrackerRunDevelopment.run_id == latest_run.id,
                TrackerRunDevelopment.rank.is_not(None),
            )
            .order_by(TrackerRunDevelopment.rank)
            .all()
        ]
    return TrackerStateOut(
        seen_urls=sorted(seen_urls),
        developments=[
            TrackerStateDevelopment(
                key=item.development_key,
                title=item.title,
                summary=item.summary,
                source_urls=item.source_urls,
                last_seen_at=item.last_seen_at,
            )
            for item in developments
        ],
        previous_top_k=previous_top_k,
    )


@router.post("/runs", response_model=TrackerRunCreated, status_code=status.HTTP_201_CREATED)
def create_tracker_run(
    body: TrackerRunCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TrackerRunCreated:
    article_urls = [article.canonical_url for article in body.articles]
    if len(article_urls) != len(set(article_urls)):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A run may contain only one article record per canonical URL",
        )
    valid_sources = {
        article.canonical_url for article in body.articles if article.status == "fetched"
    }
    for (sources,) in (
        db.query(TrackerDevelopment.source_urls)
        .filter(TrackerDevelopment.user_id == current_user.id)
        .all()
    ):
        valid_sources.update(sources)
    for item in body.developments:
        if not set(item.source_urls).issubset(valid_sources):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Development {item.key!r} cites an unfetched source",
            )

    run = TrackerRun(
        user_id=current_user.id,
        started_at=body.started_at,
        completed_at=body.completed_at,
        status=body.status,
        topic=body.topic,
        top_k=body.top_k,
        report_markdown=body.report_markdown,
        budget=body.budget,
    )
    db.add(run)
    db.flush()

    for article in body.articles:
        db.add(
            TrackerArticle(
                run_id=run.id,
                user_id=current_user.id,
                **article.model_dump(),
            )
        )

    seen_at = body.completed_at or datetime.now(UTC)
    for item in body.developments:
        development = (
            db.query(TrackerDevelopment)
            .filter(
                TrackerDevelopment.user_id == current_user.id,
                TrackerDevelopment.development_key == item.key,
            )
            .first()
        )
        if development is None:
            development = TrackerDevelopment(
                user_id=current_user.id,
                development_key=item.key,
                title=item.title,
                summary=item.summary,
                source_urls=item.source_urls,
                first_seen_at=seen_at,
                last_seen_at=seen_at,
            )
            db.add(development)
            db.flush()
        else:
            development.title = item.title
            development.summary = item.summary
            development.source_urls = sorted(set(development.source_urls + item.source_urls))
            development.last_seen_at = seen_at
        db.add(
            TrackerRunDevelopment(
                run_id=run.id,
                development_id=development.id,
                rank=item.rank,
                bucket=item.bucket,
                score=item.score,
                source_urls=item.source_urls,
            )
        )

    for event in body.trace:
        db.add(TrackerTraceEvent(run_id=run.id, **event.model_dump()))

    db.commit()
    return TrackerRunCreated(id=run.id)


@router.get("/reports/latest", response_model=TrackerReportOut)
def latest_report(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> TrackerReportOut:
    run = (
        db.query(TrackerRun)
        .filter(TrackerRun.user_id == current_user.id)
        .order_by(TrackerRun.started_at.desc())
        .first()
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No tracker runs yet")
    return _report_for_run(run, db)


def _report_for_run(run: TrackerRun, db: Session) -> TrackerReportOut:
    rows = (
        db.query(TrackerRunDevelopment, TrackerDevelopment)
        .join(
            TrackerDevelopment,
            TrackerDevelopment.id == TrackerRunDevelopment.development_id,
        )
        .filter(TrackerRunDevelopment.run_id == run.id)
        .order_by(
            TrackerRunDevelopment.rank.is_(None),
            TrackerRunDevelopment.rank,
            TrackerDevelopment.title,
        )
        .all()
    )
    return TrackerReportOut(
        run=_run_summary(run),
        report_markdown=run.report_markdown,
        developments=[
            TrackerDevelopmentOut(
                key=development.development_key,
                title=development.title,
                summary=development.summary,
                rank=association.rank,
                bucket=association.bucket,
                score=association.score,
                source_urls=association.source_urls,
            )
            for association, development in rows
        ],
    )


@router.get("/runs/{run_id}/report", response_model=TrackerReportOut)
def run_report(
    run_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TrackerReportOut:
    """Return one historical report without exposing another user's run."""
    run = (
        db.query(TrackerRun)
        .filter(TrackerRun.id == run_id, TrackerRun.user_id == current_user.id)
        .first()
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return _report_for_run(run, db)


@router.get("/runs", response_model=list[TrackerRunSummaryOut])
def list_runs(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[TrackerRunSummaryOut]:
    runs = (
        db.query(TrackerRun)
        .filter(TrackerRun.user_id == current_user.id)
        .order_by(TrackerRun.started_at.desc())
        .all()
    )
    return [_run_summary(run) for run in runs]


@router.get("/runs/{run_id}/articles", response_model=list[TrackerArticleOut])
def run_articles(
    run_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TrackerArticleOut]:
    run = (
        db.query(TrackerRun)
        .filter(TrackerRun.id == run_id, TrackerRun.user_id == current_user.id)
        .first()
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return [
        TrackerArticleOut.model_validate(article, from_attributes=True)
        for article in db.query(TrackerArticle)
        .filter(TrackerArticle.run_id == run.id)
        .order_by(TrackerArticle.fetched_at)
        .all()
    ]
