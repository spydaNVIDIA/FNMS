"""Launch one authenticated tracker subprocess at a time for the local UI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RunCoordinator:
    """Coordinate local tracker runs without exposing provider secrets to the browser."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[2]
        self._lock = threading.Lock()
        self._active_user_id: int | None = None
        self._thread: threading.Thread | None = None
        self._statuses: dict[int, dict[str, Any]] = {}

    def start(self, user_id: int, token: str) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("A tracker run is already in progress")
            status = {
                "state": "running",
                "started_at": datetime.now(UTC).isoformat(),
                "completed_at": None,
                "run_id": None,
                "run_status": None,
                "report_path": None,
                "trace_path": None,
                "message": "Tracker run started",
            }
            self._statuses[user_id] = status
            self._active_user_id = user_id
            self._thread = threading.Thread(
                target=self._execute,
                args=(user_id, token),
                name="tracker-runner",
                daemon=True,
            )
            self._thread.start()
            return dict(status)

    def status(self, user_id: int) -> dict[str, Any]:
        with self._lock:
            if (
                self._thread is not None
                and self._thread.is_alive()
                and self._active_user_id != user_id
            ):
                return {
                    "state": "busy",
                    "started_at": None,
                    "completed_at": None,
                    "run_id": None,
                    "run_status": None,
                    "report_path": None,
                    "trace_path": None,
                    "message": "Another tracker run is already in progress",
                }
            return dict(
                self._statuses.get(
                    user_id,
                    {
                        "state": "idle",
                        "started_at": None,
                        "completed_at": None,
                        "run_id": None,
                        "run_status": None,
                        "report_path": None,
                        "trace_path": None,
                        "message": "No UI-triggered run in this server session",
                    },
                )
            )

    def _execute(self, user_id: int, token: str) -> None:
        environment = os.environ.copy()
        environment["TRACKER_API_TOKEN"] = token
        environment["TRACKER_API_BASE_URL"] = "http://127.0.0.1:8000"
        command = [
            sys.executable,
            "-m",
            "tracker",
            "run",
            "--config",
            str(self.root / "config.yaml"),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=self.root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30 * 60,
                check=False,
            )
            if completed.returncode == 0:
                result = json.loads(completed.stdout)
                if result.get("status") == "failed":
                    state = "failed"
                    message = "Tracker completed with a failed report; inspect the saved run"
                elif result.get("status") == "partial":
                    state = "succeeded"
                    message = "Tracker stopped at its budget and saved a partial report"
                else:
                    state = "succeeded"
                    message = "Tracker run completed"
            else:
                result = {}
                state = "failed"
                message = self._failure_message(completed.stderr, completed.returncode)
        except subprocess.TimeoutExpired:
            result = {}
            state = "failed"
            message = "Tracker run exceeded the 30-minute UI timeout"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result = {}
            state = "failed"
            message = f"Tracker runner failed: {exc}"

        with self._lock:
            previous = self._statuses.get(user_id, {})
            self._statuses[user_id] = {
                "state": state,
                "started_at": previous.get("started_at"),
                "completed_at": datetime.now(UTC).isoformat(),
                "run_id": result.get("run_id"),
                "run_status": result.get("status"),
                "report_path": result.get("report_path"),
                "trace_path": result.get("trace_path"),
                "message": message,
            }
            self._active_user_id = None

    @staticmethod
    def _failure_message(stderr: str, returncode: int) -> str:
        try:
            payload = json.loads(stderr)
            message = payload.get("message")
            if message:
                return str(message)
        except (json.JSONDecodeError, TypeError):
            pass
        return f"Tracker process exited with code {returncode}"


run_coordinator = RunCoordinator()
