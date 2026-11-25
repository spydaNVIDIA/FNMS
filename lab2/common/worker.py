"""Authenticated, idempotent worker HTTP boundary.

One process owns one registry. Each admitted job lives independently of its caller's
connection. A retry observes that job or replays its immutable final response.
"""

import asyncio
import hashlib
import hmac
import json
import math
import os
import time
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .errors import BudgetStop, ProviderFailure
from .runtime import Context
from .schemas import Error, ResearchResult, VerifyResult, WorkerRequest, WorkerResponse
from .tracing import emit


@dataclass
class Job:
    fingerprint: str
    context: Context
    task: asyncio.Task
    receipts: int = 1


def create_worker(service, path, payload_type, handler):
    app = FastAPI(title=f"Lab 2 {service}", docs_url=None, redoc_url=None, openapi_url=None)
    registry: dict[str, Job] = {}
    lock = asyncio.Lock()
    app.state.registry = registry

    def rejection(code, message, http_status):
        return JSONResponse(
            {"error": {"code": code, "message": message, "retryable": False}},
            status_code=http_status,
        )

    def busy(job, message):
        context = job.context
        envelope = context.request
        body = WorkerResponse(
            request_id=envelope.request_id,
            run_id=envelope.run_id,
            status="failed",
            usage=context.usage,
            result=context.partial_result,
            error=Error(code="request_in_progress", message=message, retryable=True),
        )
        return JSONResponse(
            body.model_dump(mode="json"),
            status_code=503,
            # With a small caller attempt cap, frequent polling could exhaust all
            # retries before this original task completes. Its deadline is a safe
            # upper bound for when a final cached answer will be available.
            headers={"Retry-After": str(max(1, math.ceil(context.remaining_seconds)))},
        )

    @app.middleware("http")
    async def authenticate_and_trace(request: Request, call_next):
        request.state.started = time.monotonic()
        request.state.ids = {}
        request.state.attempt = 1
        response = None
        try:
            if not (request.method == "GET" and request.url.path == "/healthz"):
                expected = os.getenv(f"{service.upper()}_TOKEN", "")
                supplied = request.headers.get("authorization", "")
                if not expected or not hmac.compare_digest(
                    supplied.encode("utf-8"), f"Bearer {expected}".encode("utf-8")
                ):
                    response = JSONResponse(
                        {
                            "error": {
                                "code": "authentication",
                                "message": "Not authenticated",
                                "retryable": False,
                            }
                        },
                        status_code=401,
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    return response
            response = await call_next(request)
            return response
        finally:
            emit(
                service,
                **request.state.ids,
                direction="in",
                endpoint_or_tool=request.url.path,
                status=str(response.status_code) if response is not None else "disconnected",
                latency_ms=(time.monotonic() - request.state.started) * 1000,
                attempt=request.state.attempt,
            )

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    async def execute(envelope, payload, context):
        status, error, http_status = "complete", None, 200
        result = context.partial_result
        try:
            async with asyncio.timeout(context.remaining_seconds):
                result = await handler(payload, context)
            result_type = ResearchResult if service == "researcher" else VerifyResult
            result = result_type.model_validate(result).model_dump(mode="json")
        except (BudgetStop, TimeoutError) as exc:
            status = "partial"
            code = exc.code if isinstance(exc, BudgetStop) else "time_budget_exhausted"
            message = (
                exc.message
                if isinstance(exc, BudgetStop)
                else "Worker elapsed-time budget exhausted"
            )
            error = Error(code=code, message=message, retryable=False)
            result = context.partial_result
        except ProviderFailure as exc:
            # Runtime retries transient provider calls within this same task/deadline.
            # An escaped failure is terminal: never restart the handler from scratch.
            status, http_status = "failed", 500
            error = Error(code=exc.code, message=exc.message, retryable=False)
            result = context.partial_result
        except Exception:
            status, http_status = "failed", 500
            error = Error(
                code="worker_error",
                message="Worker could not complete this request",
                retryable=False,
            )
            result = context.partial_result
        finally:
            await context.close()
        body = WorkerResponse(
            request_id=envelope.request_id,
            run_id=envelope.run_id,
            status=status,
            usage=context.usage,
            result=result,
            error=error,
        ).model_dump(mode="json")
        return http_status, body

    @app.post(path)
    async def process(request: Request):
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 1000000:
                    return rejection("invalid_schema", "Request body is too large", 400)
                body.extend(chunk)
            raw = json.loads(body)
            envelope = WorkerRequest.model_validate(raw)
            payload = payload_type.model_validate(envelope.payload)
        except (ValueError, TypeError, ValidationError):
            return rejection("invalid_schema", "Invalid request schema", 400)
        request.state.ids = {"run_id": envelope.run_id, "request_id": envelope.request_id}
        canonical = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
        key = str(envelope.request_id)
        async with lock:
            job = registry.get(key)
            if job is not None:
                job.receipts += 1
                request.state.attempt = job.receipts
                if job.fingerprint != fingerprint:
                    return rejection(
                        "idempotency_conflict", "request_id reused with a different body", 409
                    )
                if job.task.done():
                    code, cached = job.task.result()
                    return JSONResponse(cached, status_code=code)
                return busy(job, "Request is still being processed")
            context = Context(service, envelope, started=request.state.started)
            context.partial_result = (
                {"developments": []} if service == "researcher" else {"verdicts": []}
            )
            task = asyncio.create_task(execute(envelope, payload, context))
            job = Job(fingerprint, context, task)
            registry[key] = job
        # A model 429 can notify the caller while the original job sleeps/resumes.
        # Holding the task in registry keeps it alive if this HTTP request disconnects.
        transient = asyncio.create_task(context.retry_event.wait())
        try:
            done, _ = await asyncio.wait({job.task, transient}, return_when=asyncio.FIRST_COMPLETED)
            if job.task in done:
                code, result = job.task.result()
                return JSONResponse(result, status_code=code)
            return busy(job, "Provider temporarily unavailable; work remains in progress")
        finally:
            transient.cancel()

    return app
