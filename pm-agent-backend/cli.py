"""
CLI — talk to the agent from your terminal. Fastest way to test locally.

Run (Windows & Mac, from the project root, with venv active):
    python cli.py

Type messages; type 'exit' or 'quit' to stop.
"""
from __future__ import annotations
import asyncio
import uuid

from agent.core import Agent
from agent.mcp_client import MCPClient
from shared.config import get_llm, get_session_store


async def main() -> None:
    llm = get_llm()
    store = get_session_store()
    mcp = MCPClient()

    print("Connecting to MCP server...")
    await mcp.connect()
    tools = await mcp.list_tools()
    print(f"Connected. Tools available: {', '.join(t.name for t in tools)}")
    print("PM Delivery Agent ready. Type 'exit' to quit.\n")

    session_id = f"cli-{uuid.uuid4().hex[:8]}"

    try:
        while True:
            user = input("You: ").strip()
            if user.lower() in ("exit", "quit"):
                break
            if not user:
                continue
            answer = await agent_reply(llm, store, mcp, session_id, user)
            print(f"\nAgent: {answer}\n")
    finally:
        await mcp.close()
        print("Goodbye.")


async def agent_reply(llm, store, mcp, session_id, user) -> str:
    agent = Agent(llm, store, mcp)
    return await agent.chat(session_id, user)


if __name__ == "__main__":
    asyncio.run(main())
