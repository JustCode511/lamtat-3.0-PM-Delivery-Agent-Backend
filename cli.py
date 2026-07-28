"""
CLI — talk to the agent from your terminal.

Run (from the project root, with venv active):
    python cli.py

Multi-line input: keep typing across lines, then press Enter on a blank line to send.
This lets you paste structured bug reports, user stories, etc. as a single message.
Type 'exit' or 'quit' on its own line to quit.
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


def read_message() -> str | None:
    """
    Read a user message, supporting multi-line input.
    - Type or paste across multiple lines.
    - Press Enter on a blank line to submit.
    - Type 'exit' or 'quit' alone on the first line to quit.
    Returns None on exit/EOF.
    """
    print("You (blank line to send):")
    lines: list[str] = []
    while True:
        try:
            line = input("  ")
        except EOFError:
            # Ctrl-D — submit whatever was typed, or exit if nothing
            return "\n".join(lines).strip() or None

        # Allow exit only when nothing has been typed yet
        if not lines and line.strip().lower() in ("exit", "quit", ""):
            if line.strip().lower() in ("exit", "quit"):
                return None
            continue  # skip leading blank lines

        # Blank line after content = submit
        if not line.strip() and lines:
            break

        lines.append(line)

    return "\n".join(lines).strip()


async def main() -> None:
    llm = get_llm()
    store = get_session_store()
    mcp = get_mcp_client()

    print("Connecting to MCP server...")
    await mcp.connect()
    tools = await mcp.list_tools()
    print(f"Connected. Tools: {', '.join(t.name for t in tools)}")
    print("\nPM Delivery Agent ready.")
    print("Tip: paste multi-line bug reports / user stories freely — press Enter twice to send.\n")

    session_id = f"cli-{uuid.uuid4().hex[:8]}"
    agent = Agent(llm, store, mcp)  # built once, not per message

    try:
        while True:
            user = read_message()
            if user is None:
                break
            if not user:
                continue
            answer = await agent.chat(session_id, user)
            print(f"\nAgent: {answer}\n")
    finally:
        await mcp.close()
        print("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
