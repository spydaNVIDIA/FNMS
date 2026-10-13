"""Test environment must be configured before importing the FastAPI app."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_database = tempfile.NamedTemporaryFile(prefix="fnms-tracker-test-", suffix=".db", delete=False)
_database.close()
os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{_database.name}"
os.environ["JWT_SECRET"] = "test-only-secret"
