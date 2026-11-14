"""The command line.

    hemisphere-harness               list the providers available here
    hemisphere-harness providers     the same
    hemisphere-harness web           start the web UI and open it in the browser
    hemisphere-harness chat          talk to the harness in the terminal
    hemisphere-harness eval          measure the left hemisphere on decisions with known answers
    hemisphere-harness replay RUN    ask a past run's questions again, of another left hemisphere
    hemisphere-harness compare       run tasks with the harness and with its right alone, and compare

`python -m hemisphere_harness` is the same command.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import secrets
import sys
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import HarnessConfig, Settings
from .conversation import Conversation
from .errors import ConfigError, HarnessError
from .harness import Harness
from .left import MockLeftHemisphere
from .lookup import searcher_from_settings
from .mcp_tools import toolbox_from_settings
from .memory import DEFAULT_FILE as DEFAULT_MEMORY_FILE
from .memory import MemoryStore
from .providers import build_left, build_right, left_providers, right_providers
from .schemas import RunResult, RunStatus
from .trace import TracePrinter

DEFAULT_PORT = 8765


def list_providers() -> None:
    for registry in (right_providers, left_providers):
        rows = registry.describe()
        width = max(len(label) for label, _ in rows)
        print(f"{registry.kind.capitalize()} hemisphere providers  ({registry.variable})")
        for label, summary in rows:
            print(f"  {label:<{width}}  {summary}")
        print()
    print("To add a provider, see docs/providers.md.")
    print("To configure them in a browser: hemisphere-harness web")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hemisphere-harness", description="The hemisphere agent harness.")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("providers", help="list the providers available here (the default)")
    web = commands.add_parser("web", help="start the web UI")
    web.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    web.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT})")
    web.add_argument("--no-open", action="store_true", help="do not open a browser")
    web.add_argument("--env-file", type=Path, default=Path(".env"), help="settings file to read and write (default: ./.env)")
    web.add_argument(
        "--data-dir", type=Path, default=Path(".hemisphere"), help="where run history is kept (default: ./.hemisphere)"
    )
    chat = commands.add_parser("chat", help="talk to the harness in the terminal")
    chat.add_argument("--context", default="", help="facts that hold for the whole conversation")
    chat.add_argument("--trace", action="store_true", help="show every step, not only the answers")
    chat.add_argument("--env-file", type=Path, default=Path(".env"), help="settings file (default: ./.env)")
    evaluate = commands.add_parser("eval", help="measure the left hemisphere on decisions with known answers")
    evaluate.add_argument("--scenarios", type=Path, help="a scenarios file (default: the ones that ship with the package)")
    evaluate.add_argument(
        "--signoffs", nargs="?", const=Path(".hemisphere/signoffs.json"), type=Path, metavar="FILE",
        help="your own sign-offs from the web UI instead (default file: .hemisphere/signoffs.json)",
    )  # fmt: skip
    evaluate.add_argument("--domain", help="only scenarios whose id starts with this, e.g. support")
    evaluate.add_argument("--json", type=Path, help="also write the full report, with every answer, to this file")
    evaluate.add_argument("--env-file", type=Path, default=Path(".env"), help="settings file (default: ./.env)")
    again = commands.add_parser("replay", help="ask a past run's questions again, of another left hemisphere")
    again.add_argument("run", help="a run id from the web UI's history, or the path to its .json file")
    again.add_argument("--left-provider", help="the left to ask (default: LEFT_PROVIDER)")
    again.add_argument("--left-model", help="the model to ask (default: LEFT_MODEL)")
    again.add_argument("--data-dir", type=Path, default=Path(".hemisphere"), help="where run history is kept (default: ./.hemisphere)")
    again.add_argument("--json", type=Path, help="also write the replay to this file")
    again.add_argument("--env-file", type=Path, default=Path(".env"), help="settings file (default: ./.env)")
    versus = commands.add_parser("compare", help="run tasks with the harness and with its right alone, and compare")
    versus.add_argument("--tasks", type=Path, help="a tasks file (default: the ones that ship with the package)")
    versus.add_argument("--only", action="append", metavar="TASK", help="run only this task; repeat for more")
    versus.add_argument("--max-steps", type=int, default=4, help="steps per run (default: 4)")
    versus.add_argument("--parallel", type=int, default=4, help="runs at once (default: 4)")
    versus.add_argument("--json", type=Path, help="also write every run, with its answer, to this file")
    versus.add_argument("--env-file", type=Path, default=Path(".env"), help="settings file (default: ./.env)")
    args = parser.parse_args(argv)

    if args.command == "compare":
        return run_compare_command(args)
    if args.command == "eval":
        return run_eval_command(args)
    if args.command == "replay":
        return run_replay_command(args)
    if args.command == "web":
        return run_web(args)
    if args.command == "chat":
        return run_chat(args)
    list_providers()
    return 0


def run_web(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .web import WebConfig, create_app
    except ImportError:
        print('The web UI needs its extra dependencies:  pip install "hemisphere-harness[web]"', file=sys.stderr)
        return 2

    if os.environ.get("HARNESS_IN_CONTAINER"):
        print("In a container: publish the port to 127.0.0.1 only, as compose.yaml does.", file=sys.stderr)
    elif not _is_loopback(args.host):
        print(
            f"Warning: listening on {args.host} makes the UI reachable from other machines. Anyone with the "
            "link can start runs with your API keys. Prefer 127.0.0.1 and an SSH tunnel.",
            file=sys.stderr,
        )

    token = os.environ.get("HARNESS_WEB_TOKEN") or secrets.token_urlsafe(24)
    config = WebConfig(
        env_file=args.env_file.resolve(),
        data_dir=args.data_dir.resolve(),
        token=token,
        host=args.host,
        port=args.port,
    )
    app = create_app(config)
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    if ":" in shown_host:
        shown_host = f"[{shown_host}]"
    url = f"http://{shown_host}:{args.port}/?token={token}"

    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))

    async def serve() -> None:
        task = asyncio.create_task(server.serve())
        while not server.started and not task.done():
            await asyncio.sleep(0.05)
        if server.started:
            print(f"Hemisphere Harness web UI:  {url}")
            print(f"Settings file: {config.env_file}   Run history: {config.data_dir}")
            print("Actions in the UI are simulated. Press Ctrl+C to stop.", flush=True)
            if not args.no_open and not os.environ.get("SSH_CONNECTION"):
                webbrowser.open(url)
        await task

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    if not server.started:
        print(f"Could not start on {args.host}:{args.port}. Is the port in use? Try --port.", file=sys.stderr)
        return 1
    return 0


CHAT_HELP = """Commands:
  /approve <option> [note]   sign off an option the agent stopped on; it carries on from there
  /reject [note]             turn those options down; the agent looks for another way
  /quit                      leave (or Ctrl+D)
Anything else is a message. Actions are simulated, except calls to tools."""


def run_chat(args: argparse.Namespace) -> int:
    settings = Settings(_env_file=args.env_file)
    if not settings.right_model:
        print("Chat needs a right hemisphere: set RIGHT_PROVIDER and RIGHT_MODEL (see .env.example),", file=sys.stderr)
        print("or choose one under Settings in `hemisphere-harness web`.", file=sys.stderr)
        return 2
    try:
        right = build_right(settings)
        left = build_left(settings)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    config = settings.harness_config()
    if isinstance(left, MockLeftHemisphere):
        config.check_answers = False  # the mock would call every claim unsure
    print(f"Talking to {right.name} · {settings.right_model}, with {left.name} as the left hemisphere.")

    async def read() -> str | None:
        try:
            return await asyncio.to_thread(input, "you> ")
        except (EOFError, KeyboardInterrupt):
            return None

    async def session() -> None:
        try:
            toolbox = await toolbox_from_settings(settings.harness_mcp_config)
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            return
        if toolbox is not None:
            free = [t.name for t in toolbox.tools if not t.needs_approval]
            gated = [t.name for t in toolbox.tools if t.needs_approval]
            print(f"Tools: {', '.join(free) or 'none'} run freely; {', '.join(gated) or 'none'} wait for your /approve.")
            for server, error in toolbox.errors.items():
                print(f"  {server} did not start: {error}")
        print(CHAT_HELP + "\n")
        try:
            searcher = searcher_from_settings(settings)
        except ConfigError as exc:
            print(exc, file=sys.stderr)
            searcher = None
        if searcher is not None:
            print(f"Claims the left is unsure of are looked up on {searcher.name}.")
        memory = MemoryStore(settings.harness_memory_file or DEFAULT_MEMORY_FILE) if settings.harness_memory else None
        if memory is not None:
            print(f"Memory is on: {len(memory.notes)} note(s) in {memory.path}.")
        harness = Harness(
            right, left, config=config, on_event=TracePrinter() if args.trace else None,
            toolbox=toolbox, searcher=searcher, memory=memory,
        )  # fmt: skip
        try:
            async with harness:
                await chat_loop(Conversation(harness, context=args.context), read, print)
        finally:
            if toolbox is not None:
                await toolbox.aclose()
            if searcher is not None:
                await searcher.aclose()

    try:
        asyncio.run(session())
    except KeyboardInterrupt:
        pass
    print()
    return 0


async def chat_loop(
    conversation: Conversation, read: Callable[[], Awaitable[str | None]], write: Callable[[str], object]
) -> None:
    """Read messages and commands until the person leaves. Separate from the terminal, for tests."""
    while (line := await read()) is not None:
        line = line.strip()
        if not line:
            continue
        command, _, rest = line.partition(" ")
        try:
            if command == "/quit":
                return
            if command == "/help":
                write(CHAT_HELP)
                continue
            if command == "/approve":
                option, _, note = rest.strip().partition(" ")
                if not option:
                    write("Which option? /approve <option> [note]")
                    continue
                result = await conversation.approve(option, note.strip())
            elif command == "/reject":
                result = await conversation.reject(rest.strip())
            elif command.startswith("/"):
                write(f"Unknown command {command}. /help lists them.")
                continue
            else:
                write("…")
                result = await conversation.send(line)
        except ValueError as exc:
            write(f"Cannot do that: {exc}")
            continue
        except HarnessError as exc:
            write(f"The run failed: {exc}")
            continue
        _show(conversation, result, write)


def _show(conversation: Conversation, result: RunResult, write: Callable[[str], object]) -> None:
    write(f"\nagent> {result.report}\n")
    if result.status is RunStatus.NEEDS_HUMAN and conversation.waiting:
        write("Waiting for your sign-off on one of these:")
        for option in conversation.waiting:
            write(f"  {option.id}: {option.description}")
        write("Reply /approve <option> [note], /reject [note], or just write.\n")
    elif result.status is RunStatus.MAX_STEPS:
        write("(Stopped at the step limit. Reply to let it carry on.)\n")


def run_compare_command(args: argparse.Namespace) -> int:
    from .evals.compare import CompareReport, format_compare, load_tasks, run_compare

    settings = Settings(_env_file=args.env_file)
    if not settings.right_model:
        print("Comparing needs a real right hemisphere: set RIGHT_PROVIDER and RIGHT_MODEL.", file=sys.stderr)
        return 2
    try:
        tasks = load_tasks(args.tasks)
        if args.only:
            tasks = tasks.model_copy(update={"tasks": [t for t in tasks.tasks if t.id in args.only]})
        right, left = build_right(settings), build_left(settings)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Cannot compare: {exc}", file=sys.stderr)
        return 2
    if not tasks.tasks:
        print("No tasks match --only.", file=sys.stderr)
        return 2
    print(f"Running {len(tasks.tasks)} task(s) twice: with the harness, and with the right alone.", flush=True)
    if isinstance(left, MockLeftHemisphere):
        print("Note: the left is the offline mock, so the harness column says nothing about a real left.")
    config = settings.harness_config().model_copy(update={"max_steps": args.max_steps})

    done: list[object] = []

    def progress(run: object) -> None:
        mark = "!" if getattr(run, "error", None) else ("." if getattr(run, "succeeded", False) else "x")
        print(mark, end="", flush=True)
        done.append(run)
        if args.json:  # kept as it goes, so a run cut short still leaves its results
            args.json.write_text(json.dumps([r.model_dump(mode="json") for r in done if hasattr(r, "model_dump")], indent=1), encoding="utf-8")

    async def go() -> object:
        try:
            return await run_compare(right, left, tasks, config, on_run=progress, parallel=args.parallel)
        finally:
            await left.aclose()
            await right.aclose()

    report = asyncio.run(go())
    assert isinstance(report, CompareReport)
    left_model = settings.jev_model if report.left == "jev" else settings.left_model
    names = {"right": f"{report.right} · {settings.right_model}", "left": report.left + (f" · {left_model}" if left_model else "")}
    report = report.model_copy(update=names)
    print("\n")
    print(format_compare(report))
    if args.json:
        args.json.write_text(report.model_dump_json(indent=1), encoding="utf-8")
    return 0


def run_replay_command(args: argparse.Namespace) -> int:
    from .replay import format_replay, replay
    from .schemas import WorldState
    from .trace import TraceEvent

    path = Path(args.run)
    if not path.suffix:
        path = args.data_dir / "runs" / f"{args.run}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not data.get("state"):
            raise ValueError("this run kept no state to replay (it failed or was stopped early)")
        state = WorldState.model_validate(data["state"])
        events = [TraceEvent.model_validate(e) for e in data.get("events", [])]
        config = HarnessConfig.model_validate(data.get("config") or {})
        overrides = {k: v for k, v in (("left_provider", args.left_provider), ("left_model", args.left_model)) if v}
        settings = Settings(_env_file=args.env_file, **overrides)
        left = build_left(settings)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Cannot replay {args.run}: {exc}", file=sys.stderr)
        return 2
    before = next((e.data.get("model") for e in events if e.phase == "decide" and e.data.get("model")), "")
    after = left.name + (f" · {settings.left_model}" if settings.left_model and left.name != "jev" else "")

    async def go() -> object:
        try:
            return await replay(left, state, config=config, events=events, run_id=data.get("id", args.run), left_before=str(before))
        finally:
            await left.aclose()

    from .replay import Replay

    result = asyncio.run(go())
    assert isinstance(result, Replay)
    result = result.model_copy(update={"left_after": after})
    print(format_replay(result))
    if args.json:
        args.json.write_text(result.model_dump_json(indent=1), encoding="utf-8")
    return 0


def run_eval_command(args: argparse.Namespace) -> int:
    from .evals import format_report, load_scenarios, run_eval

    settings = Settings(_env_file=args.env_file)
    try:
        left = build_left(settings)
        if args.signoffs and not args.signoffs.exists():
            print(f"No sign-offs recorded yet in {args.signoffs}. Approve a few in the web UI first.", file=sys.stderr)
            return 1
        scenarios = load_scenarios(args.signoffs or args.scenarios).only(args.domain)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"Cannot run the evaluation: {exc}", file=sys.stderr)
        return 2
    if not scenarios.scenarios:
        print(f"No scenarios match --domain {args.domain}.", file=sys.stderr)
        return 2
    model = settings.jev_model if left.name == "jev" else settings.left_model
    print(f"Evaluating {left.name}{f' · {model}' if model else ''} on {len(scenarios.scenarios)} scenarios.")
    if isinstance(left, MockLeftHemisphere):
        print("Note: the left is the offline mock. Its numbers say nothing about a real model.")

    def progress(result: object) -> None:
        mark = "!" if getattr(result, "error", None) else ("." if getattr(result, "correct", False) else "x")
        print(mark, end="", flush=True)

    async def go() -> object:
        try:
            return await run_eval(left, scenarios, settings.harness_config(), on_result=progress)
        finally:
            await left.aclose()

    report = asyncio.run(go())
    print("\n")
    from .evals import EvalReport

    assert isinstance(report, EvalReport)
    report = report.model_copy(update={"left": left.name + (f" · {model}" if model else "")})
    print(format_report(report))
    if args.json:
        args.json.write_text(report.model_dump_json(indent=1), encoding="utf-8")
        print(f"\nFull report: {args.json}")
    return 0


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
