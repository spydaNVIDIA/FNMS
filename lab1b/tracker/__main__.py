"""Run the tracker with `python -m tracker run`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from .agent import TrackerAgent
from .config import load_config
from .errors import TrackerError
from .providers import OpenAICompatibleProvider
from .state import APIStateClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Open-source robotics model tracker")
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--config", help="Path to config.yaml")
    args = parser.parse_args()
    config = load_config(args.config)
    root = config.path.parent
    load_dotenv(root / ".env")
    provider = None
    state_client = None
    try:
        provider = OpenAICompatibleProvider(config)
        state_client = APIStateClient()
        result = TrackerAgent(config, provider, state_client, Path(root)).run()
    except TrackerError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_class": exc.failure.category,
                    "message": exc.failure.message,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    finally:
        if provider is not None:
            provider.close()
        if state_client is not None:
            state_client.close()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
