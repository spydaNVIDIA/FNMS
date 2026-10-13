"""Minimal HTTP providers; no agent framework is used."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from .config import TrackerConfig
from .errors import TrackerError, classify_http_failure
from .types import Failure


@dataclass(frozen=True)
class ModelResult:
    message: dict[str, Any]
    input_tokens: int
    output_tokens: int


class OpenAICompatibleProvider:
    def __init__(self, config: TrackerConfig, client: httpx.Client | None = None):
        self.config = config
        self.api_key = os.getenv("MODEL_API_KEY")
        if not self.api_key:
            raise TrackerError(Failure("authentication", False, "MODEL_API_KEY is not set"))
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=float(config.model["timeout_seconds"]))

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> ModelResult:
        endpoint = f"{str(self.config.model['base_url']).rstrip('/')}/chat/completions"
        try:
            response = self.client.post(
                endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.config.model["name"],
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                },
            )
        except httpx.TimeoutException as exc:
            raise TrackerError(Failure("timeout", True, "Model request timed out")) from exc
        except httpx.NetworkError as exc:
            raise TrackerError(Failure("network", True, f"Model network failure: {exc}")) from exc
        if response.status_code >= 400:
            raise TrackerError(
                classify_http_failure(response.status_code, response.headers, response.text)
            )
        data = response.json()
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise TrackerError(
                Failure("provider_response", False, "Model returned an invalid response")
            ) from exc
        usage = data.get("usage", {})
        return ModelResult(
            message=message,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )
