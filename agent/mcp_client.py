"""
MCP client — the agent's connection to the MCP server.

Launches the PM MCP server as a subprocess over stdio, discovers the tools
it exposes, and calls them on request. This is the client half of MCP;
the server half is mcp_servers/pm_server/server.py.

Locally this uses stdio (subprocess). On AWS you'd point it at an HTTP
transport instead — but the rest of the agent code doesn't change.
"""
from __future__ import annotations
import sys
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from interfaces.llm import ToolSpec


class MCPClient:
    def __init__(self) -> None:
        self.session: ClientSession | None = None
        self._stack = AsyncExitStack()

    async def connect(self) -> None:
        """Start the MCP server subprocess and open a session."""
        # Launch: python -m mcp_servers.pm_server.server
        # sys.executable ensures we use the SAME Python (venv) on Win & Mac.
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_servers.pm_server.server"],
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

    async def list_tools(self) -> list[ToolSpec]:
        """Discover tools the server exposes, in our neutral ToolSpec shape."""
        assert self.session is not None
        result = await self.session.list_tools()
        specs: list[ToolSpec] = []
        for t in result.tools:
            schema = t.inputSchema or {}
            specs.append(
                ToolSpec(
                    name=t.name,
                    description=t.description or "",
                    parameters=schema.get("properties", {}),
                    required=schema.get("required", []),
                )
            )
        return specs

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Invoke a tool on the MCP server and return its text result."""
        assert self.session is not None
        result = await self.session.call_tool(name, arguments)
        # MCP returns a list of content blocks; concatenate the text ones.
        parts: list[str] = []
        for block in result.content:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts)

    async def close(self) -> None:
        await self._stack.aclose()
