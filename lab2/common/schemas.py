"""The HTTP contract. No worker implementation or persistence is shared here."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Claim(StrictModel):
    claim_id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=4000)
    source_url: str = Field(min_length=1, max_length=4096)


class Development(StrictModel):
    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=4000)
    claims: list[Claim] = Field(min_length=1, max_length=20)


class Verdict(StrictModel):
    claim_id: str = Field(min_length=1, max_length=160)
    verdict: Literal["supported", "unsupported", "unreachable"]
    evidence: str | None

    @model_validator(mode="after")
    def evidence_matches_verdict(self):
        if self.verdict == "supported":
            if not self.evidence or not self.evidence.strip() or len(self.evidence) > 300:
                raise ValueError("supported requires nonempty evidence of at most 300 characters")
        elif self.evidence is not None:
            raise ValueError("non-supported verdicts require null evidence")
        return self


class Limits(StrictModel):
    max_steps: int = Field(ge=0, le=1000)
    max_fetches: int = Field(ge=0, le=1000)
    max_tokens: int = Field(ge=0, le=2000000)


class Usage(StrictModel):
    steps: int = Field(default=0, ge=0)
    fetches: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)


class Error(StrictModel):
    code: str
    message: str
    retryable: bool


class WorkerRequest(StrictModel):
    # UUID coercion is intentional on the JSON boundary; other fields stay strict.
    request_id: UUID = Field(strict=False)
    run_id: UUID = Field(strict=False)
    time_budget_ms: int = Field(ge=1, le=1800000)
    budget: Limits
    payload: dict[str, Any]


class WorkerResponse(StrictModel):
    request_id: UUID = Field(strict=False)
    run_id: UUID = Field(strict=False)
    status: Literal["complete", "partial", "failed"]
    usage: Usage
    result: dict[str, Any]
    error: Error | None = None

    @model_validator(mode="after")
    def status_matches_error(self):
        if self.status != "complete" and self.error is None:
            raise ValueError("partial and failed results require an error")
        if self.status == "complete" and self.error is not None:
            raise ValueError("complete results must not contain an error")
        return self


class ResearchPayload(StrictModel):
    topic: str = Field(min_length=1, max_length=500)
    k: int = Field(ge=3, le=10)


class VerifyPayload(StrictModel):
    claims: list[Claim] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_claims(self):
        ids = [claim.claim_id for claim in self.claims]
        if len(ids) != len(set(ids)):
            raise ValueError("claim IDs must be unique")
        return self


class ResearchResult(StrictModel):
    developments: list[Development]

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.id for item in self.developments]
        claims = [claim.claim_id for item in self.developments for claim in item.claims]
        if len(ids) != len(set(ids)) or len(claims) != len(set(claims)):
            raise ValueError("development and claim IDs must be unique")
        return self


class VerifyResult(StrictModel):
    verdicts: list[Verdict]

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [item.claim_id for item in self.verdicts]
        if len(ids) != len(set(ids)):
            raise ValueError("verdict claim IDs must be unique")
        return self
