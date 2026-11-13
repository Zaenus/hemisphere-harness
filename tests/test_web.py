"""The web UI's server: access control, settings, runs and model listing. No network."""

import asyncio
import json
import os
import stat

import httpx
import pytest

from hemisphere_harness import Plan, ScriptedRightHemisphere
from hemisphere_harness.cli import _is_loopback, main
from hemisphere_harness.providers import left_providers, right_providers
from hemisphere_harness.web import WebConfig, create_app
from hemisphere_harness.web import runs as runs_module

TOKEN = "test-token"
BASE = "http://127.0.0.1:8765"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def make_app(tmp_path, *, host="127.0.0.1", transport=None):
    return create_app(
        WebConfig(env_file=tmp_path / ".env", data_dir=tmp_path / "data", token=TOKEN, host=host, transport=transport, demo_pace_s=0)
    )


def client(app, **kwargs):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE, **kwargs)


async def wait_for_end(http, run_id, timeout=5.0):
    async def poll():
        while True:
            run = (await http.get(f"/api/runs/{run_id}", headers=AUTH)).json()
            if run["status"] != "running":
                return run
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout)


# --- Access ----------------------------------------------------------------------------


async def test_the_api_needs_the_token(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        assert (await http.get("/api/status")).status_code == 401
        assert (await http.get("/api/status", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await http.get("/api/status", headers=AUTH)).status_code == 200


async def test_the_sign_in_link_sets_a_strict_cookie_and_drops_the_token(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        locked = await http.get("/")
        assert locked.status_code == 401
        assert "link printed" in locked.text or "terminal" in locked.text

        signed_in = await http.get(f"/?token={TOKEN}")
        assert signed_in.status_code == 303
        assert signed_in.headers["location"] == "/"
        cookie = signed_in.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie

        page = await http.get("/")  # the client kept the cookie
        assert page.status_code == 200
        assert "app.js" in page.text
        assert (await http.get("/api/status")).status_code == 200


async def test_a_foreign_host_header_is_refused_on_loopback(clean_env, tmp_path):
    """A DNS-rebinding page reaches the server with its own host name in Host."""
    async with client(make_app(tmp_path)) as http:
        response = await http.get("/api/status", headers={**AUTH, "Host": "attacker.example:8765"})
        assert response.status_code == 421
        assert (await http.get("/api/status", headers={**AUTH, "Host": "localhost:8765"})).status_code == 200


async def test_on_a_public_address_any_host_works_but_the_token_is_still_needed(clean_env, tmp_path):
    async with client(make_app(tmp_path, host="0.0.0.0")) as http:
        assert (await http.get("/api/status", headers={"Host": "box.lan:8765"})).status_code == 401
        assert (await http.get("/api/status", headers={**AUTH, "Host": "box.lan:8765"})).status_code == 200


async def test_cross_origin_writes_are_refused(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        response = await http.put(
            "/api/settings", headers={**AUTH, "Origin": "http://attacker.example"}, json={"RIGHT_MODEL": "x"}
        )
        assert response.status_code == 403
        assert not (tmp_path / ".env").exists()
        same = await http.put("/api/settings", headers={**AUTH, "Origin": BASE}, json={"RIGHT_MODEL": "x"})
        assert same.status_code == 200


async def test_every_response_carries_the_security_headers(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        for response in (await http.get("/api/status", headers=AUTH), await http.get("/static/app.js")):
            assert "script-src 'self'" in response.headers["content-security-policy"]
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["x-frame-options"] == "DENY"


# --- Settings ----------------------------------------------------------------------------


async def test_settings_are_saved_to_the_env_file_for_the_owner_only(clean_env, tmp_path):
    (tmp_path / ".env").write_text("# my notes\nUNRELATED=keep me\nRIGHT_MODEL=old\n")
    async with client(make_app(tmp_path)) as http:
        response = await http.put(
            "/api/settings",
            headers=AUTH,
            json={"RIGHT_MODEL": "new-model", "HARNESS_POLICY": 'Refunds above $500 need sign-off. Don\'t "guess".'},
        )
    assert response.status_code == 200
    text = (tmp_path / ".env").read_text()
    assert "# my notes" in text and "UNRELATED=keep me" in text
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600

    from hemisphere_harness import Settings

    settings = Settings(_env_file=tmp_path / ".env")
    assert settings.right_model == "new-model"
    assert settings.harness_policy == 'Refunds above $500 need sign-off. Don\'t "guess".'


async def test_saved_keys_are_never_sent_back(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        saved = await http.put("/api/settings", headers=AUTH, json={"OPENAI_API_KEY": "sk-very-secret"})
        listed = await http.get("/api/settings", headers=AUTH)
    for response in (saved, listed):
        assert "sk-very-secret" not in response.text
    field = next(f for f in listed.json()["fields"] if f["name"] == "OPENAI_API_KEY")
    assert field == {**field, "set": True, "source": "file"}
    assert "value" not in field


async def test_a_variable_from_the_shell_is_reported_as_overriding_the_file(clean_env, tmp_path):
    clean_env.setenv("RIGHT_MODEL", "from-shell")
    clean_env.setenv("JEV_API_KEY", "jev-secret")
    async with client(make_app(tmp_path)) as http:
        fields = {f["name"]: f for f in (await http.get("/api/settings", headers=AUTH)).json()["fields"]}
    assert fields["RIGHT_MODEL"]["source"] == "environment"
    assert fields["RIGHT_MODEL"]["value"] == "from-shell"
    assert fields["JEV_API_KEY"] == {**fields["JEV_API_KEY"], "set": True, "source": "environment"}
    assert "jev-secret" not in json.dumps(fields)


async def test_a_typesafe_alias_counts_as_the_jev_key(clean_env, tmp_path):
    clean_env.setenv("TYPESAFE_API_KEY", "ts-secret")
    async with client(make_app(tmp_path)) as http:
        fields = {f["name"]: f for f in (await http.get("/api/settings", headers=AUTH)).json()["fields"]}
    assert fields["JEV_API_KEY"]["set"] is True


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"HARNESS_COMMIT_THRESHOLD": "1.7"}, "HARNESS_COMMIT_THRESHOLD"),
        ({"HARNESS_PERMIT_NO_THRESHOLD": "0.9"}, "permit_no_threshold must be below"),
        ({"RIGHT_PROVIDER": "nonexistent"}, "not a known right-hemisphere provider"),
        ({"SOMETHING_ELSE": "x"}, "unknown settings"),
        ({"HARNESS_POLICY": "Never ${expand} this"}, "cannot hold '${' literally"),
        ({"RIGHT_MODEL": ["not", "a", "string"]}, "object of variable names"),
    ],
)
async def test_bad_settings_are_refused_and_nothing_is_written(clean_env, tmp_path, change, message):
    async with client(make_app(tmp_path)) as http:
        response = await http.put("/api/settings", headers=AUTH, json=change)
    assert response.status_code == 400
    assert message in response.json()["error"]
    assert not (tmp_path / ".env").exists()


async def test_null_removes_a_saved_value(clean_env, tmp_path):
    (tmp_path / ".env").write_text("OPENAI_API_KEY='sk-old'\nRIGHT_MODEL='m'\n")
    async with client(make_app(tmp_path)) as http:
        await http.put("/api/settings", headers=AUTH, json={"OPENAI_API_KEY": None, "RIGHT_MODEL": ""})
    text = (tmp_path / ".env").read_text()
    assert "OPENAI_API_KEY" not in text and "RIGHT_MODEL" not in text


async def test_the_settings_list_describes_every_provider(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        data = (await http.get("/api/settings", headers=AUTH)).json()
    right = {p["name"]: p for p in data["right_providers"]}
    left = {p["name"]: p for p in data["left_providers"]}
    assert right["openai"]["api_key_env"] == "OPENAI_API_KEY"
    assert right["ollama"]["base_url"] == "http://localhost:11434/v1"
    assert right["openai-compatible"]["base_url"] is None
    assert left["llm-experimental"]["experimental"] is True
    assert "LEFT_MODEL" in left["ollama"]["settings"]
    field_names = {f["name"] for f in data["fields"]}
    assert set(left["jev"]["settings"]) <= field_names
    assert set(right["openai"]["settings"]) <= field_names


# --- Runs ----------------------------------------------------------------------------------


async def test_the_offline_demos_run_end_to_end(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        started = await http.post("/api/runs", headers=AUTH, json={"demo": "lockout"})
        assert started.status_code == 201
        run = await wait_for_end(http, started.json()["id"])
        assert run["status"] == "done"
        assert run["offline"] is True
        phases = [event["phase"] for event in run["events"]]
        assert phases[0] == "plan" and phases[-1] == "end"
        decide = next(event for event in run["events"] if event["phase"] == "decide")
        assert decide["data"]["decision"]["probabilities"]["unlock_account"] == pytest.approx(0.89)

        refund = await wait_for_end(http, (await http.post("/api/runs", headers=AUTH, json={"demo": "refund"})).json()["id"])
        assert refund["status"] == "needs_human"
        assert refund["policy"]


async def test_the_event_stream_replays_the_run_and_ends(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        run_id = (await http.post("/api/runs", headers=AUTH, json={"demo": "refund"})).json()["id"]
        run = await wait_for_end(http, run_id)
        stream = await http.get(f"/api/runs/{run_id}/events", headers=AUTH)
        assert stream.headers["content-type"].startswith("text/event-stream")
        traces = [json.loads(line[6:]) for line in stream.text.splitlines() if line.startswith("data: {\"actor\"")]
        assert [t["phase"] for t in traces] == [e["phase"] for e in run["events"]]
        assert "event: end" in stream.text

        resumed = await http.get(f"/api/runs/{run_id}/events", headers={**AUTH, "Last-Event-ID": "5"})
        ids = [int(line[4:]) for line in resumed.text.splitlines() if line.startswith("id: ")]
        assert ids == list(range(6, len(run["events"])))


async def test_runs_are_kept_across_a_restart(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        run_id = (await http.post("/api/runs", headers=AUTH, json={"demo": "lockout"})).json()["id"]
        await wait_for_end(http, run_id)

    # A run that was still going when the server stopped.
    data = json.loads((tmp_path / "data" / "runs" / f"{run_id}.json").read_text())
    (tmp_path / "data" / "runs" / "stale.json").write_text(json.dumps({**data, "id": "stale", "status": "running"}))
    (tmp_path / "data" / "runs" / "broken.json").write_text("{not json")

    async with client(make_app(tmp_path)) as http:
        runs = {run["id"]: run for run in (await http.get("/api/runs", headers=AUTH)).json()["runs"]}
        assert runs[run_id]["status"] == "done"
        assert runs["stale"]["status"] == "failed"
        assert "broken" not in runs


async def test_a_goal_of_your_own_needs_a_right_hemisphere(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        response = await http.post("/api/runs", headers=AUTH, json={"goal": "Do something"})
        assert response.status_code == 400
        assert "Settings" in response.json()["error"]
        assert (await http.get("/api/runs", headers=AUTH)).json()["runs"] == []


async def test_a_run_uses_the_settings_saved_since_the_server_started(clean_env, tmp_path, monkeypatch):
    built = []

    def fake_right(settings):
        built.append(settings.right_model)
        return ScriptedRightHemisphere(plan=Plan(situation="s"), proposals=[])

    monkeypatch.setattr(runs_module, "build_right", fake_right)
    async with client(make_app(tmp_path)) as http:
        await http.put("/api/settings", headers=AUTH, json={"RIGHT_MODEL": "saved-just-now", "HARNESS_POLICY": "p"})
        run_id = (await http.post("/api/runs", headers=AUTH, json={"goal": "g"})).json()["id"]
        run = await wait_for_end(http, run_id)
    assert built == ["saved-just-now"]
    assert run["policy"] == "p"
    assert run["status"] == "failed"  # the script had no proposal: the right failing ends the run
    assert "nothing left for propose" in run["error"]


async def test_a_run_can_be_stopped(clean_env, tmp_path, monkeypatch):
    class Stuck(ScriptedRightHemisphere):
        async def plan(self, state):
            await asyncio.Event().wait()

    monkeypatch.setattr(runs_module, "build_right", lambda s: Stuck(plan=Plan(situation="s"), proposals=[]))
    clean_env.setenv("RIGHT_MODEL", "anything")
    async with client(make_app(tmp_path)) as http:
        run_id = (await http.post("/api/runs", headers=AUTH, json={"goal": "g"})).json()["id"]
        assert (await http.delete(f"/api/runs/{run_id}", headers=AUTH)).status_code == 409
        assert (await http.post(f"/api/runs/{run_id}/cancel", headers=AUTH)).json() == {"cancelled": True}
        run = await wait_for_end(http, run_id)
        assert run["status"] == "cancelled"
        assert (await http.delete(f"/api/runs/{run_id}", headers=AUTH)).status_code == 200
        assert (await http.get(f"/api/runs/{run_id}", headers=AUTH)).status_code == 404


async def test_a_custom_run_can_turn_the_policy_off(clean_env, tmp_path, monkeypatch):
    monkeypatch.setattr(runs_module, "build_right", lambda s: ScriptedRightHemisphere(plan=Plan(situation="s"), proposals=[]))
    clean_env.setenv("RIGHT_MODEL", "anything")
    clean_env.setenv("HARNESS_POLICY", "from settings")
    async with client(make_app(tmp_path)) as http:
        default = (await http.post("/api/runs", headers=AUTH, json={"goal": "g"})).json()
        off = (await http.post("/api/runs", headers=AUTH, json={"goal": "g", "policy": ""})).json()
        await wait_for_end(http, default["id"])
        await wait_for_end(http, off["id"])
    assert default["policy"] == "from settings"
    assert off["policy"] == ""


# --- Model listing -----------------------------------------------------------------------


async def test_ollama_models_are_marked_by_whether_they_can_decide(clean_env, tmp_path):
    def handler(request):
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:latest"}, {"name": "tev1:latest"}]})
        model = json.loads(request.content)["model"]
        return httpx.Response(200, json={"capabilities": ["decision"] if model.startswith("tev1") else ["completion"]})

    async with client(make_app(tmp_path, transport=httpx.MockTransport(handler))) as http:
        response = await http.post("/api/models", headers=AUTH, json={"hemisphere": "left", "provider": "ollama"})
    assert response.json()["models"] == [{"id": "tev1:latest", "decision": True}, {"id": "gemma4:latest", "decision": False}]


async def test_a_providers_key_is_sent_only_to_its_own_endpoint_when_listing(clean_env, tmp_path):
    clean_env.setenv("OPENAI_API_KEY", "sk-home")
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}]})

    async with client(make_app(tmp_path, transport=httpx.MockTransport(handler))) as http:
        home = await http.post("/api/models", headers=AUTH, json={"hemisphere": "right", "provider": "openai"})
        away = await http.post(
            "/api/models", headers=AUTH,
            json={"hemisphere": "right", "provider": "openai", "base_url": "https://proxy.example/v1"},
        )  # fmt: skip
    assert home.json()["models"] == [{"id": "a"}, {"id": "b"}]
    assert away.status_code == 200
    assert seen == [("api.openai.com", "Bearer sk-home"), ("proxy.example", None)]


async def test_an_unreachable_server_is_reported_not_raised(clean_env, tmp_path):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    async with client(make_app(tmp_path, transport=httpx.MockTransport(handler))) as http:
        response = await http.post("/api/models", headers=AUTH, json={"hemisphere": "right", "provider": "vllm"})
    assert response.status_code == 502
    assert "Could not reach http://localhost:8000/v1/models" in response.json()["error"]


async def test_the_status_reports_what_is_set_up(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        status = (await http.get("/api/status", headers=AUTH)).json()
    assert status["right"]["ready"] is False
    assert status["left"] == {**status["left"], "ready": True, "mock": True}


async def test_checking_the_mock_left_says_it_is_a_mock(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        response = await http.post("/api/check", headers=AUTH, json={"hemisphere": "left"})
    assert response.json()["ok"] is True
    assert "mock" in response.json()["message"]


# --- Command line ------------------------------------------------------------------------


def test_with_no_command_the_cli_lists_providers(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Right hemisphere providers" in out and "hemisphere-harness web" in out


@pytest.mark.parametrize(("host", "loopback"), [("127.0.0.1", True), ("::1", True), ("localhost", True), ("0.0.0.0", False), ("192.168.1.5", False)])
def test_loopback_detection(host, loopback):
    assert _is_loopback(host) is loopback


def test_registries_expose_provider_metadata():
    assert right_providers.get("nim").base_url == "https://integrate.api.nvidia.com/v1"
    assert left_providers.get("jev").api_key_env == "JEV_API_KEY"
    assert not left_providers.get("mock").settings


async def test_the_lockout_demo_checks_its_answer_and_corrects_it(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        run = await wait_for_end(http, (await http.post("/api/runs", headers=AUTH, json={"demo": "lockout"})).json()["id"])
    phases = [event["phase"] for event in run["events"]]
    assert phases[-5:] == ["draft", "check", "revise", "report", "end"]
    check = next(event for event in run["events"] if event["phase"] == "check")
    assert [c["verdict"] for c in check["data"]["checks"]] == ["holds", "doubtful"]
    assert "second lockout" in run["report"] and "first lockout" not in run["report"]


async def test_with_the_mock_left_a_live_run_does_not_check_answers(clean_env, tmp_path, monkeypatch):
    monkeypatch.setattr(runs_module, "build_right", lambda s: ScriptedRightHemisphere(plan=Plan(situation="s"), proposals=[]))
    clean_env.setenv("RIGHT_MODEL", "anything")  # the left stays the mock
    async with client(make_app(tmp_path)) as http:
        run = (await http.post("/api/runs", headers=AUTH, json={"goal": "g"})).json()
        await wait_for_end(http, run["id"])
    assert run["config"]["check_answers"] is False


# --- Conversations ------------------------------------------------------------------------


class OneStepRight(ScriptedRightHemisphere):
    """Answers every message in one step and keeps the conversation it was shown."""

    conversations: list = []

    def __init__(self, *args, **kwargs):
        super().__init__(plan=Plan(situation="s"), proposals=[])

    async def plan(self, state):
        OneStepRight.conversations.append([(t.request, t.answer) for t in state.conversation])
        return Plan(situation="reading")

    async def propose(self, state):
        from hemisphere_harness import Option, Proposal, Stakes

        return Proposal(options=[Option(id="a", description="a"), Option(id="b", description="b")],
                        rationale="r", stakes=Stakes.LOW, open_ended=False)  # fmt: skip

    async def evaluate(self, state, proposal, decision):
        from hemisphere_harness import Evaluation, Verdict

        return Evaluation(verdict=Verdict.COMMIT, option_id="a", reasoning="r")

    async def reintegrate(self, state, step):
        from hemisphere_harness import Reintegration

        return Reintegration(situation="done", goal_met=True)

    async def generate(self, prompt, state=None):
        return f"answer to: {state.goal}"


async def test_a_reply_continues_the_conversation_with_what_came_before(clean_env, tmp_path, monkeypatch):
    OneStepRight.conversations = []
    monkeypatch.setattr(runs_module, "build_right", lambda s: OneStepRight())
    clean_env.setenv("RIGHT_MODEL", "anything")
    async with client(make_app(tmp_path)) as http:
        first = (await http.post("/api/runs", headers=AUTH, json={"goal": "Is Bitcoin decentralized?", "context": "ctx", "policy": "p"})).json()
        await wait_for_end(http, first["id"])
        reply = await http.post("/api/runs", headers=AUTH, json={"goal": "And Ethereum?", "conversation_id": first["conversation_id"]})
        assert reply.status_code == 201
        second = await wait_for_end(http, reply.json()["id"])
        conversation = (await http.get(f"/api/conversations/{first['id']}", headers=AUTH)).json()
        listed = (await http.get("/api/runs", headers=AUTH)).json()["runs"]

    assert first["conversation_id"] == first["id"]
    assert second["status"] == "done", second["error"]
    assert second["conversation_id"] == first["id"]
    assert (second["message"], second["context"], second["policy"]) == ("And Ethereum?", "ctx", "p")
    assert OneStepRight.conversations == [[], [("Is Bitcoin decentralized?", "answer to: Is Bitcoin decentralized?")]]
    assert [run["id"] for run in conversation["runs"]] == [first["id"], second["id"]]
    assert all("state" not in run for run in conversation["runs"])  # the saved state stays on the server
    assert {run["conversation_id"] for run in listed} == {first["id"]}


async def test_replies_wait_for_the_running_run_and_need_a_right_hemisphere(clean_env, tmp_path, monkeypatch):
    class Stuck(ScriptedRightHemisphere):
        async def plan(self, state):
            await asyncio.Event().wait()

    async with client(make_app(tmp_path)) as http:
        demo = await wait_for_end(http, (await http.post("/api/runs", headers=AUTH, json={"demo": "lockout"})).json()["id"])
        offline = await http.post("/api/runs", headers=AUTH, json={"goal": "thanks", "conversation_id": demo["conversation_id"]})
        assert offline.status_code == 400 and "Replies need a right hemisphere" in offline.json()["error"]

        monkeypatch.setattr(runs_module, "build_right", lambda s: Stuck(plan=Plan(situation="s"), proposals=[]))
        clean_env.setenv("RIGHT_MODEL", "anything")
        running = (await http.post("/api/runs", headers=AUTH, json={"goal": "g"})).json()
        busy = await http.post("/api/runs", headers=AUTH, json={"goal": "more", "conversation_id": running["conversation_id"]})
        assert busy.status_code == 409
        await http.post(f"/api/runs/{running['id']}/cancel", headers=AUTH)
        await wait_for_end(http, running["id"])

        missing = await http.post("/api/runs", headers=AUTH, json={"goal": "x", "conversation_id": "nope"})
        assert missing.status_code == 400


async def test_approving_the_refund_demo_continues_the_stopped_run(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        stopped = await wait_for_end(http, (await http.post("/api/runs", headers=AUTH, json={"demo": "refund"})).json()["id"])
        assert stopped["status"] == "needs_human"

        resumed = await http.post(f"/api/runs/{stopped['id']}/resume", headers=AUTH,
                                  json={"approve": True, "option_id": "full_refund", "note": "finance agrees"})  # fmt: skip
        assert resumed.status_code == 201
        run = await wait_for_end(http, resumed.json()["id"])

        again = await http.post(f"/api/runs/{stopped['id']}/resume", headers=AUTH, json={"approve": True, "option_id": "full_refund"})
        conversation = (await http.get(f"/api/conversations/{stopped['conversation_id']}", headers=AUTH)).json()

    assert run["status"] == "done"
    assert run["message"] == "Approved: Cancel the plan and refund the full $2,400 — finance agrees"
    assert run["resumed_from"] == stopped["id"] and run["conversation_id"] == stopped["conversation_id"]
    first = run["events"][0]
    assert (first["actor"], first["phase"]) == ("human", "approve")
    act = next(event for event in run["events"] if event["phase"] == "act")
    assert act["data"]["action"]["output"] == "Plan cancelled; $2,400 refunded to the card on file."
    assert run["report"].startswith("Done: with your sign-off")
    assert again.status_code == 409  # the conversation has moved on
    assert len(conversation["runs"]) == 2


async def test_what_can_and_cannot_be_resumed(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        stopped = await wait_for_end(http, (await http.post("/api/runs", headers=AUTH, json={"demo": "refund"})).json()["id"])
        done = await wait_for_end(http, (await http.post("/api/runs", headers=AUTH, json={"demo": "lockout"})).json()["id"])

        invented = await http.post(f"/api/runs/{stopped['id']}/resume", headers=AUTH, json={"approve": True, "option_id": "invented"})
        assert invented.status_code == 400 and "not one of" in invented.json()["error"]
        rejected = await http.post(f"/api/runs/{stopped['id']}/resume", headers=AUTH, json={"approve": False})
        assert rejected.status_code == 400 and "only for an approval" in rejected.json()["error"]
        finished = await http.post(f"/api/runs/{done['id']}/resume", headers=AUTH, json={"approve": False})
        assert finished.status_code == 409


async def test_runs_saved_before_conversations_load_as_conversations_of_one(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        run_id = (await http.post("/api/runs", headers=AUTH, json={"demo": "lockout"})).json()["id"]
        await wait_for_end(http, run_id)
    file = tmp_path / "data" / "runs" / f"{run_id}.json"
    old = json.loads(file.read_text())
    for key in ("conversation_id", "message", "resumed_from", "state"):
        old.pop(key, None)
    file.write_text(json.dumps(old))

    async with client(make_app(tmp_path)) as http:
        run = (await http.get(f"/api/runs/{run_id}", headers=AUTH)).json()
    assert run["conversation_id"] == run_id
    assert run["message"] == run["goal"]


async def test_a_conversation_is_deleted_whole(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        stopped = await wait_for_end(http, (await http.post("/api/runs", headers=AUTH, json={"demo": "refund"})).json()["id"])
        resumed = (await http.post(f"/api/runs/{stopped['id']}/resume", headers=AUTH, json={"approve": True, "option_id": "full_refund"})).json()
        await wait_for_end(http, resumed["id"])
        deleted = await http.delete(f"/api/conversations/{stopped['conversation_id']}", headers=AUTH)
        assert sorted(deleted.json()["deleted"]) == sorted([stopped["id"], resumed["id"]])
        assert (await http.get("/api/runs", headers=AUTH)).json()["runs"] == []
        assert (await http.get(f"/api/conversations/{stopped['conversation_id']}", headers=AUTH)).status_code == 404


# --- Tools ------------------------------------------------------------------------------------

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

MCP_SERVER = str(Path(__file__).with_name("mcp_test_server.py"))


def write_mcp_config(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"shop": {"command": sys.executable, "args": [MCP_SERVER]}}}))
    return path


async def test_the_tools_endpoint_lists_what_the_servers_offer(clean_env, tmp_path):
    async with client(make_app(tmp_path)) as http:
        none = (await http.get("/api/tools", headers=AUTH)).json()
        assert none == {"config": None, "tools": [], "errors": {}}

        clean_env.setenv("HARNESS_MCP_CONFIG", str(write_mcp_config(tmp_path)))
        listed = (await http.get("/api/tools", headers=AUTH)).json()
        tools = {tool["name"]: tool["needs_approval"] for tool in listed["tools"]}
        assert tools == {"shop.lookup_order": False, "shop.save_note": True, "shop.broken": False}
        assert (await http.get("/api/status", headers=AUTH)).json()["tools_config"] == str(tmp_path / "mcp.json")

        (tmp_path / "mcp.json").write_text("{broken")
        bad = await http.get("/api/tools", headers=AUTH)
        assert bad.status_code == 400 and "not valid JSON" in bad.json()["error"]


async def test_a_live_run_calls_a_real_tool(clean_env, tmp_path, monkeypatch):
    from hemisphere_harness import ChoiceAnswer, MockLeftHemisphere, Option, Proposal, Reintegration, Stakes

    lookup = Option(id="look", description="Look up order A1", tool="shop.lookup_order", arguments={"order_id": "A1"})
    right = lambda s: ScriptedRightHemisphere(  # noqa: E731
        plan=Plan(situation="s"),
        proposals=[Proposal(options=[lookup, Option(id="reply", description="reply")], rationale="r", stakes=Stakes.LOW, open_ended=False)],
        reintegrations=[Reintegration(situation="found", goal_met=True)],
        report="It has shipped.",
    )
    left = lambda s: MockLeftHemisphere(script=[{"select": ChoiceAnswer.from_probabilities({"look": 0.95, "reply": 0.05})}])  # noqa: E731
    monkeypatch.setattr(runs_module, "build_right", right)
    monkeypatch.setattr(runs_module, "build_left", left)
    clean_env.setenv("RIGHT_MODEL", "anything")
    clean_env.setenv("HARNESS_MCP_CONFIG", str(write_mcp_config(tmp_path)))

    async with client(make_app(tmp_path)) as http:
        run = await wait_for_end(http, (await http.post("/api/runs", headers=AUTH, json={"goal": "Where is A1?"})).json()["id"], timeout=20)

    assert run["status"] == "done", run["error"]
    assert run["events"][0]["phase"] == "tools"
    assert "shop.lookup_order" in run["tools"]
    act = next(event for event in run["events"] if event["phase"] == "act")
    assert act["data"]["action"] == {"option_id": "look", "ok": True, "output": "order A1: 2 items, shipped", "tool": "shop.lookup_order"}
