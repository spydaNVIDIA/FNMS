"""Request-local deadlines, accounting and bounded external I/O.

The worker owns this context for the entire logical request, including retries.
No caller retry resets its deadline, counters, collected evidence or partial result.
"""

import asyncio
import ipaddress
import json
import os
import re
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from .errors import BudgetStop, ProviderFailure, SourceUnavailable
from .schemas import Usage, WorkerRequest
from .tracing import emit


def provider_failure(response: httpx.Response) -> ProviderFailure:
    """Classify without returning provider response bodies that may contain secrets."""
    status = response.status_code
    text = response.text[:4000].lower()
    if status in {401, 403}:
        return ProviderFailure("provider_authentication", "Provider rejected its API key")
    if status == 402:
        return ProviderFailure("provider_payment", "Provider payment allowance exhausted")
    if status == 429:
        if re.search(r"per[ -]day|\btpd\b|\brpd\b|daily|quota exhausted|credits? exhausted", text):
            return ProviderFailure("provider_daily_quota", "Provider daily quota exhausted")
        delay = None
        try:
            delay = max(0.1, float(response.headers.get("retry-after", "")))
        except ValueError:
            match = re.search(r"try again in\s+([\d.]+)s", text)
            if match:
                delay = float(match.group(1)) + 0.5
        return ProviderFailure(
            "provider_rate_limit", "Provider temporarily rate limited", True, delay
        )
    if status in {408, 425} or status >= 500:
        return ProviderFailure("provider_unavailable", "Provider temporarily unavailable", True)
    return ProviderFailure("provider_bad_response", f"Provider returned HTTP {status}")


class Context:
    def __init__(self, service: str, request: WorkerRequest, *, started=None, client=None):
        self.service, self.request = service, request
        self.deadline = (
            time.monotonic() if started is None else started
        ) + request.time_budget_ms / 1000
        self.usage = Usage()
        self.partial_result = {}
        self.search_calls = 0
        self.retry_after = 1.0
        self.retry_event = asyncio.Event()
        # Pinned-IP URLs must not pool HTTPS connections across original hostnames.
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_keepalive_connections=0),
        )
        self._owns_client = client is None
        self._last_model_started = 0.0

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    @property
    def remaining_seconds(self):
        return max(0.0, self.deadline - time.monotonic())

    def check(self):
        if self.remaining_seconds <= 0:
            raise BudgetStop("time_budget_exhausted", "Worker elapsed-time budget exhausted")

    def trace(self, tool, status, started, attempt=1, tokens=0):
        emit(
            self.service,
            run_id=self.request.run_id,
            request_id=self.request.request_id,
            step=self.usage.steps,
            direction="out",
            endpoint_or_tool=tool,
            status=status,
            latency_ms=(time.monotonic() - started) * 1000,
            attempt=attempt,
            tokens=tokens,
        )

    async def _wait_retry(self, failure, attempt):
        self.check()
        delay = (
            failure.retry_after if failure.retry_after is not None else min(2 ** (attempt - 1), 30)
        )
        self.retry_after = max(0.1, delay)
        self.retry_event.set()
        await asyncio.sleep(min(self.retry_after, self.remaining_seconds))
        self.retry_event.clear()
        self.check()

    async def model(self, messages, *, tools=None, response_format=None, max_output_tokens=1024):
        if tools and response_format is not None:
            raise ValueError("Tool calls and structured responses cannot be combined")
        if self._owns_client and os.getenv("LIVE_CALLS_APPROVED", "false").lower() != "true":
            raise ProviderFailure(
                "live_calls_disabled",
                "Set LIVE_CALLS_APPROVED=true after checking provider spend limits",
            )
        api_key = os.getenv("MODEL_API_KEY", "")
        if not api_key:
            raise ProviderFailure("provider_authentication", "MODEL_API_KEY is not configured")
        base = os.getenv("MODEL_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
        max_output_tokens = min(
            max_output_tokens, int(os.getenv("MODEL_MAX_OUTPUT_TOKENS", "1800"))
        )
        body = {
            "model": os.getenv("MODEL_NAME", "openai/gpt-oss-120b"),
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_output_tokens,
        }
        if tools:
            body.update(tools=tools, tool_choice="auto")
        if response_format is not None:
            body["response_format"] = response_format
        attempt = 0
        invalid_tool_attempts = 0
        while True:
            self.check()
            pacing = float(os.getenv("MODEL_MIN_INTERVAL_SECONDS", "20"))
            wait = max(0.0, self._last_model_started + pacing - time.monotonic())
            if wait:
                await asyncio.sleep(min(wait, self.remaining_seconds))
                self.check()
            limits = self.request.budget
            if self.usage.steps >= limits.max_steps:
                raise BudgetStop("step_budget_exhausted", "Model-call budget exhausted")
            # UTF-8 bytes plus framing is a conservative bound for byte-based tokenizers.
            # Include tools, which are also part of the provider's input context.
            estimate = len(json.dumps(body, ensure_ascii=False).encode("utf-8")) + 512
            reservation = estimate + max_output_tokens
            if self.usage.tokens + reservation > limits.max_tokens:
                raise BudgetStop(
                    "token_budget_exhausted", "Insufficient tokens for next model call"
                )
            self.usage.steps += 1
            self.usage.tokens += reservation
            attempt += 1
            started = time.monotonic()
            self._last_model_started = started
            traced = False
            try:
                async with asyncio.timeout(self.remaining_seconds):
                    response = await self.client.post(
                        base + "/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=min(45.0, self.remaining_seconds),
                    )
                if response.status_code >= 400:
                    if response.status_code == 400 and "tool_use_failed" in response.text[:4000]:
                        invalid_tool_attempts += 1
                        self.trace(
                            "model", "provider_tool_validation", started, attempt, reservation
                        )
                        traced = True
                        if invalid_tool_attempts <= 2:
                            names = [item.get("function", {}).get("name") for item in tools or []]
                            body["messages"] = list(body["messages"]) + [
                                {
                                    "role": "user",
                                    "content": f"Your previous tool call was rejected. Use only these tools: {names}. "
                                    "Follow the JSON schema exactly, including all required arguments.",
                                }
                            ]
                            continue
                        raise ProviderFailure(
                            "provider_tool_validation",
                            "Model repeatedly produced invalid tool calls",
                        )
                    failure = provider_failure(response)
                    self.trace("model", failure.code, started, attempt, reservation)
                    traced = True
                    if failure.retryable:
                        await self._wait_retry(failure, attempt)
                        continue
                    raise failure
                data = response.json()
                message = data["choices"][0]["message"]
                if not isinstance(message, dict):
                    raise ValueError("invalid assistant message")
                usage = data.get("usage", {})
                actual = usage.get("total_tokens")
                if actual is None and "prompt_tokens" in usage and "completion_tokens" in usage:
                    actual = usage["prompt_tokens"] + usage["completion_tokens"]
                if isinstance(actual, bool) or not isinstance(actual, int) or actual < 0:
                    actual = reservation  # Unknown usage stays conservatively reserved.
                self.usage.tokens += actual - reservation
                self.trace("model", "ok", started, attempt, actual)
                traced = True
                if self.usage.tokens > limits.max_tokens:
                    raise BudgetStop(
                        "token_budget_exhausted", "Provider reported usage over budget"
                    )
                return message
            except TimeoutError:
                self.trace("model", "time_budget_exhausted", started, attempt, reservation)
                traced = True
                raise BudgetStop(
                    "time_budget_exhausted", "Model call reached worker deadline"
                ) from None
            except httpx.TransportError:
                self.trace("model", "network_error", started, attempt, reservation)
                traced = True
                await self._wait_retry(
                    ProviderFailure("network", "Provider network failure", True), attempt
                )
            except (ValueError, KeyError, IndexError, TypeError):
                self.trace("model", "invalid_response", started, attempt, reservation)
                traced = True
                raise ProviderFailure(
                    "provider_bad_response", "Provider returned malformed JSON"
                ) from None
            finally:
                # The worker's outer deadline can cancel this await before this
                # method's timeout handler runs. Preserve its call and reservation.
                if not traced:
                    status = (
                        "time_budget_exhausted" if not self.remaining_seconds else "interrupted"
                    )
                    self.trace("model", status, started, attempt, reservation)

    async def search(self, query, max_results=5):
        self.check()
        if self._owns_client and os.getenv("LIVE_CALLS_APPROVED", "false").lower() != "true":
            raise ProviderFailure(
                "live_calls_disabled",
                "Set LIVE_CALLS_APPROVED=true after checking provider spend limits",
            )
        if self.search_calls >= int(os.getenv("SEARCH_MAX_CALLS", "4")):
            raise BudgetStop("search_budget_exhausted", "Search allowance exhausted")
        key = os.getenv("TAVILY_API_KEY", "")
        if not key:
            raise ProviderFailure("provider_authentication", "TAVILY_API_KEY is not configured")
        self.search_calls += 1
        started = time.monotonic()
        traced = False
        try:
            async with asyncio.timeout(self.remaining_seconds):
                response = await self.client.post(
                    os.getenv("SEARCH_URL", "https://api.tavily.com/search"),
                    headers={"Authorization": f"Bearer {key}"},
                    json={
                        "query": query,
                        "max_results": min(
                            max_results, int(os.getenv("SEARCH_MAX_RESULTS", "5")), 10
                        ),
                        "search_depth": "basic",
                    },
                    timeout=min(30.0, self.remaining_seconds),
                )
            if response.status_code >= 400:
                failure = provider_failure(response)
                self.trace("search_web", failure.code, started, self.search_calls)
                traced = True
                if failure.retryable:
                    await self._wait_retry(failure, self.search_calls)
                    return await self.search(query, max_results)
                raise failure
            data = response.json()
            result = [
                {
                    "title": str(item.get("title", ""))[:500],
                    "url": str(item["url"]),
                    "snippet": str(item.get("content", ""))[:1200],
                }
                for item in data["results"]
                if item.get("url")
            ]
            self.trace("search_web", "ok", started, self.search_calls)
            traced = True
            return result
        except TimeoutError:
            self.trace("search_web", "time_budget_exhausted", started, self.search_calls)
            traced = True
            raise BudgetStop("time_budget_exhausted", "Search reached worker deadline") from None
        except httpx.TransportError:
            self.trace("search_web", "network_error", started, self.search_calls)
            traced = True
            await self._wait_retry(
                ProviderFailure("network", "Search network failure", True), self.search_calls
            )
            return await self.search(query, max_results)
        except (ValueError, KeyError, TypeError):
            self.trace("search_web", "invalid_response", started, self.search_calls)
            traced = True
            raise ProviderFailure(
                "provider_bad_response", "Search returned malformed JSON"
            ) from None
        finally:
            if not traced:
                status = "time_budget_exhausted" if not self.remaining_seconds else "interrupted"
                self.trace("search_web", status, started, self.search_calls)

    async def _public_target(self, url):
        try:
            parts = urlsplit(url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username
                or parts.password
            ):
                raise SourceUnavailable("Only HTTP(S) URLs without credentials are allowed")
            port = parts.port or (443 if parts.scheme == "https" else 80)
            host = parts.hostname.rstrip(".").lower()
            try:
                addresses = [str(ipaddress.ip_address(host))]
            except ValueError:
                answers = await asyncio.to_thread(
                    socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
                )
                addresses = sorted({answer[4][0] for answer in answers})
            if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise SourceUnavailable("Source resolves to a forbidden nonpublic address")
            return host, port, addresses[0]
        except (ValueError, socket.gaierror):
            raise SourceUnavailable("Invalid URL or DNS resolution failed") from None

    async def fetch(self, url):
        """Connect to a checked IP, retaining the hostname for Host/TLS verification."""
        current = url
        redirects = int(os.getenv("FETCH_MAX_REDIRECTS", "3"))
        max_bytes = int(os.getenv("FETCH_MAX_BYTES", "2000000"))
        try:
            async with asyncio.timeout(self.remaining_seconds):
                for hop in range(redirects + 1):
                    self.check()
                    host, port, address = await self._public_target(current)
                    if self.usage.fetches >= self.request.budget.max_fetches:
                        raise BudgetStop("fetch_budget_exhausted", "Page-request budget exhausted")
                    self.usage.fetches += 1
                    parsed = httpx.URL(current)
                    pinned = parsed.copy_with(host=address)
                    default_port = 443 if parsed.scheme == "https" else 80
                    authority = f"[{host}]" if ":" in host else host
                    if port != default_port:
                        authority += f":{port}"
                    started = time.monotonic()
                    status = "network_error"
                    try:
                        async with self.client.stream(
                            "GET",
                            pinned,
                            headers={"Host": authority, "User-Agent": "FNMS-Lab2/1.0"},
                            extensions={"sni_hostname": host},
                            follow_redirects=False,
                            timeout=min(
                                float(os.getenv("FETCH_TIMEOUT_SECONDS", "20")),
                                self.remaining_seconds,
                            ),
                        ) as response:
                            status = str(response.status_code)
                            if response.status_code in {301, 302, 303, 307, 308}:
                                if hop == redirects or not response.headers.get("location"):
                                    raise SourceUnavailable(
                                        "Redirect limit or missing redirect location"
                                    )
                                current = urljoin(current, response.headers["location"])
                                continue
                            if response.status_code >= 400:
                                # Do not read an unbounded error body.
                                raise SourceUnavailable(
                                    f"Source returned HTTP {response.status_code}"
                                )
                            content_type = (
                                response.headers.get("content-type", "").split(";")[0].lower()
                            )
                            if content_type not in {
                                "text/plain",
                                "text/html",
                                "application/xhtml+xml",
                            }:
                                raise SourceUnavailable("Source has unsupported content type")
                            payload = bytearray()
                            async for chunk in response.aiter_bytes():
                                self.check()
                                if len(payload) + len(chunk) > max_bytes:
                                    raise SourceUnavailable("Source exceeds byte limit")
                                payload.extend(chunk)
                            decoded = payload.decode("utf-8", errors="replace")
                            if content_type == "text/plain":
                                return decoded
                            soup = BeautifulSoup(decoded, "html.parser")
                            for node in soup(
                                ["script", "style", "noscript", "svg", "nav", "footer"]
                            ):
                                node.decompose()
                            main = soup.find("article") or soup.find("main") or soup
                            return main.get_text("\n", strip=True)
                    finally:
                        self.trace("fetch_article", status, started, hop + 1)
        except TimeoutError:
            raise BudgetStop(
                "time_budget_exhausted", "Page fetch reached worker deadline"
            ) from None
        except httpx.TransportError:
            raise SourceUnavailable("Source connection failed or timed out") from None
        raise SourceUnavailable("Source could not be retrieved")
