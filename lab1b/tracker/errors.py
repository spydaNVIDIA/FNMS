"""Failure classification shared by model, search, and fetch calls."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .types import Failure

_DAILY_QUOTA = re.compile(
    r"daily(?: quota| limit)?|per[ -]?day|quota exhausted|credits? exhausted|"
    r"insufficient credits?|billing (?:limit|quota)",
    re.IGNORECASE,
)


class TrackerError(RuntimeError):
    def __init__(self, failure: Failure):
        super().__init__(failure.message)
        self.failure = failure


class BudgetExceeded(TrackerError):
    pass


class PolicyViolation(TrackerError):
    pass


def classify_http_failure(
    status_code: int, headers: Mapping[str, str] | None, body: str
) -> Failure:
    headers = headers or {}
    body = body[:1000]
    if status_code == 400 and "tool_use_failed" in body.lower():
        return Failure(
            "provider_tool_validation",
            True,
            f"Provider rejected generated tool call: {body}",
        )
    if status_code == 429:
        if _DAILY_QUOTA.search(body):
            return Failure("daily_quota", False, f"Daily quota exhausted: {body}")
        retry_after: float | None = None
        try:
            retry_after = max(0.0, float(headers.get("retry-after", "")))
        except ValueError:
            pass
        return Failure("rate_limit", True, f"Per-minute rate limit: {body}", retry_after)
    if status_code in {408, 425} or 500 <= status_code <= 599:
        return Failure("transient_http", True, f"Transient HTTP {status_code}: {body}")
    if status_code in {401, 403}:
        return Failure("authentication", False, f"Authentication failed ({status_code})")
    if status_code == 402:
        return Failure("payment", False, "Provider payment or credit failure")
    return Failure("terminal_http", False, f"HTTP {status_code}: {body}")
