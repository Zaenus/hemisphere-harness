"""Tools from MCP servers. Needs the "mcp" extra: pip install "hemisphere-harness[mcp]".

The config file uses the same `mcpServers` shape as other MCP clients, plus two optional
keys per server that this harness understands:

    {
      "mcpServers": {
        "files": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"]
        },
        "search": {
          "url": "https://example.com/mcp",
          "headers": {"Authorization": "Bearer ..."},
          "approval": "never",
          "tools": ["web_search"]
        }
      }
    }

`approval` is when the server's tools need a person's sign-off: "unless_read_only" (the
default: tools the server marks read-only run freely), "always" or "never". `tools`, if
given, is the list of the server's tools to offer; the rest are hidden. Tools are named
"<server>.<tool>".

A server is a program on your machine or a service you trust with whatever its tools can
reach. Only add servers you would run yourself.
"""

import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, TextIO

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import local
from .errors import ConfigError
from .tools import Approval, Toolbox, ToolResult, ToolSpec, needs_approval

log = logging.getLogger(__name__)

CALL_TIMEOUT_S = 120.0


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    approval: Approval = "unless_read_only"
    tools: list[str] | None = None
    disabled: bool = False

    @model_validator(mode="after")
    def _one_transport(self) -> "MCPServerConfig":
        if bool(self.command) == bool(self.url):
            raise ValueError("give either a command (a local server) or a url (a remote one)")
        return self


class MCPConfig(BaseModel):
    servers: dict[str, MCPServerConfig] = Field(default_factory=dict, alias="mcpServers")


def load_mcp_config(path: str | os.PathLike[str]) -> MCPConfig:
    """Read and validate a config file. Raises ConfigError with the reason."""
    file = Path(path).expanduser()
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"MCP config file not found: {file}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"MCP config {file} is not valid JSON: {exc}") from None
    try:
        return MCPConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise ConfigError(f"MCP config {file} is not valid: {problems}") from None


async def toolbox_from_settings(
    path: str | None, *, log_dir: Path | None = None, local_only: bool = False
) -> "MCPToolbox | None":
    """Connect the servers in HARNESS_MCP_CONFIG, or return None if it is not set.

    With `local_only` (HARNESS_LOCAL_ONLY), a server reached by a URL off this machine and
    its network is skipped and recorded in `errors`, like a server that failed.
    """
    if not path:
        return None
    return await MCPToolbox.connect(load_mcp_config(path), log_dir=log_dir, local_only=local_only)


class MCPToolbox(Toolbox):
    """Connected MCP servers and their tools. Create with `await MCPToolbox.connect(config)`."""

    def __init__(self) -> None:
        self.tools = []
        self.errors: dict[str, str] = {}
        """Servers that could not be reached, and why. The others still work."""
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}
        self._targets: dict[str, tuple[str, str]] = {}  # tool name -> (server, tool on that server)

    @classmethod
    async def connect(cls, config: MCPConfig, *, log_dir: Path | None = None, local_only: bool = False) -> "MCPToolbox":
        """Start or reach every enabled server and list its tools.

        A server that fails is recorded in `errors` and skipped. Local servers' own error
        output goes to `<log_dir>/mcp-<server>.log`, or nowhere without a log_dir.
        """
        try:
            from mcp import ClientSession  # noqa: F401  (fail early, with a clear message)
        except ImportError:
            raise ConfigError('MCP tools need the "mcp" extra: pip install "hemisphere-harness[mcp]"') from None
        toolbox = cls()
        for name, server in config.servers.items():
            if server.disabled:
                continue
            try:
                if local_only and server.url:
                    local.require_local(f"the MCP server '{name}'", server.url)
                await toolbox._open(name, server, log_dir)
            except Exception as exc:  # one bad server must not take the others down
                message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("MCP server %s failed: %s", name, message)
                toolbox.errors[name] = message
        return toolbox

    async def _open(self, name: str, server: MCPServerConfig, log_dir: Path | None) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        try:
            if server.command:
                errlog: TextIO
                if log_dir is not None:
                    log_dir.mkdir(parents=True, exist_ok=True)
                    errlog = stack.enter_context(open(log_dir / f"mcp-{name}.log", "a", encoding="utf-8"))
                else:
                    errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
                parameters = StdioServerParameters(command=server.command, args=server.args, env=server.env, cwd=server.cwd)
                read, write = await stack.enter_async_context(stdio_client(parameters, errlog=errlog))
            else:
                import httpx2
                from mcp.client.streamable_http import streamable_http_client

                client = await stack.enter_async_context(httpx2.AsyncClient(headers=server.headers, timeout=CALL_TIMEOUT_S))
                streams = await stack.enter_async_context(streamable_http_client(server.url or "", http_client=client))
                read, write = streams[0], streams[1]
            session = await stack.enter_async_context(ClientSession(read, write, read_timeout_seconds=CALL_TIMEOUT_S))
            await session.initialize()
            listed = await session.list_tools()
        except BaseException:
            await stack.aclose()
            raise
        await self._stack.enter_async_context(stack)
        self._sessions[name] = session

        for tool in listed.tools:
            if server.tools is not None and tool.name not in server.tools:
                continue
            read_only = bool(tool.annotations and tool.annotations.read_only_hint)
            qualified = f"{name}.{tool.name}"
            self._targets[qualified] = (name, tool.name)
            self.tools.append(
                ToolSpec(
                    name=qualified,
                    description=tool.description or (tool.annotations.title if tool.annotations and tool.annotations.title else ""),
                    input_schema=dict(tool.input_schema or {}),
                    source=name,
                    read_only=read_only,
                    needs_approval=needs_approval(server.approval, read_only),
                )
            )

    async def _call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        server, tool = self._targets[name]
        result = await self._sessions[server].call_tool(tool, arguments or {}, read_timeout_seconds=CALL_TIMEOUT_S)
        parts: list[str] = []
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            parts.append(text if isinstance(text, str) else f"[{getattr(item, 'type', 'content')} not shown]")
        structured = getattr(result, "structured_content", None)
        if not parts and structured is not None:
            parts.append(json.dumps(structured))
        return ToolResult(ok=not getattr(result, "is_error", False), output="\n".join(parts) or "(no output)")

    async def aclose(self) -> None:
        self._sessions.clear()
        try:
            await self._stack.aclose()
        except Exception as exc:  # a server that dies on the way out is not worth a crash
            log.warning("closing MCP servers: %s", exc)
