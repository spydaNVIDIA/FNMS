from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tracker.report import prepare_developments
from tracker.artifacts import write_artifacts


ROOT = Path(__file__).parents[1]


def test_submission_files_and_policy_are_present() -> None:
    required = [
        ROOT / "AGENT.md",
        ROOT / "README.md",
        ROOT / "config.yaml",
        ROOT / ".env.example",
        ROOT / "tracker" / "agent.py",
        ROOT / "tracker" / "tools.py",
        ROOT / "frontend" / "tracker.html",
    ]
    assert all(path.is_file() for path in required)


def test_no_provider_secret_is_committed() -> None:
    tavily_pattern = re.compile(re.escape("tvly" + "-") + r"[A-Za-z0-9_-]{20,}")
    groq_pattern = re.compile(re.escape("gsk" + "_") + r"[A-Za-z0-9]{20,}")
    assert "/.env" in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in {".venv", ".git", "__pycache__"} for part in path.parts):
            continue
        if path == ROOT / ".env":
            continue
        if path.name == ".env.example":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert tavily_pattern.search(text) is None
        assert groq_pattern.search(text) is None


def test_provenance_rejects_an_unfetched_citation() -> None:
    raw = [
        {
            "key": "unsupported-claim",
            "title": "Unsupported claim",
            "summary": "This claim has no fetched evidence.",
            "score": 90,
            "source_urls": ["https://github.com/example/not-fetched"],
        }
    ]
    with pytest.raises(ValueError, match="unfetched source"):
        prepare_developments(raw, {}, set(), 5)


def test_new_url_matches_saved_development_and_change_buckets_are_complete() -> None:
    state = {
        "developments": [
            {
                "key": "project-v1-release",
                "title": "Project V1 release",
                "summary": "Earlier evidence.",
                "source_urls": ["https://github.com/example/original"],
            },
            {
                "key": "older-project-release",
                "title": "Older project release",
                "summary": "Earlier evidence.",
                "source_urls": ["https://github.com/example/older"],
            },
        ],
        "previous_top_k": ["project-v1-release", "older-project-release"],
    }
    new_url = "https://arxiv.org/abs/2601.00001"
    another_url = "https://github.com/example/new-project"
    raw = [
        {
            "key": "model-generated-different-key",
            "title": "Project V1 release",
            "summary": "The same release is supported by a newly discovered source.",
            "score": 95,
            "source_urls": [new_url],
        },
        {
            "key": "new-project-release",
            "title": "New project release",
            "summary": "A newly discovered development.",
            "score": 90,
            "source_urls": [another_url],
        },
    ]
    result = prepare_developments(raw, state, {new_url, another_url}, top_k=2)
    ranked = [item for item in result if item.rank is not None]
    dropped = [item for item in result if item.bucket == "dropped"]
    assert ranked[0].key == "project-v1-release"
    assert ranked[0].bucket == "still"
    assert ranked[0].source_urls == [new_url]
    assert ranked[1].bucket == "new"
    assert [item.key for item in dropped] == ["older-project-release"]


def test_failed_artifact_does_not_consume_run1_slot(tmp_path: Path) -> None:
    started = datetime(2026, 10, 12, tzinfo=UTC)
    failed_report, _ = write_artifacts(tmp_path, started, "failed", "failed", [])
    first_report, _ = write_artifacts(
        tmp_path, started + timedelta(minutes=1), "complete", "complete", []
    )
    assert failed_report.name.endswith("-failed.md")
    assert first_report.name == "run1.md"
