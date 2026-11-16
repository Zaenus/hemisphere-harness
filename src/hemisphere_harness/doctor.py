"""`hemisphere-harness doctor`: what runs on this machine, what the settings make of it,
and where data would go.

    hemisphere-harness doctor            the report
    hemisphere-harness doctor --check    also make one small real call to each hemisphere
    hemisphere-harness doctor --setup    choose local models and write them to .env
"""

import asyncio
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import envfile, local
from .config import Settings
from .errors import ConfigError, HarnessError
from .left import LeftHemisphere, MockLeftHemisphere
from .lookup import BRAVE_URL
from .mcp_tools import load_mcp_config
from .providers import build_left, build_right, endpoint
from .right import LLMRightHemisphere, RightHemisphere
from .schemas import ChoiceAnswer, ChoiceQuestion

Write = Callable[[str], object]
Ask = Callable[[str], str]

SERVER_NAMES = {"ollama": "Ollama", "lmstudio": "LM Studio", "llamacpp": "llama.cpp", "vllm": "vLLM"}
SUGGESTED_LEFT = "tev1"
"""The decision model docs/evaluation.md measured as a sound left hemisphere."""


@dataclass
class Destination:
    what: str
    url: str

    @property
    def local(self) -> bool:
        return local.is_local_url(self.url)


@dataclass
class Report:
    found: list[local.Found]
    right: RightHemisphere | None = None
    right_problem: str | None = None
    left: LeftHemisphere | None = None
    left_problem: str | None = None
    destinations: list[Destination] | None = None
    other_problems: list[str] | None = None

    def chat_models(self) -> list[tuple[str, local.LocalModel]]:
        return [(f.server.provider, m) for f in self.found if f.up for m in f.models if m.chat]

    def decision_models(self) -> list[local.LocalModel]:
        models = [m for f in self.found if f.up and f.server.ollama for m in f.models if m.decision]
        return sorted(models, key=lambda m: -(m.size or 0))

    @property
    def ollama(self) -> local.Found | None:
        return next((f for f in self.found if f.server.ollama), None)


def gather(settings: Settings) -> Report:
    """Look at the machine and build what the settings name. Makes no call to a model."""
    report = Report(found=local.scan())
    if settings.right_model:
        try:
            report.right = build_right(settings)
        except ConfigError as exc:
            report.right_problem = str(exc)
    else:
        report.right_problem = "No model is chosen: set RIGHT_MODEL."
    try:
        report.left = build_left(settings)
    except ConfigError as exc:
        report.left_problem = str(exc)
    report.destinations, report.other_problems = _destinations(settings, report)
    return report


def _destinations(settings: Settings, report: Report) -> tuple[list[Destination], list[str]]:
    """Every endpoint a run with these settings would send data to."""
    out: list[Destination] = []
    problems: list[str] = []
    for what, hemisphere in (("the right hemisphere", report.right), ("the left hemisphere", report.left)):
        url = endpoint(hemisphere) if hemisphere is not None else None
        if url:
            out.append(Destination(what, url))
    lookup = (settings.harness_lookup or "").strip().lower()
    lookup_urls = {
        "wikipedia": f"https://{settings.harness_lookup_language or 'en'}.wikipedia.org",
        "brave": BRAVE_URL,
        "searxng": settings.harness_lookup_url or "",
    }
    if lookup_urls.get(lookup):
        out.append(Destination(f"lookups ({lookup})", lookup_urls[lookup]))
    if settings.harness_notify_url:
        out.append(Destination("the notification webhook", settings.harness_notify_url))
    if settings.harness_mcp_config:
        try:
            servers = load_mcp_config(settings.harness_mcp_config).servers
        except ConfigError as exc:
            problems.append(str(exc))
        else:
            out += [Destination(f"the MCP server '{name}'", s.url) for name, s in servers.items() if s.url and not s.disabled]
    return out, problems


def print_report(report: Report, settings: Settings, env_file: Path, write: Write = print) -> None:
    write("On this machine")
    for found in report.found:
        name = SERVER_NAMES.get(found.server.provider, found.server.provider)
        if not found.up:
            write(f"  {name:<10} {found.server.url:<26} {found.problem}")
            continue
        write(f"  {name:<10} {found.server.url:<26} {len(found.models)} model{'s' if len(found.models) != 1 else ''}")
        decision = [m for m in found.models if m.decision]
        chat = [m for m in found.models if m.chat]
        if decision:
            write(f"               decision  {_names(decision)}")
        if chat:
            write(f"               chat      {_names(chat)}")
    write("")

    write(f"As configured  ({env_file if env_file.exists() else 'no .env file; defaults and the environment'})")
    write("  right   " + _describe(report.right, settings.right_model, report.right_problem))
    write("  left    " + _describe(report.left, _left_model(report.left, settings), report.left_problem))
    for problem in report.other_problems or []:
        write(f"  ✗ {problem}")
    write("")

    leaving = [d for d in report.destinations or [] if not d.local]
    if leaving:
        write("Data leaves this machine and its network, to:")
        for d in leaving:
            write(f"  {d.what}: {d.url}")
    elif report.destinations:
        write("Everything stays on this machine or its network.")
    else:
        write("Nothing is sent anywhere: both hemispheres are offline stand-ins.")
    if settings.harness_local_only:
        write("HARNESS_LOCAL_ONLY is on: anything that is not local is refused.")
    elif not leaving and report.destinations:
        write("Turn HARNESS_LOCAL_ONLY on to make that a rule rather than how things happen to be set.")
    write("")

    steps = next_steps(report, settings)
    if steps:
        write("Next")
        for step in steps:
            write(f"  - {step}")


def next_steps(report: Report, settings: Settings) -> list[str]:
    steps: list[str] = []
    ollama = report.ollama
    if report.right is None:
        chat = report.chat_models()
        if chat:
            steps.append(
                "Choose a right hemisphere: `hemisphere-harness doctor --setup`, or set RIGHT_MODEL to one of "
                + ", ".join(m.id for _, m in chat[:5])
            )
        elif ollama is not None and ollama.up:
            steps.append("Pull a chat model for the right hemisphere, for example `ollama pull gemma4`, then run --setup.")
        else:
            steps.append(
                "No local model server is running. Install Ollama (https://ollama.com) for a local right hemisphere, "
                "or set RIGHT_PROVIDER to a hosted one (`hemisphere-harness providers` lists them)."
            )
    if isinstance(report.left, MockLeftHemisphere) and not settings.left_provider.strip().lower() == "mock":
        if ollama is not None and ollama.up:
            steps.append(
                f"The left hemisphere is the offline mock. `ollama pull {SUGGESTED_LEFT}` (4.6 GB) gives you the "
                "decision model measured in docs/evaluation.md; `auto` picks it up."
            )
        else:
            steps.append("The left hemisphere is the offline mock. With Ollama running, `ollama pull tev1` gives you a real one.")
    if report.right is not None and report.left is not None and not settings.harness_local_only:
        if all(d.local for d in report.destinations or []) and report.destinations:
            steps.append("Set HARNESS_LOCAL_ONLY=true to keep it that way.")
    return steps


# --- --check -------------------------------------------------------------------------------


async def check(report: Report, write: Write = print) -> bool:
    """One small real call to each hemisphere. True if both answered."""
    ok = True
    if report.right is not None:
        started = time.perf_counter()
        try:
            if isinstance(report.right, LLMRightHemisphere):
                reply = await report.right.complete([{"role": "user", "content": "Reply with the single word: ready"}])
                write(f"  ✓ right replied “{reply.strip()[:40]}” in {_ms(started)}")
            else:
                write(f"  · right ({report.right.name}) has no simple check")
        except HarnessError as exc:
            ok = False
            write(f"  ✗ right: {exc}")
    if report.left is not None:
        started = time.perf_counter()
        try:
            result = await report.left.decide(
                {"situation": "A shopper asks which of two items is a fruit."},
                {"select": ChoiceQuestion(instructions="Which item is a fruit?", options={"apple": "an apple", "hammer": "a hammer"})},
            )
            answer = result.answers["select"]
            assert isinstance(answer, ChoiceAnswer)  # decide() checked it against the question
            write(f"  ✓ left picked “{answer.choice}” (p={answer.probability:.2f}) in {_ms(started)}")
        except HarnessError as exc:
            ok = False
            write(f"  ✗ left: {exc}")
    return ok


# --- --setup -------------------------------------------------------------------------------


def setup(report: Report, settings: Settings, env_file: Path, ask: Ask = input, write: Write = print) -> dict[str, str]:
    """Offer the local models, one hemisphere at a time, and write the choices. Returns them."""
    changes: dict[str, str] = {}

    chat = report.chat_models()
    if chat:
        write("Right hemisphere: the model that plans, proposes and writes. Prefer the strongest you can run.")
        picked = _pick([f"{m.id}  ({SERVER_NAMES.get(server, server)}{_size(m)})" for server, m in chat], settings.right_model, [m.id for _, m in chat], ask, write)
        if picked is not None:
            changes.update(RIGHT_PROVIDER="auto", RIGHT_MODEL=chat[picked][1].id)
    else:
        write("No local chat model was found for the right hemisphere.")

    decision = report.decision_models()
    if decision:
        write("\nLeft hemisphere: a decision model.")
        picked = _pick([f"{m.id}{_size(m)}" for m in decision], settings.left_model, [m.id for m in decision], ask, write)
        if picked is not None:
            changes.update(LEFT_PROVIDER="auto", LEFT_MODEL=decision[picked].id)
    else:
        write(f"\nNo local decision model was found. `ollama pull {SUGGESTED_LEFT}`, then run --setup again.")

    if changes and not settings.harness_local_only:
        answer = ask("\nKeep everything on this machine (HARNESS_LOCAL_ONLY=true)? [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            changes["HARNESS_LOCAL_ONLY"] = "true"

    if changes:
        envfile.write(env_file, dict(changes))
        write(f"\nWrote to {env_file}: " + ", ".join(f"{k}={v}" for k, v in changes.items()))
    else:
        write("\nNothing changed.")
    return changes


def _pick(labels: list[str], current: str | None, ids: list[str], ask: Ask, write: Write) -> int | None:
    for number, label in enumerate(labels, 1):
        mark = "  (current)" if current and local.same_model(current, ids[number - 1]) else ""
        write(f"  {number}. {label}{mark}")
    while True:
        answer = ask(f"Choose 1-{len(labels)}, or press Enter to leave it as it is: ").strip()
        if not answer:
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(labels):
            return int(answer) - 1
        write("  That is not one of the numbers above.")


# --- The command -----------------------------------------------------------------------------


def run(env_file: Path, *, do_check: bool = False, do_setup: bool = False) -> int:
    settings = Settings(_env_file=env_file)
    report = gather(settings)
    if do_setup:
        if not sys.stdin.isatty():
            print("--setup asks questions, so it needs a terminal.", file=sys.stderr)
            return 2
        try:
            changes = setup(report, settings, env_file)
        except ConfigError as exc:
            print(f"Not saved: {exc}", file=sys.stderr)
            _close(report)
            return 1
        if changes:
            _close(report)
            settings = Settings(_env_file=env_file)
            report = gather(settings)
        print()
    print_report(report, settings, env_file)
    healthy = report.right_problem is None and report.left_problem is None and not report.other_problems
    if do_check:
        print("\nChecking each hemisphere with one small call")
        healthy = asyncio.run(_check_and_close(report)) and healthy
    else:
        _close(report)
    return 0 if healthy else 1


async def _check_and_close(report: Report) -> bool:
    try:
        return await check(report)
    finally:
        for hemisphere in (report.right, report.left):
            if hemisphere is not None:
                await hemisphere.aclose()


def _close(report: Report) -> None:
    async def close() -> None:
        for hemisphere in (report.right, report.left):
            if hemisphere is not None:
                await hemisphere.aclose()

    asyncio.run(close())


def _describe(hemisphere: RightHemisphere | LeftHemisphere | None, model: str | None, problem: str | None) -> str:
    if hemisphere is None:
        return f"✗ {problem}"
    if isinstance(hemisphere, MockLeftHemisphere):
        return "mock · offline stand-in, not calibrated"
    url = endpoint(hemisphere)
    where = "" if url is None else f"   {url}" + ("" if local.is_local_url(url) else "   (hosted)")
    return f"✓ {hemisphere.name}" + (f" · {model}" if model else "") + where


def _left_model(left: LeftHemisphere | None, settings: Settings) -> str | None:
    model = getattr(left, "model", None)
    return model if isinstance(model, str) else settings.left_model


def _names(models: list[local.LocalModel]) -> str:
    shown = ", ".join(f"{m.id}{_size(m)}" for m in models[:6])
    return shown + (f", and {len(models) - 6} more" if len(models) > 6 else "")


def _size(model: local.LocalModel) -> str:
    return f" · {model.size / 1e9:.1f} GB" if model.size else ""


def _ms(started: float) -> str:
    return f"{(time.perf_counter() - started) * 1000:.0f} ms"
