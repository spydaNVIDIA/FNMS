"""ASGI entry point: python -m uvicorn verifier.app:app --port 8012."""

from common.schemas import VerifyPayload
from common.worker import create_worker

from .agent import verify

app = create_worker("verifier", "/verify", VerifyPayload, verify)
