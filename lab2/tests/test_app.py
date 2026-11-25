"""A1 authentication and orchestration integration with an isolated test database."""

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def isolated_app_storage(tmp_path, monkeypatch):
    """Override every DB entry point even when another test imported the app first."""
    monkeypatch.setenv("JWT_SECRET", "offline-test-secret")
    module = importlib.import_module("orchestrator.app")
    routes = importlib.import_module("orchestrator.routes")
    engine = create_engine(
        f"sqlite:///{tmp_path}/auth.db", connect_args={"check_same_thread": False}
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(module, "seed", lambda: None)
    monkeypatch.setattr(routes, "SessionLocal", sessions)
    monkeypatch.setattr(routes, "ROOT", tmp_path)

    def get_test_db():
        with sessions() as db:
            yield db

    previous_overrides = module.app.dependency_overrides.copy()
    module.app.dependency_overrides[module.get_db] = get_test_db
    try:
        yield module, routes
    finally:
        module.app.dependency_overrides.clear()
        module.app.dependency_overrides.update(previous_overrides)
        engine.dispose()


def test_auth_ownership_and_immutable_history(isolated_app_storage, tmp_path, monkeypatch):
    module, routes = isolated_app_storage

    async def fake_run(run_id, topic, k, previous, policy):
        return dict(
            run_id=run_id,
            status="complete",
            topic=topic,
            k=k,
            message="offline test",
            developments=[],
            dropped=[],
            failed_workers=[],
            ledger={},
            charged_usage={},
        )

    monkeypatch.setattr(routes, "orchestrate", fake_run)
    with TestClient(module.app) as client:
        assert client.get("/api/tracker/runs").status_code == 401
        assert client.get("/").status_code == 200
        assert client.get("/healthz").json() == {"status": "ok"}
        first = client.post(
            "/api/auth/register", json={"username": "owner", "password": "password123"}
        )
        assert first.status_code == 201
        assert "password_hash" not in first.json()
        second = client.post(
            "/api/auth/register", json={"username": "other", "password": "password123"}
        )
        assert second.status_code == 201
        headers = {}
        for user in ("owner", "other"):
            token = client.post(
                "/api/auth/login", json={"username": user, "password": "password123"}
            ).json()["token"]
            headers[user] = {"Authorization": f"Bearer {token}"}
        launch = client.post("/api/tracker/run-now", json={"k": 3}, headers=headers["owner"])
        assert launch.status_code == 202
        run_id = launch.json()["id"]
        # Let the managed task run before reading it again.
        for _ in range(20):
            report = client.get(f"/api/tracker/runs/{run_id}", headers=headers["owner"])
            if report.json()["status"] != "running":
                break
        assert report.json()["status"] == "complete"
        assert report.json()["snapshot"]["message"] == "offline test"
        assert (
            client.get(f"/api/tracker/runs/{run_id}", headers=headers["other"]).status_code == 404
        )
        assert client.get("/api/tracker/runs", headers=headers["other"]).json() == []
        assert (
            client.get(f"/api/users/{first.json()['id']}", headers=headers["other"]).status_code
            == 404
        )
        persisted = json.loads((tmp_path / ".state/reports" / f"{run_id}.json").read_text())
        assert persisted["run_id"] == run_id


def test_unexpected_endpoint_failure_still_emits_safe_inbound_trace(monkeypatch):
    from fastapi import FastAPI, Request

    monkeypatch.setenv("JWT_SECRET", "offline-test-secret")
    module = importlib.import_module("orchestrator.app")
    events = []
    monkeypatch.setattr(module, "trace", lambda **event: events.append(event))
    app = FastAPI()
    app.middleware("http")(module.request_trace)
    run_id = str(uuid4())

    @app.get("/failure-fixture")
    async def failure(request: Request):
        request.state.run_id = run_id
        raise RuntimeError("fixture-sensitive-detail-must-not-be-logged")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/failure-fixture")
    assert response.status_code == 500
    assert len(events) == 1
    event = events[0]
    assert event["direction"] == "in"
    assert event["status"] == 500
    assert event["run_id"] == run_id
    assert event["endpoint_or_tool"] == "/failure-fixture"
    assert event["latency_ms"] >= 0
    assert "fixture-sensitive-detail" not in json.dumps(event)


@pytest.fixture
def isolated_run_storage(tmp_path, monkeypatch):
    """Keep lifecycle tests independent of the application's imported database engine."""
    monkeypatch.setenv("JWT_SECRET", "offline-test-secret")
    app = importlib.import_module("orchestrator.app")
    routes = importlib.import_module("orchestrator.routes")
    models = importlib.import_module("orchestrator.models")
    engine = create_engine(
        f"sqlite:///{tmp_path}/lifecycle.db", connect_args={"check_same_thread": False}
    )
    models.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(routes, "SessionLocal", sessions)
    monkeypatch.setattr(routes, "ROOT", tmp_path)
    monkeypatch.setattr(app, "engine", engine)
    monkeypatch.setattr(app, "seed", lambda: None)
    with sessions() as db:
        db.add_all(
            [
                models.User(id=1, username="owner", password_hash="unused"),
                models.User(id=2, username="other", password_hash="unused"),
            ]
        )
        db.commit()
    yield app, routes, models.Run, sessions
    engine.dispose()


def saved_development(identifier, *, stale=False):
    return {
        "id": identifier,
        "rank": 1,
        "title": identifier,
        "summary": "A saved development.",
        "bucket": "new",
        "stale": stale,
        "claims": [
            {
                "claim_id": f"{identifier}-claim",
                "text": "The model provides public weights.",
                "source_url": "https://example.org/model",
                "verdict": "supported",
                "evidence": "public weights",
            }
        ],
    }


@pytest.mark.parametrize("same_topic_exists", [False, True])
def test_run_baseline_uses_only_same_topic_fresh_history(
    isolated_run_storage, monkeypatch, same_topic_exists
):
    _, routes, Run, sessions = isolated_run_storage
    now = datetime.now(UTC)
    run_id = str(uuid4())
    matching = [saved_development("matching")]
    records = [
        (1, "another topic", 5, [saved_development("unrelated")]),
        (1, "robotics", 10, [saved_development("stale", stale=True)]),
        (2, "robotics", 2, [saved_development("another-user")]),
    ]
    if same_topic_exists:
        records.append((1, "robotics", 30, matching))
    with sessions() as db:
        for user_id, topic, minutes, developments in records:
            db.add(
                Run(
                    id=str(uuid4()),
                    user_id=user_id,
                    started_at=now - timedelta(minutes=minutes),
                    status="complete",
                    snapshot={"topic": topic, "developments": developments},
                )
            )
        db.add(
            Run(
                id=run_id,
                user_id=1,
                started_at=now,
                status="running",
                snapshot={"topic": "robotics", "k": 3},
            )
        )
        db.commit()
    observed = []

    async def fake_orchestrate(identifier, topic, k, previous, policy):
        observed.append(previous)
        return {
            "run_id": identifier,
            "topic": topic,
            "k": k,
            "status": "partial",
            "message": "Offline test",
            "failed_workers": [],
            "developments": [],
            "dropped": [],
        }

    monkeypatch.setattr(routes, "orchestrate", fake_orchestrate)
    asyncio.run(routes.execute_run(run_id, 1, "robotics", 3, {}))
    assert observed == [matching if same_topic_exists else []]


def test_startup_settles_only_running_rows_and_preserves_saved_work(isolated_run_storage):
    app, routes, Run, sessions = isolated_run_storage
    now = datetime.now(UTC)
    initial = {"topic": "robotics", "k": 3}
    saved = {
        **initial,
        "developments": [saved_development("saved")],
        "ledger": {"request": {"charged": {"steps": 2}}},
        "charged_usage": {"steps": 2, "fetches": 1, "tokens": 100},
        "failed_workers": [{"worker": "researcher", "reason": "budget_exhausted"}],
    }
    # A legacy incomplete finding must not prevent startup or erase the finding.
    incomplete = {**initial, "developments": [{"id": "unfinished"}]}
    row_ids = [str(uuid4()) for _ in range(5)]
    with sessions() as db:
        for index, snapshot in enumerate([initial, saved, incomplete, initial, initial]):
            db.add(
                Run(
                    id=row_ids[index],
                    user_id=1,
                    started_at=now - timedelta(minutes=1),
                    status="running" if index < 3 else ("complete" if index == 3 else "partial"),
                    snapshot=snapshot,
                    report_markdown="Existing historical report",
                )
            )
        db.commit()

    with TestClient(app.app) as client:
        assert client.get("/healthz").status_code == 200
    with sessions() as db:
        for index, identifier in enumerate(row_ids):
            run = db.get(Run, identifier)
            if index >= 3:
                assert run.status == ("complete" if index == 3 else "partial")
                assert run.snapshot == initial
                assert run.completed_at is None
                assert run.report_markdown == "Existing historical report"
                continue
            assert run.status == run.snapshot["status"] == "partial"
            assert run.completed_at is not None
            assert run.snapshot["completed_at"]
            assert run.snapshot["topic"] == "robotics"
            assert "restarted" in run.snapshot["message"]
            assert run.snapshot["failed_workers"][-1] == {
                "worker": "orchestrator",
                "reason": "orchestrator_restart",
            }
            assert "Status: partial" in run.report_markdown
            assert "restarted" in run.report_markdown
            files = routes.ROOT / ".state" / "reports"
            assert json.loads((files / f"{identifier}.json").read_text()) == run.snapshot
            assert (files / f"{identifier}.md").read_text() == run.report_markdown
        retained = db.get(Run, row_ids[1]).snapshot
        for key in ("developments", "ledger", "charged_usage"):
            assert retained[key] == saved[key]
        assert retained["failed_workers"][0] == saved["failed_workers"][0]
        assert db.get(Run, row_ids[2]).snapshot["developments"] == incomplete["developments"]
        completed = db.get(Run, row_ids[1]).completed_at

    # A subsequent startup leaves the now-finalized records immutable.
    with TestClient(app.app):
        pass
    with sessions() as db:
        assert db.get(Run, row_ids[1]).snapshot == retained
        assert db.get(Run, row_ids[1]).completed_at == completed
