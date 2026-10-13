"""Shared typed records used by the tracker runtime."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ArticleRecord:
    title: str
    url: str
    canonical_url: str
    fetched_at: str
    status: str
    rejection_reason: str | None = None
    content_hash: str | None = None
    text: str = ""

    def persistence_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("text")
        return value


@dataclass
class Development:
    key: str
    title: str
    summary: str
    score: float
    source_urls: list[str]
    rank: int | None = None
    bucket: str = "new"


@dataclass
class TraceEvent:
    step: int
    kind: str
    status: str
    latency_ms: int
    tool: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    credits: float = 0
    error_class: str | None = None
    created_at: str = field(default_factory=lambda: utc_now().isoformat())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Failure:
    category: str
    transient: bool
    message: str
    retry_after: float | None = None
