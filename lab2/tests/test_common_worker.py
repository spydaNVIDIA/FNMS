"""Exercise the real HTTP boundaries without model/search spending."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from common.runtime import BudgetStop, ProviderFailure
from common.schemas import ResearchPayload, VerifyPayload
from common.worker import create_worker


def envelope(payload):
    return {
        "request_id": str(uuid4()),
        "run_id": str(uuid4()),
        "time_budget_ms": 1000,
        "budget": {"max_steps": 10, "max_fetches": 10, "max_tokens": 40000},
        "payload": payload,
    }


@pytest.fixture(autouse=True)
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("RESEARCHER_TOKEN", "research-secret")
    monkeypatch.setenv("VERIFIER_TOKEN", "verify-secret")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))


@pytest.mark.parametrize(
    "service,path,kind,payload,secret,result",
    [
        (
            "researcher",
            "/research",
            ResearchPayload,
            {"topic": "robotics", "k": 5},
            "research-secret",
            {"developments": []},
        ),
        (
            "verifier",
            "/verify",
            VerifyPayload,
            {"claims": [{"claim_id": "c1", "text": "claim", "source_url": "https://example.com"}]},
            "verify-secret",
            {"verdicts": []},
        ),
    ],
)
def test_auth_schema_and_semantic_replay(service, path, kind, payload, secret, result):
    async def run():
        calls = []

        async def handler(body, context):
            calls.append(body)
            context.usage.steps += 1
            return result

        app = create_worker(service, path, kind, handler)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            assert (await client.get("/healthz")).json() == {"status": "ok"}
            assert (await client.post(path, content="{broken")).status_code == 401
            assert (
                await client.post(
                    path, json=envelope(payload), headers={"Authorization": "Bearer wrong"}
                )
            ).status_code == 401
            other = "verify-secret" if service == "researcher" else "research-secret"
            assert (
                await client.post(
                    path, json=envelope(payload), headers={"Authorization": f"Bearer {other}"}
                )
            ).status_code == 401
            headers = {"Authorization": f"Bearer {secret}"}
            assert (await client.post(path, content="{broken", headers=headers)).status_code == 400
            assert (await client.post(path, json={}, headers=headers)).status_code == 400
            request = envelope(payload)
            first = await client.post(path, json=request, headers=headers)
            reordered = dict(reversed(list(request.items())))
            replay = await client.post(
                path, content=json.dumps(reordered, indent=4), headers=headers
            )
            assert first.status_code == replay.status_code == 200
            assert first.json() == replay.json()
            assert len(calls) == 1
            conflict = await client.post(
                path, json=request | {"time_budget_ms": 900}, headers=headers
            )
            assert conflict.status_code == 409

    asyncio.run(run())


def test_concurrent_duplicate_does_not_execute_twice():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def handler(payload, ctx):
            nonlocal count
            count += 1
            started.set()
            await release.wait()
            return {"developments": []}

        app = create_worker("researcher", "/research", ResearchPayload, handler)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            req = envelope({"topic": "robots", "k": 5})
            headers = {"Authorization": "Bearer research-secret"}
            first = asyncio.create_task(client.post("/research", json=req, headers=headers))
            await started.wait()
            duplicate = await client.post("/research", json=req, headers=headers)
            assert duplicate.status_code == 503
            assert float(duplicate.headers["retry-after"]) > 0
            changed = await client.post(
                "/research", json=req | {"time_budget_ms": 500}, headers=headers
            )
            assert changed.status_code == 409
            release.set()
            assert (await first).status_code == 200
            assert count == 1

    asyncio.run(run())


def test_tiny_deadline_returns_partial_and_caches_it():
    async def run():
        async def handler(payload, ctx):
            await asyncio.sleep(1)
            return {"developments": []}

        app = create_worker("researcher", "/research", ResearchPayload, handler)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            req = envelope({"topic": "robots", "k": 5}) | {"time_budget_ms": 5}
            headers = {"Authorization": "Bearer research-secret"}
            response = await client.post("/research", json=req, headers=headers)
            assert response.status_code == 200
            assert response.json()["status"] == "partial"
            assert response.json()["error"]["code"] == "time_budget_exhausted"
            assert (
                await client.post("/research", json=req, headers=headers)
            ).json() == response.json()

    asyncio.run(run())


@pytest.mark.parametrize("operation,tool", [("model", "model"), ("search", "search_web")])
def test_worker_deadline_records_inflight_provider_call(monkeypatch, operation, tool):
    from common.runtime import Context

    monkeypatch.setenv("MODEL_API_KEY", "offline-fixture-key")
    monkeypatch.setenv("TAVILY_API_KEY", "offline-fixture-key")
    monkeypatch.setenv("MODEL_MIN_INTERVAL_SECONDS", "0")
    events = []
    monkeypatch.setattr("common.runtime.emit", lambda *args, **event: events.append(event))

    async def delayed_provider(request):
        await asyncio.sleep(1)
        pytest.fail("Worker deadline should cancel this fixture first")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(delayed_provider)) as mock:
            monkeypatch.setattr(
                "common.worker.Context",
                lambda service, request, started: Context(
                    service, request, started=started, client=mock
                ),
            )

            async def handler(payload, context):
                if operation == "model":
                    await context.model(
                        [{"role": "user", "content": "fixture"}], max_output_tokens=1
                    )
                else:
                    await context.search("fixture")

            app = create_worker("researcher", "/research", ResearchPayload, handler)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://worker"
            ) as client:
                req = envelope({"topic": "fixture", "k": 5}) | {"time_budget_ms": 20}
                response = await client.post(
                    "/research", json=req, headers={"Authorization": "Bearer research-secret"}
                )
                assert response.json()["status"] == "partial"
                assert response.json()["error"]["code"] == "time_budget_exhausted"
        assert len(events) == 1
        assert events[0]["endpoint_or_tool"] == tool
        assert events[0]["status"] == "time_budget_exhausted"

    asyncio.run(run())


@pytest.mark.parametrize(
    "exception,expected_status,http_status",
    [
        (BudgetStop("fetch_budget_exhausted", "No fetches left"), "partial", 200),
        (ProviderFailure("provider_authentication", "Bad model key"), "failed", 500),
    ],
)
def test_partial_and_terminal_errors_preserve_completed_work(
    exception, expected_status, http_status
):
    async def run():
        async def handler(payload, ctx):
            ctx.partial_result = {
                "verdicts": [{"claim_id": "c1", "verdict": "unreachable", "evidence": None}]
            }
            raise exception

        app = create_worker("verifier", "/verify", VerifyPayload, handler)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            req = envelope(
                {
                    "claims": [
                        {"claim_id": "c1", "text": "claim", "source_url": "https://example.com"}
                    ]
                }
            )
            response = await client.post(
                "/verify", json=req, headers={"Authorization": "Bearer verify-secret"}
            )
            assert response.status_code == http_status
            assert response.json()["status"] == expected_status
            assert response.json()["result"]["verdicts"][0]["claim_id"] == "c1"

    asyncio.run(run())


def test_transient_signal_keeps_same_job_and_final_result():
    async def run():
        count = 0
        release = asyncio.Event()

        async def handler(payload, ctx):
            nonlocal count
            count += 1
            ctx.retry_after = 0.1
            ctx.retry_event.set()
            await release.wait()
            return {"developments": []}

        app = create_worker("researcher", "/research", ResearchPayload, handler)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            req = envelope({"topic": "robots", "k": 5})
            headers = {"Authorization": "Bearer research-secret"}
            assert (await client.post("/research", json=req, headers=headers)).status_code == 503
            release.set()
            await asyncio.sleep(0.05)
            assert (await client.post("/research", json=req, headers=headers)).status_code == 200
            assert count == 1

    asyncio.run(run())


def test_caller_disconnect_does_not_restart_admitted_work():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def handler(payload, ctx):
            nonlocal count
            count += 1
            started.set()
            await release.wait()
            return {"developments": []}

        app = create_worker("researcher", "/research", ResearchPayload, handler)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://worker"
        ) as client:
            req = envelope({"topic": "robots", "k": 5})
            headers = {"Authorization": "Bearer research-secret"}
            abandoned = asyncio.create_task(client.post("/research", json=req, headers=headers))
            await started.wait()
            abandoned.cancel()
            with pytest.raises(asyncio.CancelledError):
                await abandoned
            release.set()
            await asyncio.sleep(0.05)
            replay = await client.post("/research", json=req, headers=headers)
            assert replay.status_code == 200
            assert count == 1

    asyncio.run(run())
