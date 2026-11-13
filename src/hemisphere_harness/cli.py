"""The command line.

    hemisphere-harness               list the providers available here
    hemisphere-harness providers     the same
    hemisphere-harness web           start the web UI and open it in the browser

`python -m hemisphere_harness` is the same command.
"""

import argparse
import asyncio
import ipaddress
import os
import secrets
import sys
import webbrowser
from pathlib import Path

from .providers import left_providers, right_providers

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
    args = parser.parse_args(argv)

    if args.command == "web":
        return run_web(args)
    list_providers()
    return 0


def run_web(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .web import WebConfig, create_app
    except ImportError:
        print('The web UI needs its extra dependencies:  pip install "hemisphere-harness[web]"', file=sys.stderr)
        return 2

    if not _is_loopback(args.host):
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


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
