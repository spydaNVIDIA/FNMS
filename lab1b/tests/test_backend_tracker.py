from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from backend.app import tracker_routes
from backend.app.main import app


def _register_and_login(client: TestClient, username: str) -> str:
    password = "safe-password"
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": password, "email": None},
    )
    assert response.status_code == 201
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200
    return response.json()["token"]


def test_tracker_endpoints_are_authenticated_and_user_scoped() -> None:
    with TestClient(app) as client:
        first_token = _register_and_login(client, "tracker-user-one")
        second_token = _register_and_login(client, "tracker-user-two")
        started = datetime.now(UTC)
        now = started.isoformat()
        completed = (started + timedelta(seconds=90)).isoformat()
        run = {
            "started_at": now,
            "completed_at": completed,
            "status": "complete",
            "topic": "open-source robotics foundation models",
            "top_k": 5,
            "report_markdown": "# Report\n",
            "budget": {"steps": 2},
            "articles": [
                {
                    "title": "Primary release",
                    "url": "https://github.com/example/project",
                    "canonical_url": "https://github.com/example/project",
                    "fetched_at": now,
                    "status": "fetched",
                    "content_hash": "a" * 64,
                }
            ],
            "developments": [
                {
                    "key": "project-release-v1",
                    "title": "Project v1 released",
                    "summary": "The project published weights and code.",
                    "rank": 1,
                    "bucket": "new",
                    "score": 90,
                    "source_urls": ["https://github.com/example/project"],
                }
            ],
            "trace": [
                {
                    "step": 1,
                    "kind": "tool",
                    "tool": "fetch_article",
                    "arguments": {"url": "https://github.com/example/project"},
                    "status": "ok",
                    "latency_ms": 10,
                    "created_at": now,
                }
            ],
        }
        missing = client.get("/api/tracker/state")
        assert missing.status_code == 401
        created = client.post(
            "/api/tracker/runs",
            json=run,
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]

        duplicate = run | {"articles": run["articles"] * 2}
        rejected = client.post(
            "/api/tracker/runs",
            json=duplicate,
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert rejected.status_code == 422

        state = client.get(
            "/api/tracker/state", headers={"Authorization": f"Bearer {first_token}"}
        )
        assert state.status_code == 200
        assert state.json()["previous_top_k"] == ["project-release-v1"]
        assert state.json()["seen_urls"] == ["https://github.com/example/project"]

        report = client.get(
            "/api/tracker/reports/latest",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert report.status_code == 200
        assert report.json()["developments"][0]["title"] == "Project v1 released"
        assert report.json()["run"]["duration_seconds"] == 90

        historical_report = client.get(
            f"/api/tracker/runs/{run_id}/report",
            headers={"Authorization": f"Bearer {first_token}"},
        )
        assert historical_report.status_code == 200
        assert historical_report.json() == report.json()

        history = client.get(
            "/api/tracker/runs", headers={"Authorization": f"Bearer {first_token}"}
        )
        assert history.status_code == 200
        assert history.json()[0]["duration_seconds"] == 90

        hidden = client.get(
            f"/api/tracker/runs/{run_id}/articles",
            headers={"Authorization": f"Bearer {second_token}"},
        )
        assert hidden.status_code == 404
        hidden_report = client.get(
            f"/api/tracker/runs/{run_id}/report",
            headers={"Authorization": f"Bearer {second_token}"},
        )
        assert hidden_report.status_code == 404


def test_ui_run_endpoint_is_authenticated_and_scoped_to_current_user(monkeypatch) -> None:
    launched: dict[str, object] = {}

    def fake_start(user_id: int, token: str) -> dict:
        launched.update(user_id=user_id, token=token)
        return {
            "state": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "completed_at": None,
            "run_id": None,
            "run_status": None,
            "report_path": None,
            "trace_path": None,
            "message": "Tracker run started",
        }

    monkeypatch.setattr(tracker_routes.run_coordinator, "start", fake_start)
    with TestClient(app) as client:
        assert client.post("/api/tracker/run-now").status_code == 401
        token = _register_and_login(client, "ui-runner")
        response = client.post(
            "/api/tracker/run-now",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 202
        assert response.json()["state"] == "running"
        assert launched["user_id"] > 0
        assert launched["token"]
