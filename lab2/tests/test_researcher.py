"""Offline research trials use a scripted model; these are not live report artifacts."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from common.runtime import BudgetStop, SourceUnavailable
from common.schemas import Development, Limits, ResearchPayload, Usage
from researcher.agent import (
    ResearchLoop,
    canonical_url,
    development_identity,
    research,
    tool_schemas,
)


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
        self.request = SimpleNamespace(
            budget=Limits(max_steps=18, max_fetches=16, max_tokens=80000),
            time_budget_ms=600000,
        )
        self.usage = Usage()
        self.remaining_seconds = 600.0

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
        "skip_source",
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


def research_ledger(loop):
    content = loop.messages()[1]["content"]
    return json.loads(content.split("RESEARCH_DATA\n", 1)[1].split("\nEND_RESEARCH_DATA", 1)[0])


def test_ledger_exposes_actual_remaining_runtime_budgets_without_changing_them():
    context = ScriptedContext([])
    context.usage = Usage(steps=11, fetches=5, tokens=44000)
    context.remaining_seconds = 210.4
    usage_before = context.usage.model_dump()
    limits_before = context.request.budget.model_dump()
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), context)
    assert research_ledger(loop)["remaining_budget"] == {
        "model_steps": 7,
        "page_fetches": 11,
        "tokens": 36000,
        "seconds": 210.4,
        "elapsed_seconds": 389.6,
    }
    assert context.usage.model_dump() == usage_before
    assert context.request.budget.model_dump() == limits_before


def test_ledger_lists_only_unattempted_candidates_and_keeps_fetched_source_inventory():
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), ScriptedContext([]))
    fetched, rejected, available = [
        f"https://example.org/{name}"
        for name in (
            "fetched",
            "rejected",
            "available",
        )
    ]
    for url in (fetched, rejected, available):
        loop.add_candidate({"url": url})
    loop.sources[fetched] = "Evidence already retrieved."
    loop.rejected[rejected] = "HTTP 404"
    ledger = research_ledger(loop)
    assert [item["url"] for item in ledger["search_results"]] == [available]
    assert ledger["fetched_sources"] == [{"url": fetched, "review": {"status": "pending"}}]
    assert ledger["rejected_urls"] == {rejected: "HTTP 404"}
    assert set(loop.candidates) == {fetched, rejected, available}


def test_independent_fetches_and_findings_can_be_batched_in_one_model_turn():
    urls = [f"https://example.org/project-{number}" for number in range(1, 4)]
    findings = [finding(number, url=url) for number, url in enumerate(urls, start=1)]

    class MultipleSourcesContext(ScriptedContext):
        async def search(self, query, max_results):
            self.search_calls += 1
            return [{"url": url} for url in urls]

    context = MultipleSourcesContext(
        [
            [call("search_web", query="robotics")],
            [call("fetch_article", url=url) for url in urls],
            [call("remember_development", development=item) for item in findings],
            [call("finish", developments=findings)],
        ]
    )
    result = asyncio.run(research(ResearchPayload(topic="robotics", k=3), context))
    assert result == {"developments": findings}
    assert context.partial_result == result
    assert context.fetches == urls
    assert len(context.messages) == 4


def test_remember_and_finish_schemas_require_exactly_one_claim():
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tool_schemas(3)}
    remember_item = schemas["remember_development"]["properties"]["development"]
    finish_item = schemas["finish"]["properties"]["developments"]["items"]
    for item in (remember_item, finish_item):
        assert item["properties"]["claims"]["minItems"] == 1
        assert item["properties"]["claims"]["maxItems"] == 1


@pytest.mark.parametrize("operation", ["remember", "finish"])
def test_multiple_claims_are_rejected_without_corrupting_saved_findings(operation):
    context = ScriptedContext([])
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), context)
    loop.sources[URL] = "real source text"
    loop.remember(finding(1))
    invalid = finding(2)
    invalid["claims"].append(finding(3)["claims"][0])
    with pytest.raises(ValueError, match="exactly one atomic claim"):
        if operation == "remember":
            loop.remember(invalid)
        else:
            loop.finish([finding(1), invalid, finding(3)])
    assert context.partial_result == {"developments": [finding(1)]}


def test_generic_ids_use_titles_and_do_not_merge_unrelated_findings():
    context = ScriptedContext([])
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), context)
    loop.sources[URL] = "real source text"
    first = finding(1)
    first.update(id="dev1", title="OpenVLA 7B checkpoint release")
    second = finding(2)
    second.update(id="dev1", title="Octo 1.5 weights release")
    assert loop.remember(first).id == "openvla-7b-checkpoint-release"
    assert loop.remember(second).id == "octo-1-5-weights-release"
    # The same finding gets the same identity even if the model changes its index.
    first["id"] = "development-2"
    assert loop.remember(first).id == "openvla-7b-checkpoint-release"
    assert len(context.partial_result["developments"]) == 2
    assert development_identity("OpenVLA-7B-Weights", first["title"]) == "openvla-7b-weights"


@pytest.mark.parametrize("title", ["model release " * 35, "机器人模型发布"])
def test_derived_identity_stays_valid_for_long_and_non_latin_titles(title):
    item = finding(1)
    item["title"] = title
    item["id"] = development_identity("dev1", title)
    assert len(item["id"]) <= 160
    assert item["id"] == development_identity("dev99", title)
    assert Development.model_validate(item).id == item["id"]


def test_pending_page_blocks_another_network_turn_until_a_finding_is_saved():
    context = ScriptedContext(
        [
            [call("search_web", query="robotics")],
            [call("fetch_article", url=URL)],
            [call("search_web", query="ignore the pending page"), call("fetch_article", url=URL)],
            [call("remember_development", development=finding(1))],
        ]
    )
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), context)
    with pytest.raises(BudgetStop):
        asyncio.run(loop.run())
    assert context.searches == ["robotics"]
    assert context.fetches == [URL]
    assert {tool["function"]["name"] for tool in context.tools[2]} == {
        "remember_development",
        "skip_source",
        "finish",
    }
    assert loop.pending_sources == []
    assert context.partial_result == {"developments": [finding(1)]}
    assert {"tool": "fetch_article", "outcome": "validation_error"} in loop.recent_actions


def test_skipping_a_page_reopens_research_without_inventing_a_finding():
    context = ScriptedContext(
        [
            [call("search_web", query="robotics")],
            [call("fetch_article", url=URL)],
            [
                call(
                    "skip_source",
                    url=URL,
                    reason="This page is a link collection, not primary evidence.",
                )
            ],
            [call("search_web", query="different primary sources")],
        ]
    )
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), context)
    with pytest.raises(BudgetStop):
        asyncio.run(loop.run())
    assert len(context.searches) == 2
    assert context.partial_result == {"developments": []}
    assert loop.pending_sources == []
    assert loop.source_reviews[URL]["status"] == "skipped"
    assert URL in loop.sources
    with pytest.raises(ValueError, match="actually fetched"):
        loop.remember(finding(1, url="https://example.org/unfetched"))


def test_focus_excerpt_stays_stable_and_actions_are_bounded():
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), ScriptedContext([]))
    loop.sources[URL] = "A" * 9000
    loop.pending_sources = [URL]
    loop.latest_fetched_url = URL
    first = research_ledger(loop)["focus_source"]
    for number in range(15):
        url = f"https://example.org/old-{number}"
        loop.sources[url] = "Old page" * 1000
        loop.record_action("fetch_article", "fetched", url=url)
    ledger = research_ledger(loop)
    assert ledger["focus_source"] == first == {"url": URL, "text": "A" * 8000}
    assert ledger["latest_fetched_url"] == URL
    assert ledger["pending_source_urls"] == [URL]
    assert len(ledger["fetched_sources"]) == 12
    assert all("text" not in item for item in ledger["fetched_sources"])
    assert len(ledger["recent_actions"]) == 4
    assert ledger["recent_actions"][-1]["url"] == "https://example.org/old-14"


def test_skip_only_resolves_a_pending_fetched_url_and_invalid_finish_keeps_it():
    loop = ResearchLoop(ResearchPayload(topic="robotics", k=3), ScriptedContext([]))
    loop.sources[URL] = "Evidence"
    loop.pending_sources = [URL]
    with pytest.raises(ValueError, match="pending fetched"):
        asyncio.run(
            loop.execute(
                "skip_source",
                {
                    "url": "https://example.org/unfetched",
                    "reason": "Not useful",
                },
            )
        )
    with pytest.raises(ValueError, match="exactly 3"):
        loop.finish([finding(1)])
    assert loop.pending_sources == [URL]
    assert loop.context.partial_result == {"developments": []}
