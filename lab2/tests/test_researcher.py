"""Offline research trials use a scripted model; these are not live report artifacts."""

import asyncio
import json

import pytest

from common.runtime import BudgetStop, SourceUnavailable
from common.schemas import ResearchPayload
from researcher.agent import ResearchLoop, canonical_url, research, tool_schemas


URL = "https://github.com/example/project"


def finding(number=1, *, url=URL):
    return {
        "id": f"project-release-{number}",
        "title": f"Project release {number}",
        "summary": f"The project published release {number}.",
        "claims": [
            {
                "claim_id": f"claim-{number}",
                "text": f"The project published release {number}.",
                "source_url": url,
            }
        ],
    }


def call(name, **arguments):
    return {"function": {"name": name, "arguments": json.dumps(arguments)}}


class ScriptedContext:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.partial_result = {}
        self.messages = []
        self.fetches = []
        self.searches = []
        self.search_calls = 0
        self.tools = []
        self.traces = []

    def check(self):
        pass

    def trace(self, tool, status, started):
        self.traces.append((tool, status))

    async def model(self, messages, **kwargs):
        self.messages.append(messages)
        self.tools.append(kwargs.get("tools", []))
        try:
            return {"tool_calls": next(self.turns)}
        except StopIteration:
            raise BudgetStop("step_budget_exhausted", "No more model calls") from None

    async def search(self, query, max_results):
        self.searches.append(query)
        self.search_calls += 1
        return [{"title": "Primary project", "url": URL, "snippet": "Project releases"}]

    async def fetch(self, url):
        self.fetches.append(url)
        return "Project published releases 1, 2, and 3. Each includes publicly available weights."


def test_real_loop_consumes_search_fetch_and_finish_results():
    context = ScriptedContext(
        [
            [call("search_web", query="robotics release code")],
            [call("fetch_article", url=URL)],
            [call("remember_development", development=finding(1))],
            [call("finish", developments=[finding(i) for i in range(1, 4)])],
        ]
    )
    result = asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
    assert len(result["developments"]) == 3
    assert context.searches == ["robotics release code"]
    assert context.fetches == [URL]
    assert context.partial_result == result


def test_budget_exhaustion_preserves_already_remembered_findings():
    context = ScriptedContext(
        [
            [call("search_web", query="robotics")],
            [call("fetch_article", url=URL)],
            [call("remember_development", development=finding(1))],
        ]
    )
    with pytest.raises(BudgetStop):
        asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
    assert context.partial_result == {"developments": [finding(1)]}


def test_unfetched_citation_is_rejected_then_model_can_repair_it():
    context = ScriptedContext(
        [
            [call("search_web", query="robotics")],
            [call("fetch_article", url=URL)],
            [
                call(
                    "finish",
                    developments=[
                        finding(i, url="https://example.org/made-up") for i in range(1, 4)
                    ],
                )
            ],
            [call("finish", developments=[finding(i) for i in range(1, 4)])],
        ]
    )
    result = asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
    assert len(result["developments"]) == 3
    assert "actually fetched" in context.messages[-1][1]["content"]


def test_undiscovered_url_is_not_fetched():
    context = ScriptedContext([[call("fetch_article", url="https://example.org/invented")]])
    with pytest.raises(BudgetStop):
        asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
    assert context.fetches == []


def test_repeated_fetch_uses_request_local_memory():
    context = ScriptedContext(
        [
            [call("search_web", query="robotics")],
            [call("fetch_article", url=URL), call("fetch_article", url=URL + "?utm_source=test")],
        ]
    )
    with pytest.raises(BudgetStop):
        asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
    assert context.fetches == [URL]


def test_finish_checks_distinct_findings_after_normalization_and_preserves_partial():
    context = ScriptedContext([])
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), context)
    loop.sources[URL] = "real source text"
    loop.remember(finding(1))
    duplicate = finding(2)
    duplicate["id"] = "PROJECT RELEASE 1"
    with pytest.raises(ValueError, match="distinct"):
        loop.finish([finding(1), duplicate, finding(3)])
    assert context.partial_result == {"developments": [finding(1)]}


def test_same_claim_with_new_title_is_not_another_development():
    context = ScriptedContext([])
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), context)
    loop.sources[URL] = "real source text"
    loop.remember(finding(1))
    duplicate = finding(2)
    duplicate["claims"][0]["text"] = finding(1)["claims"][0]["text"]
    with pytest.raises(ValueError, match="claim already"):
        loop.remember(duplicate)


def test_finish_rejects_duplicate_claim_ids():
    context = ScriptedContext([])
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), context)
    loop.sources[URL] = "real source text"
    second = finding(2)
    second["claims"][0]["claim_id"] = "claim-1"
    with pytest.raises(ValueError, match="Claim IDs"):
        loop.finish([finding(1), second, finding(3)])


def test_fetch_count_does_not_force_finish():
    tools = tool_schemas(3)
    assert {item["function"]["name"] for item in tools} == {
        "search_web",
        "fetch_article",
        "remember_development",
        "finish",
    }
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), ScriptedContext([]))
    loop.sources.update({f"https://example.org/{i}": "unhelpful page" for i in range(10)})
    assert loop.context.partial_result == {"developments": []}


def test_source_failure_does_not_masquerade_as_evidence():
    class UnreachableContext(ScriptedContext):
        async def fetch(self, url):
            raise SourceUnavailable("HTTP 404")

    context = UnreachableContext(
        [
            [call("search_web", query="robotics")],
            [call("fetch_article", url=URL)],
            [call("remember_development", development=finding(1))],
        ]
    )
    with pytest.raises(BudgetStop):
        asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
    assert context.partial_result == {"developments": []}


def test_raw_readme_alternative_is_derived_from_discovered_repo():
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), ScriptedContext([]))
    loop.add_candidate({"url": URL})
    assert "https://raw.githubusercontent.com/example/project/HEAD/README.md" in loop.candidates


def test_normalization_does_not_remove_meaningful_query_or_ipv6_brackets():
    assert (
        canonical_url("https://EXAMPLE.org/p?v=2&utm_source=x#heading")
        == "https://example.org/p?v=2"
    )
    assert canonical_url("https://[2606:4700::1111]:443/") == "https://[2606:4700::1111]/"


def test_search_cap_hides_tool_and_rejects_extra_call_without_spending(monkeypatch):
    monkeypatch.setenv("SEARCH_MAX_CALLS", "3")
    context = ScriptedContext(
        [
            [
                call("search_web", query="first"),
                call("search_web", query="second"),
                call("search_web", query="third"),
            ],
            [call("search_web", query="forbidden fourth")],
            [call("fetch_article", url=URL)],
            [call("remember_development", development=finding(1))],
            [call("finish", developments=[finding(i) for i in range(1, 4)])],
        ]
    )
    result = asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
    assert len(result["developments"]) == 3
    assert context.searches == ["first", "second", "third"]
    assert context.search_calls == 3
    for tools in context.tools[1:]:
        assert "search_web" not in {tool["function"]["name"] for tool in tools}
    assert '"remaining": 0' in context.messages[1][1]["content"]
    assert "Search allowance exhausted" in context.messages[2][1]["content"]
    assert ("tool/search_web", "error") in context.traces


def test_search_retries_using_last_allowance_still_allow_existing_evidence(monkeypatch):
    monkeypatch.setenv("SEARCH_MAX_CALLS", "3")

    class RetriedSearchContext(ScriptedContext):
        async def search(self, query, max_results):
            if self.search_calls:
                self.searches.append(query)
                self.search_calls = 3  # Two transient attempts used the remaining slots.
                raise BudgetStop("search_budget_exhausted", "Search allowance exhausted")
            return await super().search(query, max_results)

    context = RetriedSearchContext(
        [
            [call("search_web", query="first")],
            [call("search_web", query="transient retries")],
            [call("search_web", query="forbidden retry"), call("fetch_article", url=URL)],
            [call("finish", developments=[finding(i) for i in range(1, 4)])],
        ]
    )
    result = asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
    assert len(result["developments"]) == 3
    assert context.searches == ["first", "transient retries"]
    assert context.search_calls == 3
    assert context.fetches == [URL]
    assert "exhausted by attempts" in context.messages[2][1]["content"]


def test_search_time_budget_still_stops_the_request(monkeypatch):
    monkeypatch.setenv("SEARCH_MAX_CALLS", "3")

    class TimedOutSearchContext(ScriptedContext):
        async def search(self, query, max_results):
            raise BudgetStop("time_budget_exhausted", "Worker deadline")

    context = TimedOutSearchContext([[call("search_web", query="robotics")]])
    with pytest.raises(BudgetStop, match="Worker deadline"):
        asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
