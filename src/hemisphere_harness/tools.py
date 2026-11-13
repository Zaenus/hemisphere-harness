"""Tools: real actions the agent can take.

A toolbox lists tools and calls them. The right sees the list and may make any option a
call to one of them; the left chooses among options as usual; the harness carries out a
committed option by calling its tool. Two toolboxes ship:

    FunctionToolbox   plain Python functions, for your own code and for tests
    MCPToolbox        tools from MCP servers (see mcp_tools.py; needs the "mcp" extra)

Every tool says whether it needs a person's sign-off. One that does is never carried out
by the hemispheres alone: the harness stops for a human, exactly as for an option the
policy gate refused.
"""

import inspect
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, create_model

Approval = Literal["always", "never", "unless_read_only"]
"""When a tool needs a person's sign-off. The default, unless_read_only, lets tools that
only read run freely and stops for anything that writes, sends or deletes."""

MAX_OUTPUT_CHARS = 8_000


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    source: str = ""
    """Where the tool comes from: an MCP server's name, or "python"."""
    read_only: bool = False
    needs_approval: bool = True


class ToolResult(BaseModel):
    ok: bool
    output: str


def needs_approval(approval: Approval, read_only: bool) -> bool:
    if approval == "always":
        return True
    if approval == "never":
        return False
    return not read_only


class Toolbox(ABC):
    """A set of tools. Subclasses list them in `tools` and implement `_call`."""

    tools: list[ToolSpec]

    def get(self, name: str | None) -> ToolSpec | None:
        return next((tool for tool in self.tools if tool.name == name), None)

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Call a tool. Never raises for the tool's own failure: that is a result too."""
        if self.get(name) is None:
            known = ", ".join(tool.name for tool in self.tools) or "none"
            return ToolResult(ok=False, output=f"there is no tool named '{name}' (tools: {known})")
        try:
            result = await self._call(name, arguments)
        except Exception as exc:  # a failed tool is information for the right, not a crash
            return ToolResult(ok=False, output=f"{type(exc).__name__}: {exc}")
        if len(result.output) > MAX_OUTPUT_CHARS:
            cut = len(result.output) - MAX_OUTPUT_CHARS
            result = result.model_copy(update={"output": result.output[:MAX_OUTPUT_CHARS] + f"\n[… {cut} more characters]"})
        return result

    @abstractmethod
    async def _call(self, name: str, arguments: dict[str, Any]) -> ToolResult: ...

    async def aclose(self) -> None:
        """Disconnect from whatever the tools run on."""


ToolFunction = Callable[..., Any]


class FunctionToolbox(Toolbox):
    """Tools that are Python functions, sync or async. The input schema comes from the signature.

        toolbox = FunctionToolbox()

        @toolbox.tool(read_only=True)
        def lookup_order(order_id: str) -> str:
            "Find an order by its id."
            ...
    """

    def __init__(self, *, approval: Approval = "unless_read_only") -> None:
        self.tools = []
        self._approval = approval
        self._functions: dict[str, ToolFunction] = {}
        self._models: dict[str, type[BaseModel]] = {}

    def tool(
        self, name: str | None = None, *, description: str | None = None, read_only: bool = False
    ) -> Callable[[ToolFunction], ToolFunction]:
        def register(function: ToolFunction) -> ToolFunction:
            self.add(function, name=name, description=description, read_only=read_only)
            return function

        return register

    def add(
        self, function: ToolFunction, *, name: str | None = None, description: str | None = None, read_only: bool = False
    ) -> None:
        tool_name = name or function.__name__
        fields: dict[str, Any] = {}
        for parameter in inspect.signature(function).parameters.values():
            annotation = parameter.annotation if parameter.annotation is not inspect.Parameter.empty else Any
            default = parameter.default if parameter.default is not inspect.Parameter.empty else ...
            fields[parameter.name] = (annotation, default)
        model = create_model(f"{tool_name}_arguments", **fields)
        self._functions[tool_name] = function
        self._models[tool_name] = model
        self.tools.append(
            ToolSpec(
                name=tool_name,
                description=description or inspect.getdoc(function) or "",
                input_schema=model.model_json_schema(),
                source="python",
                read_only=read_only,
                needs_approval=needs_approval(self._approval, read_only),
            )
        )

    async def _call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        validated = self._models[name].model_validate(arguments or {})
        result = self._functions[name](**dict(validated))
        if isinstance(result, Awaitable):
            result = await result
        output = result if isinstance(result, str) else json.dumps(TypeAdapter(Any).dump_python(result, mode="json"))
        return ToolResult(ok=True, output=output)
