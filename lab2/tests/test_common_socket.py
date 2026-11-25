"""Real TCP trials of duplicate handling, with an explicitly synthetic handler.

No model or web provider is contacted. These are transport tests, not research
results. They exercise disconnection behavior that an in-process ASGI client can
otherwise conceal.
"""

import asyncio
import socket
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest
import uvicorn

from common.schemas import VerifyPayload
from common.worker import create_worker


@contextmanager
def running_worker():
    state = {"executions": 0}

    async def handler(payload, context):
        state["executions"] += 1
        await asyncio.sleep(0.15)
        return {
            "verdicts": [
                {"claim_id": payload.claims[0].claim_id, "verdict": "unreachable", "evidence": None}
            ]
        }

    app = create_worker("verifier", "/verify", VerifyPayload, handler)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        listener.close()


def envelope():
    return {
        "request_id": str(uuid4()),
        "run_id": str(uuid4()),
        "time_budget_ms": 1000,
        "budget": {"max_steps": 0, "max_fetches": 0, "max_tokens": 0},
        "payload": {
            "claims": [
                {
                    "claim_id": "fixture",
                    "text": "Synthetic network test",
                    "source_url": "http://127.0.0.1/",
                }
            ]
        },
    }


def test_actual_socket_lost_response_replays_one_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("VERIFIER_TOKEN", "socket-test-only")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    with running_worker() as (url, state), httpx.Client(trust_env=False) as client:
        body = envelope()
        headers = {"Authorization": "Bearer socket-test-only"}
        with pytest.raises(httpx.ReadTimeout):
            client.post(url + "/verify", json=body, headers=headers, timeout=0.03)
        duplicate = client.post(url + "/verify", json=body, headers=headers)
        assert duplicate.status_code == 503
        assert "retry-after" in duplicate.headers
        time.sleep(0.2)
        completed = client.post(url + "/verify", json=body, headers=headers)
        replay = client.post(url + "/verify", json=body, headers=headers)
        assert completed.status_code == replay.status_code == 200
        assert completed.json() == replay.json()
        assert state["executions"] == 1
        conflict = client.post(
            url + "/verify", json=body | {"time_budget_ms": 800}, headers=headers
        )
        assert conflict.status_code == 409
