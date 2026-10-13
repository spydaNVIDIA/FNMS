"""Pydantic request/response schemas.

Response models intentionally omit `password_hash`, so a hash can never be
serialized out of any endpoint (Rule 1).
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    email: EmailStr | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class UpdateUserRequest(BaseModel):
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: EmailStr | None = None
    created_at: datetime


class TokenOut(BaseModel):
    token: str
    token_type: str = "bearer"


class HealthOut(BaseModel):
    status: str


class TrackerArticleIn(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    url: str
    canonical_url: str
    fetched_at: datetime
    status: Literal["fetched", "skipped", "rejected", "failed"]
    rejection_reason: str | None = None
    content_hash: str | None = Field(default=None, max_length=64)


class TrackerDevelopmentIn(BaseModel):
    key: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=1)
    bucket: Literal["new", "still", "dropped"]
    score: float
    source_urls: list[str] = Field(min_length=1)


class TrackerTraceIn(BaseModel):
    step: int = Field(ge=0)
    kind: Literal["model", "tool", "runtime"]
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str
    latency_ms: int = Field(ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    credits: float = Field(default=0, ge=0)
    error_class: str | None = None
    created_at: datetime


class TrackerRunCreate(BaseModel):
    started_at: datetime
    completed_at: datetime | None = None
    status: Literal["complete", "partial", "failed"]
    topic: str = Field(min_length=1, max_length=255)
    top_k: int = Field(ge=3, le=10)
    report_markdown: str = Field(min_length=1)
    budget: dict[str, Any] = Field(default_factory=dict)
    articles: list[TrackerArticleIn] = Field(default_factory=list)
    developments: list[TrackerDevelopmentIn] = Field(default_factory=list)
    trace: list[TrackerTraceIn] = Field(default_factory=list)


class TrackerRunCreated(BaseModel):
    id: int


class TrackerRunLaunchOut(BaseModel):
    state: Literal["idle", "busy", "running", "succeeded", "failed"]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    run_id: int | None = None
    run_status: Literal["complete", "partial", "failed"] | None = None
    report_path: str | None = None
    trace_path: str | None = None
    message: str


class TrackerStateDevelopment(BaseModel):
    key: str
    title: str
    summary: str
    source_urls: list[str]
    last_seen_at: datetime


class TrackerStateOut(BaseModel):
    seen_urls: list[str]
    developments: list[TrackerStateDevelopment]
    previous_top_k: list[str]


class TrackerRunSummaryOut(BaseModel):
    id: int
    started_at: datetime
    completed_at: datetime | None
    duration_seconds: float | None
    status: str
    topic: str
    top_k: int


class TrackerDevelopmentOut(BaseModel):
    key: str
    title: str
    summary: str
    rank: int | None
    bucket: str
    score: float
    source_urls: list[str]


class TrackerReportOut(BaseModel):
    run: TrackerRunSummaryOut
    report_markdown: str
    developments: list[TrackerDevelopmentOut]


class TrackerArticleOut(BaseModel):
    title: str
    url: str
    canonical_url: str
    fetched_at: datetime
    status: str
    rejection_reason: str | None
