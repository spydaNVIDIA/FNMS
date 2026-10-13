"""Authenticated client for tracker state stored by the A1 backend."""

from __future__ import annotations

import os
from typing import Any, Protocol

import httpx

from .errors import TrackerError, classify_http_failure
from .types import Failure


class StateClient(Protocol):
    def load(self) -> dict[str, Any]: ...

    def save(self, run: dict[str, Any]) -> int: ...


class APIStateClient:
    def __init__(self, client: httpx.Client | None = None):
        self.base_url = os.getenv("TRACKER_API_BASE_URL", "http://localhost:8000").rstrip("/")
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=30)
        self.token = os.getenv("TRACKER_API_TOKEN")

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _ensure_token(self) -> str:
        if self.token:
            return self.token
        username = os.getenv("TRACKER_USERNAME")
        password = os.getenv("TRACKER_PASSWORD")
        if not username or not password:
            raise TrackerError(
                Failure(
                    "authentication",
                    False,
                    "Set TRACKER_API_TOKEN or TRACKER_USERNAME and TRACKER_PASSWORD",
                )
            )
        response = self.client.post(
            f"{self.base_url}/api/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code >= 400:
            raise TrackerError(
                classify_http_failure(response.status_code, response.headers, response.text)
            )
        self.token = response.json()["token"]
        return self.token

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self._ensure_token()}"},
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise TrackerError(Failure("timeout", True, "State API timed out")) from exc
        except httpx.NetworkError as exc:
            raise TrackerError(Failure("network", True, f"State API network failure: {exc}")) from exc
        if response.status_code >= 400:
            raise TrackerError(
                classify_http_failure(response.status_code, response.headers, response.text)
            )
        return response

    def load(self) -> dict[str, Any]:
        return self._request("GET", "/api/tracker/state").json()

    def save(self, run: dict[str, Any]) -> int:
        return int(self._request("POST", "/api/tracker/runs", json=run).json()["id"])
