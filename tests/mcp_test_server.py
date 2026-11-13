"""A tiny MCP server for the tests: one tool that only reads, one that writes, one that fails."""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("test-server")
NOTES: list[str] = []


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def lookup_order(order_id: str) -> str:
    """Find an order by its id."""
    return f"order {order_id}: 2 items, shipped"


@server.tool()
def save_note(text: str) -> str:
    """Save a note for the team."""
    NOTES.append(text)
    return f"saved note {len(NOTES)}"


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def broken() -> str:
    """Always fails."""
    raise RuntimeError("the backend is down")


if __name__ == "__main__":
    server.run("stdio")
