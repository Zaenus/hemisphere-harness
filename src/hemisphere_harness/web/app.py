"""The web UI's server: a JSON API, a live event stream per run, and the static page.

Security, for a server that holds API keys and can spend money on them:

- It listens on 127.0.0.1 by default and, while it does, refuses any request whose Host
  header is not a loopback name. That stops DNS-rebinding pages from reaching it.
- Every API call needs the launch token, which the browser receives once through the
  link printed at startup and then keeps in a SameSite=Strict, HttpOnly cookie.
- Requests that change anything must come from the page's own origin.
- API keys are write-only: no response ever contains one.
- Model output is untrusted text. The page renders it as text only, under a strict
  Content-Security-Policy.
"""

import asyncio
import hmac
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, MutableMapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings
from ..errors import ConfigError, HarnessError
from ..left import ExperimentalLLMLeft, MockLeftHemisphere
from ..providers import OLLAMA, build_left, build_right, left_providers, right_providers
from ..right import LLMRightHemisphere
from ..schemas import ChoiceAnswer, ChoiceQuestion
from . import envfile
from .runs import RunManager, RunRequest, RunStore

STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


@dataclass
class WebConfig:
    env_file: Path
    data_dir: Path
    token: str
    host: str = "127.0.0.1"
    port: int = 8765
    transport: httpx.AsyncBaseTransport | None = None
    """For tests: where outbound HTTP (model listing) goes."""

    @property
    def cookie(self) -> str:
        return f"hh_token_{self.port}"


class Guard:
    """Host check, token check, same-origin check and security headers, around the whole app."""

    def __init__(self, app: ASGIApp, config: WebConfig) -> None:
        self.app = app
        self.config = config
        self.loopback_only = config.host in LOOPBACK

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        problem = self._check(request)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend(
                    [
                        (b"content-security-policy", CSP.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                        (b"cache-control", b"no-store"),
                    ]
                )
            await send(message)

        if problem is not None:
            await problem(scope, receive, send_with_headers)
            return
        await self.app(scope, receive, send_with_headers)

    def _check(self, request: Request) -> Response | None:
        host = urlsplit(f"//{request.headers.get('host', '')}").hostname or ""
        if self.loopback_only and host not in LOOPBACK:
            return PlainTextResponse("This server only answers on its loopback address.", status_code=421)

        path = request.url.path
        if path.startswith("/static/") or path == "/favicon.svg":
            return None

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"error": "Cross-origin request refused."}, status_code=403)

        if path == "/" or path.startswith("/api/"):
            if self._authorised(request):
                return None
            if path == "/" and self._token_matches(request.query_params.get("token")):
                return None  # the index handler sets the cookie and drops the token from the URL
            if path == "/":
                return FileResponse(STATIC / "locked.html", status_code=401)
            return JSONResponse({"error": "Open the link printed in the terminal to sign in."}, status_code=401)
        return None

    def _authorised(self, request: Request) -> bool:
        if self._token_matches(request.cookies.get(self.config.cookie)):
            return True
        header = request.headers.get("authorization", "")
        return header.startswith("Bearer ") and self._token_matches(header.removeprefix("Bearer "))

    def _token_matches(self, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(candidate.encode(), self.config.token.encode())


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def create_app(config: WebConfig) -> Starlette:
    store = RunStore(config.data_dir / "runs")

    def settings() -> Settings:
        return Settings(_env_file=config.env_file)

    manager = RunManager(store, settings)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        yield
        await manager.shutdown()

    def endpoint(handler: Callable[[Request], Awaitable[Any]]) -> Callable[[Request], Awaitable[Response]]:
        async def wrapped(request: Request) -> Response:
            try:
                result = await handler(request)
            except ApiError as exc:
                return JSONResponse({"error": str(exc)}, status_code=exc.status)
            except ConfigError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            except ValidationError as exc:
                message = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
                return JSONResponse({"error": message}, status_code=422)
            return result if isinstance(result, Response) else JSONResponse(result)

        return wrapped

    async def body(request: Request, model: type[BaseModel] | None = None) -> Any:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError("The request body must be JSON.") from None
        return model.model_validate(data) if model else data

    # --- Pages ---------------------------------------------------------------------------

    async def index(request: Request) -> Response:
        if "token" in request.query_params:
            response: Response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                config.cookie, config.token, httponly=True, samesite="strict", secure=False, path="/"
            )
            return response
        return FileResponse(STATIC / "index.html")

    # --- Status and settings ---------------------------------------------------------------

    async def status(request: Request) -> dict[str, Any]:
        current = settings()
        right: dict[str, Any] = {"provider": current.right_provider, "model": current.right_model}
        left: dict[str, Any] = {"provider": current.left_provider}
        if current.right_model:
            try:
                built = build_right(current)
                right.update(ready=True, label=f"{built.name} · {current.right_model}")
                await built.aclose()
            except ConfigError as exc:
                right.update(ready=False, problem=str(exc))
        else:
            right.update(ready=False, problem="No model is chosen.")
        try:
            built_left = build_left(current)
            model = current.jev_model if built_left.name == "jev" else current.left_model
            left.update(
                ready=True,
                label=built_left.name + (f" · {model}" if model and not isinstance(built_left, MockLeftHemisphere) else ""),
                mock=isinstance(built_left, MockLeftHemisphere),
                experimental=isinstance(built_left, ExperimentalLLMLeft),
            )
            await built_left.aclose()
        except ConfigError as exc:
            left.update(ready=False, problem=str(exc))
        return {
            "version": __version__,
            "env_file": str(config.env_file),
            "right": right,
            "left": left,
            "policy": current.harness_policy,
            "max_steps": current.harness_max_steps,
        }

    async def get_settings(request: Request) -> dict[str, Any]:
        values = await asyncio.to_thread(envfile.read, config.env_file)

        def describe(registry: Any) -> list[dict[str, Any]]:
            return [
                {
                    "name": p.name,
                    "aliases": list(p.aliases),
                    "summary": p.summary,
                    "base_url": p.base_url,
                    "api_key_env": p.api_key_env,
                    "settings": list(p.settings),
                    "experimental": p.experimental,
                }
                for p in registry.providers()
            ] + [{"name": name, "summary": "plugin", "plugin": True, "settings": []} for name in registry.plugin_names()]

        return {
            "env_file": str(config.env_file),
            "fields": [
                {"name": f.name, "label": f.label, "kind": f.kind, "help": f.help, **values[f.name]}
                for f in envfile.FIELDS
            ],
            "right_providers": describe(right_providers),
            "left_providers": describe(left_providers),
        }

    async def put_settings(request: Request) -> dict[str, Any]:
        changes = await body(request)
        if not isinstance(changes, dict) or not all(
            isinstance(k, str) and (v is None or isinstance(v, str | int | float | bool)) for k, v in changes.items()
        ):
            raise ApiError("Send an object of variable names to values.")
        normalised = {k: None if v is None else (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in changes.items()}
        await asyncio.to_thread(envfile.write, config.env_file, normalised)
        return await get_settings(request)

    # --- Models and connection checks ------------------------------------------------------

    class ModelsRequest(BaseModel):
        hemisphere: str
        provider: str
        base_url: str | None = None
        api_key: str | None = None

    async def list_models(request: Request) -> dict[str, Any]:
        req: ModelsRequest = await body(request, ModelsRequest)
        current = settings()
        async with httpx.AsyncClient(timeout=8.0, transport=config.transport) as client:
            try:
                if req.hemisphere == "right":
                    return {"models": await _right_models(client, current, req.provider, req.base_url, req.api_key)}
                if req.hemisphere == "left":
                    return {"models": await _left_models(client, current, req.provider, req.base_url, req.api_key)}
            except httpx.HTTPStatusError as exc:
                raise ApiError(f"The server answered {exc.response.status_code} at {exc.request.url}.", 502) from None
            except httpx.HTTPError as exc:
                raise ApiError(f"Could not reach {exc.request.url}: {type(exc).__name__}.", 502) from None
        raise ApiError("hemisphere must be 'right' or 'left'.")

    async def check(request: Request) -> dict[str, Any]:
        """Make one small real call to a hemisphere, as configured now."""
        hemisphere = (await body(request)).get("hemisphere")
        current = settings()
        started = time.perf_counter()
        if hemisphere == "right":
            if not current.right_model:
                raise ApiError("Choose a model first.")
            right = build_right(current)
            try:
                if not isinstance(right, LLMRightHemisphere):
                    return {"ok": True, "message": f"{right.name} is built; it has no simple check."}
                reply = await right.complete([{"role": "user", "content": "Reply with the single word: ready"}])
            except HarnessError as exc:
                return {"ok": False, "message": str(exc)}
            finally:
                await right.aclose()
            ms = (time.perf_counter() - started) * 1000
            return {"ok": True, "message": f"{current.right_model} replied “{reply.strip()[:60]}” in {ms:.0f} ms."}
        if hemisphere == "left":
            left = build_left(current)
            try:
                result = await left.decide(
                    {"situation": "A shopper asks which of two items is a fruit."},
                    {"select": ChoiceQuestion(instructions="Which item is a fruit?", options={"apple": "an apple", "hammer": "a hammer"})},
                )
            except HarnessError as exc:
                return {"ok": False, "message": str(exc)}
            finally:
                await left.aclose()
            ms = (time.perf_counter() - started) * 1000
            answer = result.answers["select"]
            assert isinstance(answer, ChoiceAnswer)
            note = " (mock: not a real model)" if isinstance(left, MockLeftHemisphere) else ""
            return {
                "ok": True,
                "message": f"{result.model or left.name} picked “{answer.choice}” with p={answer.probability:.2f} in {ms:.0f} ms{note}. The right answer is “apple”.",
            }
        raise ApiError("hemisphere must be 'right' or 'left'.")

    # --- Runs --------------------------------------------------------------------------------

    def find(request: Request) -> Any:
        run = store.runs.get(request.path_params["run_id"])
        if run is None:
            raise ApiError("No such run.", 404)
        return run

    async def list_runs(request: Request) -> dict[str, Any]:
        return {"runs": [run.summary() for run in store.newest_first()]}

    async def start_run(request: Request) -> Response:
        run = manager.start(await body(request, RunRequest))
        return JSONResponse(run.model_dump(mode="json"), status_code=201)

    async def get_run(request: Request) -> dict[str, Any]:
        result: dict[str, Any] = find(request).model_dump(mode="json")
        return result

    async def cancel_run(request: Request) -> dict[str, Any]:
        run = find(request)
        return {"cancelled": manager.cancel(run.id)}

    async def delete_run(request: Request) -> dict[str, Any]:
        run = find(request)
        if manager.is_running(run.id):
            raise ApiError("Stop the run before deleting it.", 409)
        store.delete(run.id)
        return {"deleted": run.id}

    async def run_events(request: Request) -> Response:
        run = find(request)
        try:
            after = int(request.headers.get("last-event-id", request.query_params.get("after", "-1")))
        except ValueError:
            after = -1

        async def stream() -> AsyncIterator[str]:
            yield "retry: 2000\n\n"
            async for index, event in manager.events(run.id, after):
                yield f"id: {index}\nevent: trace\ndata: {event.model_dump_json()}\n\n"
            final = store.runs.get(run.id)
            if final is not None:
                summary = final.model_dump(mode="json", include={"id", "status", "error", "report", "finished"})
                yield f"event: end\ndata: {json.dumps(summary)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"x-accel-buffering": "no"})

    routes = [
        Route("/", endpoint(index)),
        Route("/favicon.svg", lambda request: FileResponse(STATIC / "favicon.svg")),
        Route("/api/status", endpoint(status)),
        Route("/api/settings", endpoint(get_settings), methods=["GET"]),
        Route("/api/settings", endpoint(put_settings), methods=["PUT"]),
        Route("/api/models", endpoint(list_models), methods=["POST"]),
        Route("/api/check", endpoint(check), methods=["POST"]),
        Route("/api/runs", endpoint(list_runs), methods=["GET"]),
        Route("/api/runs", endpoint(start_run), methods=["POST"]),
        Route("/api/runs/{run_id}", endpoint(get_run), methods=["GET"]),
        Route("/api/runs/{run_id}", endpoint(delete_run), methods=["DELETE"]),
        Route("/api/runs/{run_id}/cancel", endpoint(cancel_run), methods=["POST"]),
        Route("/api/runs/{run_id}/events", endpoint(run_events), methods=["GET"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    app = Starlette(routes=routes, lifespan=lifespan, middleware=[Middleware(Guard, config=config)])
    app.state.manager = manager
    app.state.config = config
    return app


# --- Model listing -----------------------------------------------------------------------


async def _openai_models(client: httpx.AsyncClient, base_url: str, api_key: str | None) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response = await client.get(base_url.rstrip("/") + "/models", headers=headers)
    response.raise_for_status()
    data = response.json().get("data", [])
    return [{"id": item["id"]} for item in sorted(data, key=lambda item: str(item.get("id"))) if item.get("id")]


async def _right_models(
    client: httpx.AsyncClient, settings: Settings, provider_name: str, base_url: str | None, api_key: str | None
) -> list[dict[str, Any]]:
    provider = right_providers.get(provider_name)
    home = provider.base_url
    url = base_url or settings.right_base_url or home
    if not url:
        raise ApiError("This provider has no endpoint of its own. Enter a base URL.")
    # The same rule as building the hemisphere: a provider's own key goes only to its own endpoint.
    key = api_key or (settings.right_api_key.get_secret_value() if settings.right_api_key else None)
    if key is None and provider.api_key_env and home and url.rstrip("/") == home.rstrip("/"):
        key = settings.env(provider.api_key_env)
    return await _openai_models(client, url, key)


async def _left_models(
    client: httpx.AsyncClient, settings: Settings, provider_name: str, base_url: str | None, api_key: str | None
) -> list[dict[str, Any]]:
    provider = left_providers.get(provider_name)
    key = api_key or (settings.left_api_key.get_secret_value() if settings.left_api_key else None)
    if provider.name == "ollama":
        root = (base_url or settings.left_base_url or OLLAMA).rstrip("/").removesuffix("/v1")
        tags = await client.get(f"{root}/api/tags")
        tags.raise_for_status()
        names = sorted(model["name"] for model in tags.json().get("models", []))

        async def describe(name: str) -> dict[str, Any]:
            try:
                shown = await client.post(f"{root}/api/show", json={"model": name})
                shown.raise_for_status()
                decision = "decision" in shown.json().get("capabilities", [])
            except httpx.HTTPError:
                decision = False
            return {"id": name, "decision": decision}

        models = await asyncio.gather(*(describe(name) for name in names))
        return sorted(models, key=lambda m: (not m["decision"], m["id"]))
    if provider.name == "llm-experimental":
        return await _openai_models(client, base_url or settings.left_base_url or f"{OLLAMA}/v1", key)
    if provider.name == "systemone":
        url = base_url or settings.left_base_url
        if not url:
            raise ApiError("Enter the server's base URL first.")
        return await _openai_models(client, url.rstrip("/").removesuffix("/v1") + "/v1", key)
    raise ApiError(f"Listing models is not available for '{provider.name}'. Type the model id.")
