"""The built-in tools: web_search, fetch_url, and the composite that carries both."""

import httpx
import pytest

from hemisphere_harness import (
    ChoiceAnswer,
    ConfigError,
    Harness,
    MockLeftHemisphere,
    Option,
    Plan,
    Proposal,
    Reintegration,
    ScriptedRightHemisphere,
    Settings,
    Stakes,
)
from hemisphere_harness import envfile
from hemisphere_harness.builtin_tools import MAX_REDIRECTS, BuiltinToolbox, builtin_toolbox, search_problem
from hemisphere_harness.mcp_tools import MCPToolbox
from hemisphere_harness.right.prompts import render_state
from hemisphere_harness.schemas import Evidence
from hemisphere_harness.tools import CompositeToolbox, FunctionToolbox


class FakeSearcher:
    """Stands in for a real search service: what it was asked, and what it found."""

    name = "fake"

    def __init__(self, found):
        self.found = found
        self.queries = []
        self.closed = False

    async def search(self, query, limit=3):
        self.queries.append((query, limit))
        return self.found.get(query, [])

    async def aclose(self):
        self.closed = True


PAGE = """<html><head><title>Order A1</title><style>.x { color: red }</style></head>
<body><script>alert(1)</script><h1>Order A1</h1><p>Shipped <b>yesterday</b>.</p></body></html>"""


def fetch_box(handler, **kwargs):
    return BuiltinToolbox(fetch=True, transport=httpx.MockTransport(handler), **kwargs)


# --- web_search ------------------------------------------------------------------------------


def test_the_built_ins_are_on_by_default_and_can_be_turned_off():
    on = builtin_toolbox(Settings(_env_file=None), None)
    assert [tool.name for tool in on.tools] == ["web_search", "fetch_url"]
    assert all(not tool.needs_approval and tool.read_only and tool.source == "builtin" for tool in on.tools)
    assert on.problems == {}

    only_search = builtin_toolbox(Settings(_env_file=None, harness_fetch_url=False), None)
    assert [tool.name for tool in only_search.tools] == ["web_search"]

    only_fetch = builtin_toolbox(Settings(_env_file=None, harness_web_search=False), None)
    assert [tool.name for tool in only_fetch.tools] == ["fetch_url"]

    assert builtin_toolbox(Settings(_env_file=None, harness_web_search=False, harness_fetch_url=False), None) is None


def test_local_only_leaves_web_search_out_with_the_reason_rather_than_failing_the_run(clean_env):
    clean_env.setenv("HARNESS_LOCAL_ONLY", "true")
    settings = Settings(_env_file=None)

    box = builtin_toolbox(settings, None)

    assert [tool.name for tool in box.tools] == ["fetch_url"]  # fetch_url refuses per call
    assert "web_search" in box.problems
    assert "HARNESS_LOCAL_ONLY" in box.problems["web_search"]
    assert search_problem(settings, lookup_configured=False) is not None
    assert search_problem(Settings(_env_file=None, harness_local_only=False), lookup_configured=False) is None


async def test_web_search_formats_what_the_service_found():
    found = {"order a1": [Evidence(title="Order A1", url="https://shop.test/a1", snippet="Shipped on Tuesday.")]}
    searcher = FakeSearcher(found)
    box = BuiltinToolbox(searcher=searcher)

    result = await box.call("web_search", {"query": "order a1", "limit": 99})

    assert result.ok is True
    assert "1 result(s) for 'order a1':" in result.output
    assert "1. Order A1\n   https://shop.test/a1\n   Shipped on Tuesday." in result.output
    assert searcher.queries == [("order a1", 10)]  # limit is clamped to what a search can return


async def test_web_search_says_so_when_there_is_nothing_to_show():
    searcher = FakeSearcher({})
    box = BuiltinToolbox(searcher=searcher)

    assert (await box.call("web_search", {"query": "nothing here"})).output == "No results for 'nothing here'."
    assert (await box.call("web_search", {"query": "  "})).ok is False  # no query at all
    no_limit = await box.call("web_search", {"query": "x", "limit": "not a number"})
    assert no_limit.ok is True and searcher.queries[-1] == ("x", 3)  # a bad limit falls back


async def test_the_built_ins_close_what_they_own_and_leave_what_they_share():
    shared = FakeSearcher({})
    box = builtin_toolbox(Settings(_env_file=None), shared)
    await box.aclose()
    assert shared.closed is False  # the answer check's searcher stays with whoever made it

    alone = builtin_toolbox(Settings(_env_file=None), None)
    await alone.aclose()
    assert alone.tools[0].name == "web_search"
    # The Wikipedia searcher was made here, so it closes here.
    assert alone._searcher._client.is_closed is True


# --- fetch_url -------------------------------------------------------------------------------


async def test_a_page_becomes_its_text_with_no_scripts_and_the_title_kept():
    box = fetch_box(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE))

    result = await box.call("fetch_url", {"url": "https://shop.test/a1"})

    assert result.ok is True
    assert result.output.startswith("Title: Order A1\nURL: https://shop.test/a1")
    assert "Order A1\n\nShipped yesterday." in result.output
    assert "alert(1)" not in result.output and "color: red" not in result.output  # script and style dropped


async def test_a_plain_text_page_is_returned_as_it_is():
    box = fetch_box(lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, text="line one\nline two"))

    result = await box.call("fetch_url", {"url": "https://shop.test/notes.txt"})

    assert result.output == "URL: https://shop.test/notes.txt\n\nline one\nline two"


async def test_a_page_in_another_charset_is_read_correctly():
    body = "café".encode("iso-8859-1")
    box = fetch_box(
        lambda request: httpx.Response(200, headers={"content-type": "text/html; charset=iso-8859-1"}, content=body)
    )

    result = await box.call("fetch_url", {"url": "https://shop.test/fr"})

    assert "café" in result.output


async def test_fetch_refuses_what_is_not_a_page():
    box = fetch_box(lambda request: httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF"))

    assert (await box.call("fetch_url", {"url": "file:///etc/passwd"})).ok is False  # only http and https
    assert (await box.call("fetch_url", {"url": "https://shop.test/doc.pdf"})).output == (
        "https://shop.test/doc.pdf is not a text page (application/pdf)."
    )
    assert (await box.call("fetch_url", {"url": ""})).ok is False  # no URL at all


async def test_a_failing_fetch_is_a_result_not_a_crash():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    box = fetch_box(refuse)
    failed = await box.call("fetch_url", {"url": "https://shop.test/down"})

    assert failed.ok is False and "could not fetch https://shop.test/down" in failed.output

    not_found = fetch_box(lambda request: httpx.Response(404, text="gone"))
    missing = await not_found.call("fetch_url", {"url": "https://shop.test/gone"})
    assert missing.ok is False and "answered HTTP 404" in missing.output


async def test_a_long_page_is_cut_short_with_a_note():
    box = fetch_box(
        lambda request: httpx.Response(200, headers={"content-type": "text/plain"}, text="x" * 500), max_bytes=100
    )

    result = await box.call("fetch_url", {"url": "https://shop.test/long"})

    assert result.output.endswith("[… page cut short]")
    assert "x" * 100 in result.output


async def test_redirects_are_followed_and_counted():
    def hop(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/middle"})
        if request.url.path == "/middle":
            return httpx.Response(301, headers={"location": "/end"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="arrived")

    box = fetch_box(hop)
    result = await box.call("fetch_url", {"url": "https://shop.test/start"})
    assert "arrived" in result.output and "URL: https://shop.test/end" in result.output

    forever = fetch_box(lambda request: httpx.Response(302, headers={"location": "/again"}))
    loop = await forever.call("fetch_url", {"url": "https://shop.test/again"})
    assert loop.ok is False and f"more than {MAX_REDIRECTS} redirects" in loop.output


async def test_local_only_refuses_a_public_url_and_a_redirect_onto_one(clean_env):
    clean_env.setenv("HARNESS_LOCAL_ONLY", "true")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://elsewhere.test/away"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="leaked")

    box = fetch_box(handler, local_only=True)

    refused = await box.call("fetch_url", {"url": "https://elsewhere.test/page"})
    assert refused.ok is False and "HARNESS_LOCAL_ONLY is on" in refused.output and seen == []  # refused before any request

    via_local = await box.call("fetch_url", {"url": "http://localhost:8080/start"})
    assert via_local.ok is False and "HARNESS_LOCAL_ONLY is on" in via_local.output  # the redirect is checked too
    assert seen == ["http://localhost:8080/start"]  # the second hop never went out

    allowed = fetch_box(handler, local_only=True)
    here = await allowed.call("fetch_url", {"url": "http://localhost:8080/page"})
    assert here.ok is True and "leaked" in here.output


# --- CompositeToolbox ------------------------------------------------------------------------


def test_the_composite_lists_every_part_and_sends_each_call_to_its_owner():
    first, second = FunctionToolbox(), FunctionToolbox()
    first.add(lambda: "one", name="one", read_only=True)
    second.add(lambda: "two", name="two", read_only=True)
    box = CompositeToolbox(None, first, second)

    assert [tool.name for tool in box.tools] == ["one", "two"]
    assert box.parts == [first, second]


async def test_the_composite_calls_the_right_part_and_closes_them_all():
    closed = []

    class Recorder(FunctionToolbox):
        async def aclose(self):
            closed.append(self)

    first, second = Recorder(), Recorder()
    first.add(lambda: "one", name="one", read_only=True)
    second.add(lambda: "two", name="two", read_only=True)
    box = CompositeToolbox(first, second)

    assert (await box.call("two", {})).output == "two"
    assert (await box.call("nope", {})).ok is False
    await box.aclose()
    assert closed == [first, second]


def test_two_tools_with_the_same_name_are_a_configuration_error():
    first, second = FunctionToolbox(), FunctionToolbox()
    first.add(lambda: 1, name="dup")
    second.add(lambda: 2, name="dup")

    with pytest.raises(ConfigError, match="both named 'dup'"):
        CompositeToolbox(first, second)


def test_the_composite_merges_server_errors_and_left_out_built_ins():
    mcp = MCPToolbox()
    mcp.errors = {"shop": "did not start"}
    box = CompositeToolbox(mcp, BuiltinToolbox(problems={"web_search": "why"}))

    assert box.errors == {"shop": "did not start"}
    assert box.problems == {"web_search": "why"}
    assert CompositeToolbox(None, None).errors == {}


def test_the_tools_event_reports_what_is_available_and_what_was_left_out():
    from hemisphere_harness.web.runs import _tools_event

    box = CompositeToolbox(BuiltinToolbox(fetch=True, problems={"web_search": "why"}))
    event = _tools_event(box)

    assert event.phase == "tools"
    assert "1 tool(s) available" in event.summary and "1 built-in(s) left out" in event.summary
    assert "web_search left out: why" in event.details
    assert event.data["problems"] == {"web_search": "why"}
    assert [tool["name"] for tool in event.data["tools"]] == ["fetch_url"]


# --- A run that searches ---------------------------------------------------------------------


async def test_a_run_may_commit_a_search_and_the_right_reads_what_it_found():
    searcher = FakeSearcher({"order a1": [Evidence(title="Order A1", url="https://shop.test/a1", snippet="Shipped Tuesday.")]})
    box = builtin_toolbox(Settings(_env_file=None), searcher)
    search = Option(id="search", description="Search for order A1's status", tool="web_search", arguments={"query": "order a1"})
    reply = Option(id="reply", description="Reply to the customer")
    right = ScriptedRightHemisphere(
        plan=Plan(situation="s"),
        proposals=[Proposal(options=[search, reply], rationale="r", stakes=Stakes.LOW, open_ended=False)],
        reintegrations=[Reintegration(situation="found it", goal_met=True)],
        report="It shipped Tuesday.",
    )  # fmt: skip
    left = MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"search": 0.95, "reply": 0.05})}])

    try:
        result = await Harness(right, left, toolbox=box).run("Where is order A1?")
    finally:
        await box.aclose()

    step = result.state.history[0]
    assert step.decision.committed_by == "left"  # read-only, so no sign-off was needed
    assert step.action.ok is True and "Shipped Tuesday." in step.action.output
    assert searcher.queries == [("order a1", 3)]
    assert "'search' (called web_search)" in step.summary()  # the right reads the result at reintegration
    assert "web_search" in render_state(result.state)  # and sees the tool when it proposes


# --- The settings ----------------------------------------------------------------------------


def test_the_built_in_toggles_round_trip_through_the_env_file(clean_env, tmp_path):
    path = tmp_path / ".env"

    envfile.write(path, {"HARNESS_WEB_SEARCH": "false"})
    assert Settings(_env_file=path).harness_web_search is False
    assert envfile.read(path)["HARNESS_WEB_SEARCH"]["value"] == "false"
    assert envfile.read(path)["HARNESS_FETCH_URL"]["default"] == "true"  # still on, and the file says so

    envfile.write(path, {"HARNESS_WEB_SEARCH": None})
    assert Settings(_env_file=path).harness_web_search is True
