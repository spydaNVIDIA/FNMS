from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend.app.run_service import RunCoordinator


def test_run_coordinator_passes_user_token_and_records_result(
    tmp_path: Path, monkeypatch
) -> None:
    observed: dict = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        observed["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "run_id": 42,
                    "status": "complete",
                    "report_path": "reports/diagnostic.md",
                    "trace_path": "logs/diagnostic.jsonl",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("backend.app.run_service.subprocess.run", fake_run)
    coordinator = RunCoordinator(tmp_path)
    coordinator._statuses[7] = {"started_at": "2026-10-12T00:00:00+00:00"}
    coordinator._execute(7, "short-lived-user-token")

    status = coordinator.status(7)
    assert status["state"] == "succeeded"
    assert status["run_id"] == 42
    assert observed["cwd"] == tmp_path
    assert observed["environment"]["TRACKER_API_TOKEN"] == "short-lived-user-token"
    assert observed["environment"]["TRACKER_API_BASE_URL"] == "http://127.0.0.1:8000"
    assert observed["command"][-1] == str(tmp_path / "config.yaml")


def test_run_coordinator_surfaces_failed_agent_status(tmp_path: Path, monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "run_id": 43,
                    "status": "failed",
                    "report_path": "reports/failed.md",
                    "trace_path": "logs/failed.jsonl",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("backend.app.run_service.subprocess.run", fake_run)
    coordinator = RunCoordinator(tmp_path)
    coordinator._statuses[8] = {"started_at": "2026-10-12T00:00:00+00:00"}
    coordinator._execute(8, "short-lived-user-token")

    status = coordinator.status(8)
    assert status["state"] == "failed"
    assert status["run_status"] == "failed"
