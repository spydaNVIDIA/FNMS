from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from tracker.agent import TrackerAgent
from tracker.config import load_config
from tracker.errors import TrackerError
from tracker.providers import ModelResult
from tracker.types import ArticleRecord, Failure


class MemoryState:
    def __init__(self, empty: bool = False):
        self.state = {
            "seen_urls": ["https://github.com/example/project"],
            "developments": [
                {
                    "key": f"project-v{number}",
                    "title": f"Project v{number}",
                    "summary": f"Previously verified release {number}.",
                    "source_urls": ["https://github.com/example/project"],
                    "last_seen_at": "2026-01-01T00:00:00+00:00",
                }
                for number in range(1, 6)
            ],
            "previous_top_k": [f"project-v{number}" for number in range(1, 6)],
        }
        if empty:
            self.state = {"seen_urls": [], "developments": [], "previous_top_k": []}
        self.saved: list[dict[str, Any]] = []

    def load(self) -> dict[str, Any]:
        return self.state

    def save(self, run: dict[str, Any]) -> int:
        self.saved.append(run)
        return 1


class NoToolProvider:
    def chat(self, messages, tools, max_tokens):
        return ModelResult({"role": "assistant", "content": "thinking"}, 10, 3)


class FailingProvider:
    def __init__(self, failure: Failure):
        self.failure = failure
        self.calls = 0

    def chat(self, messages, tools, max_tokens):
        self.calls += 1
        raise TrackerError(self.failure)


class RecrawlProvider:
    def __init__(self):
        self.index = 0

    def chat(self, messages, tools, max_tokens):
        if self.index == 0:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "fetch-1",
                        "type": "function",
                        "function": {
                            "name": "fetch_article",
                            "arguments": json.dumps(
                                {"url": "https://github.com/example/project?utm_source=test"}
                            ),
                        },
                    }
                ],
            }
        else:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "finish-1",
                        "type": "function",
                        "function": {
                            "name": "finish",
                            "arguments": json.dumps(
                                {
                                    "report": {
                                        "developments": [
                                            {
                                                "key": f"project-v{number}",
                                                "title": f"Project v{number}",
                                                "summary": (
                                                    f"Previously verified release {number} "
                                                    "remains important."
                                                ),
                                                "score": 90 - number,
                                                "source_urls": [
                                                    "https://github.com/example/project"
                                                ],
                                            }
                                            for number in range(1, 6)
                                        ]
                                    }
                                }
                            ),
                        },
                    }
                ],
            }
        self.index += 1
        return ModelResult(message, 20, 8)


class FullRunProvider:
    def __init__(self):
        self.index = 0

    def chat(self, messages, tools, max_tokens):
        if self.index == 0:
            name = "search_web"
            arguments = {"query": "robotics foundation model releases"}
        elif self.index == 1:
            name = "fetch_article"
            arguments = {"url": "https://github.com/example/project"}
        else:
            name = "finish"
            arguments = {
                "report": {
                    "developments": [
                        {
                            "key": f"project-release-{number}",
                            "title": f"Project release {number}",
                            "summary": f"Verified development number {number}.",
                            "score": 100 - number,
                            "source_urls": ["https://github.com/example/project"],
                        }
                        for number in range(1, 6)
                    ]
                }
            }
        self.index += 1
        return ModelResult(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-{self.index}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            },
            30,
            10,
        )


class ContextCapturingProvider:
    def __init__(self):
        self.messages = None

    def chat(self, messages, tools, max_tokens):
        self.messages = messages
        return ModelResult(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "finish",
                        "type": "function",
                        "function": {
                            "name": "finish",
                            "arguments": json.dumps({"report": {"developments": []}}),
                        },
                    }
                ],
            },
            10,
            5,
        )


class FetchFailureThenFinishProvider:
    def __init__(self):
        self.index = 0

    def chat(self, messages, tools, max_tokens):
        if self.index == 0:
            name = "fetch_article"
            arguments = {"url": "https://github.com/example/missing"}
        else:
            name = "finish"
            arguments = {
                "report": {
                    "developments": [
                        {
                            "key": f"project-v{number}",
                            "title": f"Project v{number}",
                            "summary": f"Previously verified release {number}.",
                            "score": 90 - number,
                            "source_urls": ["https://github.com/example/project"],
                        }
                        for number in range(1, 6)
                    ]
                }
            }
        self.index += 1
        return ModelResult(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": str(self.index),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            },
            10,
            5,
        )


def test_budget_exhaustion_writes_a_marked_partial_report(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    limits = replace(config.limits, max_steps=1)
    config = replace(config, limits=limits)
    state = MemoryState()
    result = TrackerAgent(config, NoToolProvider(), state, tmp_path).run()
    assert result["status"] == "partial"
    assert state.saved[0]["status"] == "partial"
    assert "step budget exhausted" in state.saved[0]["report_markdown"]
    assert Path(result["trace_path"]).exists()


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "expected_reason"),
    [
        ("max_steps", 0, "step budget exhausted"),
        ("max_model_calls", 0, "model call budget exhausted"),
        ("max_input_tokens", 0, "input token budget exhausted"),
        ("max_output_tokens", 0, "output token budget exhausted"),
        ("max_cost_usd", 0.0, "cost budget exhausted"),
    ],
)
def test_each_loop_and_model_budget_writes_a_partial_report(
    tmp_path: Path,
    limit_name: str,
    limit_value: int | float,
    expected_reason: str,
) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    config = replace(
        config,
        limits=replace(config.limits, **{limit_name: limit_value}),
    )
    state = MemoryState()
    result = TrackerAgent(config, NoToolProvider(), state, tmp_path).run()
    assert result["status"] == "partial"
    assert state.saved[0]["status"] == "partial"
    assert expected_reason in state.saved[0]["report_markdown"]


@pytest.mark.parametrize(
    ("limit_name", "expected_reason"),
    [
        ("max_searches", "search budget exhausted"),
        ("max_credits", "credit budget exhausted"),
    ],
)
def test_each_search_budget_writes_a_partial_report(
    tmp_path: Path,
    limit_name: str,
    expected_reason: str,
) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    config = replace(config, limits=replace(config.limits, **{limit_name: 0}))
    state = MemoryState(empty=True)
    result = TrackerAgent(config, FullRunProvider(), state, tmp_path).run()
    assert result["status"] == "partial"
    assert state.saved[0]["status"] == "partial"
    assert expected_reason in state.saved[0]["report_markdown"]


def test_fetch_budget_writes_a_partial_report(tmp_path: Path, monkeypatch) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    config = replace(config, limits=replace(config.limits, max_fetches=0))
    state = MemoryState(empty=True)
    monkeypatch.setattr(
        "tracker.agent.search_web",
        lambda query, config: [
            {"title": "Release", "url": "https://github.com/example/project"}
        ],
    )
    result = TrackerAgent(config, FullRunProvider(), state, tmp_path).run()
    assert result["status"] == "partial"
    assert state.saved[0]["status"] == "partial"
    assert "fetch budget exhausted" in state.saved[0]["report_markdown"]
    assert not state.saved[0]["articles"]


def test_network_cut_retries_to_cap_then_saves_a_failed_run(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    provider = FailingProvider(Failure("network", True, "network is offline"))
    state = MemoryState()
    result = TrackerAgent(
        config,
        provider,
        state,
        tmp_path,
        sleeper=lambda _: None,
    ).run()
    assert result["status"] == "failed"
    assert provider.calls == config.limits.max_retries + 1
    assert state.saved[0]["status"] == "failed"
    assert [event["error_class"] for event in state.saved[0]["trace"]] == [
        "network"
    ] * provider.calls


def test_bad_key_stops_without_retry_and_saves_a_failed_run(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    provider = FailingProvider(Failure("authentication", False, "invalid API key"))
    state = MemoryState()
    result = TrackerAgent(config, provider, state, tmp_path).run()
    assert result["status"] == "failed"
    assert provider.calls == 1
    assert state.saved[0]["status"] == "failed"
    assert state.saved[0]["trace"][0]["error_class"] == "authentication"


def test_recrawl_skips_seen_url_and_preserves_still_bucket(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    state = MemoryState()
    result = TrackerAgent(config, RecrawlProvider(), state, tmp_path).run()
    assert result["status"] == "complete"
    saved = state.saved[0]
    assert saved["articles"][0]["status"] == "skipped"
    assert len([item for item in saved["developments"] if item["rank"] is not None]) == 5
    assert all(item["bucket"] == "still" for item in saved["developments"])
    assert "## Still in top K" in saved["report_markdown"]


def test_frontend_never_uses_inner_html() -> None:
    frontend = Path(__file__).parents[1] / "frontend"
    source = "\n".join(path.read_text(encoding="utf-8") for path in frontend.glob("*.js"))
    assert ".innerHTML" not in source


def test_frontend_exposes_authenticated_live_run_control() -> None:
    frontend = Path(__file__).parents[1] / "frontend"
    html = (frontend / "tracker.html").read_text(encoding="utf-8")
    script = (frontend / "tracker.js").read_text(encoding="utf-8")
    assert 'id="runNow"' in html
    assert 'api("/api/tracker/run-now", { method: "POST", auth: true })' in script
    assert "24 hours" in script
    assert "sessionStorage.getItem(RUN_COMPLETION_KEY)" in script
    assert "sessionStorage.setItem(RUN_COMPLETION_KEY, completionMarker)" in script
    assert "window.location.reload()" not in script
    assert "formatDuration(run.duration_seconds)" in script
    assert "formatDuration(report.run.duration_seconds)" in script
    assert "`/api/tracker/runs/${runId}/report`" in script
    assert "renderRun(runSelect.value" in script


def test_clean_first_run_produces_ranked_top_five(tmp_path: Path, monkeypatch) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    state = MemoryState(empty=True)
    monkeypatch.setattr(
        "tracker.agent.search_web",
        lambda query, config: [
            {"title": "Release", "url": "https://github.com/example/project"}
        ],
    )
    monkeypatch.setattr(
        "tracker.agent.fetch_article",
        lambda url, config: ArticleRecord(
            title="Release",
            url=url,
            canonical_url="https://github.com/example/project",
            fetched_at="2026-10-12T00:00:00+00:00",
            status="fetched",
            content_hash="a" * 64,
            text="BEGIN_UNTRUSTED_WEB_CONTENT\nRelease evidence\nEND_UNTRUSTED_WEB_CONTENT",
        ),
    )
    result = TrackerAgent(config, FullRunProvider(), state, tmp_path).run()
    saved = state.saved[0]
    assert result["status"] == "complete"
    assert len([item for item in saved["developments"] if item["rank"] is not None]) == 5
    assert [item["rank"] for item in saved["developments"]] == [1, 2, 3, 4, 5]
    assert all(item["source_urls"] for item in saved["developments"])
    assert {event["tool"] for event in saved["trace"]} >= {
        "search_web",
        "fetch_article",
        "finish",
    }


def test_model_context_compacts_historical_tool_output(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    provider = ContextCapturingProvider()
    agent = TrackerAgent(config, provider, MemoryState(empty=True), tmp_path)
    historical_marker = "x" * 50000
    agent._call_model(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "research"},
            {"role": "tool", "content": historical_marker},
        ]
    )
    serialized = json.dumps(provider.messages)
    assert historical_marker not in serialized
    assert len(serialized) < 10000


def test_search_tool_is_removed_after_planning_cap(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    agent = TrackerAgent(config, ContextCapturingProvider(), MemoryState(empty=True), tmp_path)
    agent.budget.usage.searches = int(config.search["planning_searches"])
    agent.search_results.extend(
        {
            "title": f"Project {index}",
            "url": f"https://github.com/example/project-{index}",
        }
        for index in range(5)
    )
    schemas = agent._available_tool_schemas()
    names = {schema["function"]["name"] for schema in schemas}
    assert "search_web" not in names
    assert names == {"fetch_article"}
    fetch_schema = next(
        schema for schema in schemas if schema["function"]["name"] == "fetch_article"
    )
    assert len(
        fetch_schema["function"]["parameters"]["properties"]["url"]["enum"]
    ) == 5


def test_fetch_tool_is_hidden_until_search_discovers_a_url(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    agent = TrackerAgent(config, ContextCapturingProvider(), MemoryState(empty=True), tmp_path)
    names = {schema["function"]["name"] for schema in agent._available_tool_schemas()}
    assert names == {"search_web"}


def test_finish_schema_requires_fixed_k_after_enough_fetches(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    agent = TrackerAgent(config, ContextCapturingProvider(), MemoryState(empty=True), tmp_path)
    for index in range(config.top_k):
        agent.articles.append(
            ArticleRecord(
                title=f"Source {index}",
                url=f"https://github.com/example/source-{index}",
                canonical_url=f"https://github.com/example/source-{index}",
                fetched_at="2026-10-12T00:00:00+00:00",
                status="fetched",
                text="evidence",
            )
        )
    schemas = agent._available_tool_schemas()
    assert [schema["function"]["name"] for schema in schemas] == ["finish"]
    developments = schemas[0]["function"]["parameters"]["properties"]["report"][
        "properties"
    ]["developments"]
    assert developments["minItems"] == config.top_k
    assert developments["maxItems"] == config.top_k


def test_fetch_http_failure_rejects_source_without_stopping_run(
    tmp_path: Path, monkeypatch
) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    state = MemoryState()
    monkeypatch.setattr(
        "tracker.agent.fetch_article",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            TrackerError(Failure("terminal_http", False, "HTTP 404"))
        ),
    )
    agent = TrackerAgent(config, FetchFailureThenFinishProvider(), state, tmp_path)
    agent.search_results.append(
        {"title": "Missing", "url": "https://github.com/example/missing"}
    )
    result = agent.run()
    assert result["status"] == "complete"
    assert state.saved[0]["articles"][0]["status"] == "rejected"
    assert state.saved[0]["articles"][0]["rejection_reason"] == "HTTP 404"


def test_fetch_must_use_a_discovered_url(tmp_path: Path) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    agent = TrackerAgent(config, ContextCapturingProvider(), MemoryState(empty=True), tmp_path)
    with pytest.raises(TrackerError, match="not returned"):
        agent._tool_result(
            "fetch_article", {"url": "https://github.com/example/invented"}, {}
        )
    assert agent.articles[0].status == "rejected"


def test_redirect_aliases_collapse_to_one_canonical_article(
    tmp_path: Path, monkeypatch
) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    agent = TrackerAgent(config, ContextCapturingProvider(), MemoryState(empty=True), tmp_path)
    first = "https://github.com/example/project"
    alias = "https://github.com/example/project-alias"
    agent.search_results.extend(
        [{"title": "First", "url": first}, {"title": "Alias", "url": alias}]
    )

    def redirected(url, config):
        return ArticleRecord(
            title="Project",
            url=url,
            canonical_url=first,
            fetched_at="2026-10-12T00:00:00+00:00",
            status="fetched",
            text="evidence",
        )

    monkeypatch.setattr("tracker.agent.fetch_article", redirected)
    agent._tool_result("fetch_article", {"url": first}, {})
    result, _ = agent._tool_result("fetch_article", {"url": alias}, {})
    assert result["status"] == "skipped"
    assert len(agent.articles) == 1


def test_redirected_request_url_is_not_fetched_twice(tmp_path: Path, monkeypatch) -> None:
    config = load_config(Path(__file__).parents[1] / "config.yaml")
    agent = TrackerAgent(config, ContextCapturingProvider(), MemoryState(empty=True), tmp_path)
    requested = "https://github.com/example/project"
    agent.search_results.append({"title": "Project", "url": requested})

    def redirected(url, config):
        return ArticleRecord(
            title="Project",
            url=url,
            canonical_url=f"{requested}/",
            fetched_at="2026-10-12T00:00:00+00:00",
            status="fetched",
            text="evidence",
        )

    monkeypatch.setattr("tracker.agent.fetch_article", redirected)
    agent._tool_result("fetch_article", {"url": requested}, {})
    result, _ = agent._tool_result("fetch_article", {"url": requested}, {})
    assert result["status"] == "skipped"
    assert agent.budget.usage.fetches == 1
