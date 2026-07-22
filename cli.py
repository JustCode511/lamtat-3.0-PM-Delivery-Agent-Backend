"""
CLI — talk to the agent from your terminal. Fastest way to test locally.

Run (from the project root, with venv active):
    python cli.py
"""
from __future__ import annotations
import asyncio
import logging
import uuid

from agent.core import Agent
from shared.config import get_llm, get_mcp_client, get_session_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)


async def main() -> None:
    llm = get_llm()
    store = get_session_store()
    mcp = get_mcp_client()

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
            agent = Agent(llm, store, mcp)
            answer = await agent.chat(session_id, user)
            print(f"\nAgent: {answer}\n")
    finally:
        await mcp.close()
        print("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
