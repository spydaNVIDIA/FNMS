"""Run with: uvicorn researcher.app:app --host 127.0.0.1 --port 8011."""

from common.schemas import ResearchPayload
from common.worker import create_worker

from .agent import research

app = create_worker("researcher", "/research", ResearchPayload, research)
