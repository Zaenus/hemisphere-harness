"""Looking claims up: the search services, and the second pass of the answer check."""

import httpx
import pytest

from hemisphere_harness import (
    Actor,
    ChoiceAnswer,
    Claim,
    ClaimVerdict,
    ConfigError,
    Harness,
    MockLeftHemisphere,
    NoulAnswer,
    Option,
    Plan,
    Proposal,
    Reintegration,
    ScriptedRightHemisphere,
    Settings,
    Stakes,
)
from hemisphere_harness.lookup import BraveSearcher, Searcher, SearxngSearcher, WikipediaSearcher, searcher_from_settings
from hemisphere_harness.right.prompts import revise_prompt
from hemisphere_harness.schemas import Evidence


async def test_wikipedia_returns_the_matching_passage_and_the_pages_opening():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.params.get("list") == "search":
            return httpx.Response(200, json={"query": {"search": [
                {"title": "Bitcoin", "snippet": "a <span class=\"searchmatch\">decentralized</span>   currency"},
                {"title": "Proof of work", "snippet": ""},
            ]}})  # fmt: skip
        return httpx.Response(200, json={"query": {"pages": [
            {"title": "Proof of work", "fullurl": "https://de.wikipedia.org/wiki/Proof_of_work", "extract": "Second."},
            {"title": "Bitcoin", "fullurl": "https://de.wikipedia.org/wiki/Bitcoin", "extract": "Bitcoin is a cryptocurrency."},
        ]}})  # fmt: skip

    searcher = WikipediaSearcher("de", transport=httpx.MockTransport(handler))
    found = await searcher.search("bitcoin decentralized")
    await searcher.aclose()

    assert [e.title for e in found] == ["Bitcoin", "Proof of work"]  # in the search's order
    assert found[0].snippet == "…a decentralized currency… Bitcoin is a cryptocurrency."  # markup removed
    assert found[1].snippet == "Second."
    assert seen[0].url.host == "de.wikipedia.org" and seen[0].url.params["srsearch"] == "bitcoin decentralized"
    assert seen[1].url.params["titles"] == "Bitcoin|Proof of work"
    assert "hemisphere-harness" in seen[0].headers["user-agent"]


async def test_searxng_and_brave_are_parsed_and_brave_gets_its_key():
    def searx(request):
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [{"title": "T", "url": "https://t", "content": "c" * 2000}]})

    def brave(request):
        assert request.headers["x-subscription-token"] == "k"
        return httpx.Response(200, json={"web": {"results": [{"title": "B", "url": "https://b", "description": "d"}]}})

    s = SearxngSearcher("http://localhost:8888/", transport=httpx.MockTransport(searx))
    b = BraveSearcher("k", transport=httpx.MockTransport(brave))
    found_s, found_b = await s.search("q"), await b.search("q")
    await s.aclose()
    await b.aclose()
    assert found_s[0].title == "T" and len(found_s[0].snippet) == 700
    assert found_b == [Evidence(title="B", url="https://b", snippet="d")]


async def test_a_wikipedia_query_that_finds_nothing_is_retried_with_fewer_words():
    queries = []

    def handler(request):
        if request.url.params.get("list") == "search":
            queries.append(request.url.params["srsearch"])
            hits = [{"title": "Mining pool", "snippet": "pools &amp; &quot;hash rate&quot;"}] if len(queries) == 3 else []
            return httpx.Response(200, json={"query": {"search": hits}})
        return httpx.Response(200, json={"query": {"pages": [{"title": "Mining pool", "fullurl": "u", "extract": "x"}]}})

    searcher = WikipediaSearcher(transport=httpx.MockTransport(handler))
    found = await searcher.search("bitcoin mining pool hashrate concentration")
    await searcher.aclose()
    assert queries == ["bitcoin mining pool hashrate concentration", "bitcoin mining pool hashrate", "bitcoin mining pool"]
    assert found[0].snippet == '…pools & "hash rate"… x'


async def test_a_failed_search_finds_nothing_instead_of_failing():
    searcher = WikipediaSearcher(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert await searcher.search("anything") == []
    await searcher.aclose()


def test_the_settings_choose_the_searcher(clean_env):
    assert searcher_from_settings(Settings(_env_file=None)) is None
    assert isinstance(searcher_from_settings(Settings(_env_file=None, harness_lookup="wikipedia")), WikipediaSearcher)
    with pytest.raises(ConfigError, match="HARNESS_LOOKUP_URL"):
        searcher_from_settings(Settings(_env_file=None, harness_lookup="searxng"))
    with pytest.raises(ConfigError, match="BRAVE_API_KEY"):
        searcher_from_settings(Settings(_env_file=None, harness_lookup="brave"))
    clean_env.setenv("BRAVE_API_KEY", "k")
    assert isinstance(searcher_from_settings(Settings(_env_file=None, harness_lookup="brave")), BraveSearcher)
    with pytest.raises(ConfigError, match="not known"):
        searcher_from_settings(Settings(_env_file=None, harness_lookup="google"))


class FakeSearcher(Searcher):
    name = "fake"

    def __init__(self, found):
        super().__init__()
        self.found = found
        self.queries = []

    async def _search(self, query, limit):
        self.queries.append(query)
        return self.found.get(query, [])


SOURCE = Evidence(title="Mining pools", url="https://en.wikipedia.org/wiki/Mining_pool", snippet="A few pools mine most blocks.")


async def test_unsure_claims_are_looked_up_and_judged_again_with_the_evidence():
    right = ScriptedRightHemisphere(
        plan=Plan(situation="s"),
        proposals=[Proposal(options=[Option(id="a", description="answer"), Option(id="b", description="ask")], rationale="r", stakes=Stakes.LOW, open_ended=False)],
        reintegrations=[Reintegration(situation="r", goal_met=True)],
        report="Draft.", claims=[Claim(text="Bitcoin has no central issuer."), Claim(text="A few mining pools mine most blocks.", query="bitcoin mining pool share")],
    )  # fmt: skip
    left = MockLeftHemisphere(script=[
        {"select": ChoiceAnswer.from_probabilities({"a": 0.95, "b": 0.05})},
        {"claim:c1": NoulAnswer(probability=0.95), "claim:c2": NoulAnswer(probability=0.45)},
        {"claim:c2": NoulAnswer(probability=0.92)},
    ])  # fmt: skip
    searcher = FakeSearcher({"bitcoin mining pool share": [SOURCE]})
    result = await Harness(right, left, searcher=searcher).run("Is Bitcoin decentralized?")

    assert searcher.queries == ["bitcoin mining pool share"]  # only the unsure claim, by its query
    second_focus, second_questions = left.calls[-1]
    assert set(second_questions) == {"claim:c2"}
    assert second_focus["evidence"]["c2"][0]["text"] == "A few pools mine most blocks."
    assert "evidence found for it" in second_questions["claim:c2"].instructions

    c2 = result.check.checks[1]
    assert (c2.verdict, c2.first_probability, c2.probability, c2.sources) == (ClaimVerdict.HOLDS, 0.45, 0.92, [SOURCE])
    assert result.check.revised is False  # with the evidence, everything holds
    lookup = next(e for e in result.trace if e.phase == "lookup")
    assert lookup.actor is Actor.HARNESS and lookup.data["results"][0]["sources"][0]["title"] == "Mining pools"


async def test_without_evidence_the_first_verdict_stands_and_the_right_sees_the_sources():
    right = ScriptedRightHemisphere(
        plan=Plan(situation="s"),
        proposals=[Proposal(options=[Option(id="a", description="answer"), Option(id="b", description="ask")], rationale="r", stakes=Stakes.LOW, open_ended=False)],
        reintegrations=[Reintegration(situation="r", goal_met=True)],
        report="Draft.", claims=[Claim(text="Something obscure.")], revision="Revised.",
    )  # fmt: skip
    left = MockLeftHemisphere(script=[
        {"select": ChoiceAnswer.from_probabilities({"a": 0.95, "b": 0.05})},
        {"claim:c1": NoulAnswer(probability=0.2)},
    ])  # fmt: skip
    result = await Harness(right, left, searcher=FakeSearcher({})).run("g")
    assert result.check.checks[0].verdict is ClaimVerdict.DOUBTFUL
    assert result.report == "Revised."

    checked = result.check.checks[0].model_copy(update={"sources": [SOURCE]})
    prompt = revise_prompt(result.state, "Draft.", [checked])
    assert "evidence from Mining pools (https://en.wikipedia.org/wiki/Mining_pool): A few pools mine most blocks." in prompt
    assert "you may use what it says, and name where it comes from" in prompt
