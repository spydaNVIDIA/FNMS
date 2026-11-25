"""Budget, rate-limit and guarded-fetch tests use only mocked network transports."""

import asyncio
import json
import socket
from uuid import uuid4

import httpx
import pytest

from common.runtime import BudgetStop, Context, SourceUnavailable, provider_failure
from common.schemas import Limits, WorkerRequest


def request(**limits):
    return WorkerRequest(
        request_id=uuid4(),
        run_id=uuid4(),
        time_budget_ms=1000,
        budget=Limits(**({"max_steps": 10, "max_fetches": 8, "max_tokens": 40000} | limits)),
        payload={},
    )


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("MODEL_API_KEY", "offline-test-key")
    monkeypatch.setenv("MODEL_MIN_INTERVAL_SECONDS", "0")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[fe80::1]/",
        "http://0.0.0.0/",
        "http://[::]/",
    ],
)
def test_forbidden_sources_never_make_an_http_request(url):
    async def run():
        calls = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: calls.append(req))
        ) as client:
            ctx = Context("verifier", request(), client=client)
            with pytest.raises(SourceUnavailable):
                await ctx.fetch(url)
            assert not calls
            assert ctx.usage.fetches == 0

    asyncio.run(run())


def test_dns_is_checked_and_connection_pinned(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )

    async def run():
        def handler(req):
            assert req.url.host == "93.184.216.34"
            assert req.headers["host"] == "public.example"
            assert req.extensions["sni_hostname"] == "public.example"
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<nav>noise</nav><main>Useful article</main>",
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctx = Context("verifier", request(), client=client)
            assert await ctx.fetch("https://public.example/") == "Useful article"
            assert ctx.usage.fetches == 1

    asyncio.run(run())


def test_private_dns_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("10.0.0.1", 443))])

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: pytest.fail("HTTP attempted"))
        ) as client:
            with pytest.raises(SourceUnavailable):
                await Context("verifier", request(), client=client).fetch(
                    "https://private.example/"
                )

    asyncio.run(run())


def test_redirect_is_revalidated_and_charged_per_hop():
    async def run():
        calls = []

        def handler(req):
            calls.append(req)
            location = "http://93.184.216.35/" if len(calls) == 1 else "http://169.254.169.254/"
            return httpx.Response(302, headers={"location": location})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctx = Context("verifier", request(), client=client)
            with pytest.raises(SourceUnavailable):
                await ctx.fetch("http://93.184.216.34/")
            assert len(calls) == ctx.usage.fetches == 2

    asyncio.run(run())


def test_fetch_limit_stops_before_second_redirect_hop():
    async def run():
        calls = []

        def handler(req):
            calls.append(req)
            return httpx.Response(302, headers={"location": "http://93.184.216.35/"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctx = Context("verifier", request(max_fetches=1), client=client)
            with pytest.raises(BudgetStop, match="Page-request budget"):
                await ctx.fetch("http://93.184.216.34/")
            assert len(calls) == 1

    asyncio.run(run())


def test_page_size_is_bounded(monkeypatch):
    monkeypatch.setenv("FETCH_MAX_BYTES", "10")

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200, headers={"content-type": "text/plain"}, text="a" * 11
                )
            )
        ) as client:
            with pytest.raises(SourceUnavailable, match="byte limit"):
                await Context("verifier", request(), client=client).fetch("http://93.184.216.34/")

    asyncio.run(run())


@pytest.mark.parametrize(
    "limits,code",
    [({"max_steps": 0}, "step_budget_exhausted"), ({"max_tokens": 0}, "token_budget_exhausted")],
)
def test_model_budget_checked_before_network(limits, code):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: pytest.fail("HTTP attempted"))
        ) as client:
            ctx = Context("verifier", request(**limits), client=client)
            with pytest.raises(BudgetStop) as stopped:
                await ctx.model([{"role": "user", "content": "test"}])
            assert stopped.value.code == code

    asyncio.run(run())


def test_model_usage_reconciles_reservation():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                    },
                )
            )
        ) as client:
            ctx = Context("verifier", request(), client=client)
            assert await ctx.model([{"role": "user", "content": "test"}]) == {"content": "ok"}
            assert ctx.usage.tokens == 120
            assert ctx.usage.steps == 1

    asyncio.run(run())


def test_runtime_honors_configured_io_caps(monkeypatch):
    monkeypatch.setenv("MODEL_MAX_OUTPUT_TOKENS", "100")
    monkeypatch.setenv("SEARCH_MAX_RESULTS", "2")
    monkeypatch.setenv("FETCH_TIMEOUT_SECONDS", "0.5")
    monkeypatch.setenv("TAVILY_API_KEY", "offline-fixture-key")

    async def run():
        requests = []

        def provider(req):
            requests.append(req)
            if req.url.path.endswith("chat/completions"):
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"total_tokens": 20},
                    },
                )
            if req.url.path.endswith("search"):
                return httpx.Response(200, json={"results": []})
            return httpx.Response(200, headers={"content-type": "text/plain"}, text="source")

        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            context = Context("researcher", request(), client=client)
            await context.model([{"role": "user", "content": "fixture"}], max_output_tokens=512)
            await context.search("fixture", max_results=5)
            await context.fetch("http://93.184.216.34/")
        assert json.loads(requests[0].content)["max_tokens"] == 100
        assert json.loads(requests[1].content)["max_results"] == 2
        assert requests[2].extensions["timeout"]["read"] == 0.5

    asyncio.run(run())


def test_rate_limit_wait_is_bounded_by_worker_deadline():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    429, headers={"retry-after": "60"}, text="tokens per minute"
                )
            )
        ) as client:
            envelope = request()
            envelope.time_budget_ms = 10
            ctx = Context("verifier", envelope, client=client)
            with pytest.raises(BudgetStop) as stopped:
                await ctx.model([{"role": "user", "content": "test"}])
            assert stopped.value.code == "time_budget_exhausted"
            assert ctx.usage.steps == 1

    asyncio.run(run())


def test_daily_quota_and_body_wait_classification():
    daily = provider_failure(httpx.Response(429, text="tokens per day (TPD). Try again in 30s"))
    assert daily.code == "provider_daily_quota"
    assert not daily.retryable
    minute = provider_failure(httpx.Response(429, text="TPM: Please try again in 27.105s"))
    assert minute.retryable
    assert minute.retry_after >= 27.105


def test_invalid_generated_tool_can_be_corrected_within_budget():
    async def run():
        count = 0

        def handler(req):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(400, text='{"error":{"code":"tool_use_failed"}}')
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "corrected"}}],
                    "usage": {"total_tokens": 200},
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            ctx = Context("researcher", request(), client=client)
            assert (await ctx.model([{"role": "user", "content": "test"}]))[
                "content"
            ] == "corrected"
            assert ctx.usage.steps == count == 2
            assert ctx.usage.tokens > 200  # Failed attempt's unknown usage remains reserved.

    asyncio.run(run())
