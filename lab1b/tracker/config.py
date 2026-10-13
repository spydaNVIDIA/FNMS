"""Validated tracker policy loaded from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Limits:
    max_steps: int
    max_searches: int
    max_fetches: int
    max_model_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_credits: float
    max_cost_usd: float
    max_retries: int
    backoff_seconds: float
    backoff_cap_seconds: float


@dataclass(frozen=True)
class TrackerConfig:
    path: Path
    topic: str
    top_k: int
    instructions: tuple[str, ...]
    model: dict[str, Any]
    search: dict[str, Any]
    tools: tuple[str, ...]
    limits: Limits
    fetch: dict[str, Any]
    allowed_schemes: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    schemas: dict[str, Any]


def default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config.yaml"


def load_config(path: str | Path | None = None) -> TrackerConfig:
    config_path = Path(path) if path else default_config_path()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must contain an object")
    top_k = int(raw["top_k"])
    if not 3 <= top_k <= 10:
        raise ValueError("top_k must be between 3 and 10")
    tools = tuple(raw["tools"])
    required_tools = {"search_web", "fetch_article", "finish"}
    if not required_tools.issubset(tools):
        raise ValueError(f"tools must include {sorted(required_tools)}")
    allowed_schemes = tuple(str(value).lower() for value in raw["allowed_schemes"])
    if set(allowed_schemes) - {"http", "https"}:
        raise ValueError("only http and https may be allowed")
    limits = Limits(**raw["limits"])
    positive_limits = (
        limits.max_steps,
        limits.max_searches,
        limits.max_fetches,
        limits.max_model_calls,
        limits.max_input_tokens,
        limits.max_output_tokens,
    )
    if any(value <= 0 for value in positive_limits):
        raise ValueError("all hard limits must be positive")
    return TrackerConfig(
        path=config_path.resolve(),
        topic=str(raw["topic"]),
        top_k=top_k,
        instructions=tuple(raw["instructions"]),
        model=dict(raw["model"]),
        search=dict(raw["search"]),
        tools=tools,
        limits=limits,
        fetch=dict(raw["fetch"]),
        allowed_schemes=allowed_schemes,
        allowed_hosts=tuple(str(host).lower().rstrip(".") for host in raw["allowed_hosts"]),
        schemas=dict(raw["schemas"]),
    )
