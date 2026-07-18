"""
Agent Core — the reasoning loop.

Domain-agnostic: knows how to run an LLM conversation with tools and memory.
Delegates the tool-call cycle to the LLM adapter's run_conversation() when
available (reliable for Gemini's strict turn ordering).
"""
from __future__ import annotations
import asyncio
from typing import Any

from agent.mcp_client import MCPClient
from interfaces.llm import LLMClient
from interfaces.storage import SessionStore

SYSTEM_PROMPT = """You are a PM Delivery Agent for enterprise software projects.

You help project managers by reading live project data from Jira and answering clearly.
You have tools available — use them to get real data rather than guessing.

Guidelines:
- To list available projects, use list_projects.
- To answer about status, progress, or deadlines, use get_project_status.
- To answer about risks, blockers, or project health, use flag_risks.
- If a risk is HIGH severity, use request_approval to escalate for human sign-off.
- If the user doesn't specify a project key, ask which project (e.g. "SCRUM").
- - When referring to a project, use ONLY its friendly project_name. Never show the project key (like SCRUM) to the user — the key is internal.
- Keep answers concise and practical. Summarize data in plain English;
  never dump raw JSON at the user.
"""

MAX_TOOL_ROUNDS = 5


class Agent:
    def __init__(self, llm: LLMClient, store: SessionStore, mcp: MCPClient) -> None:
        self.llm = llm
        self.store = store
        self.mcp = mcp

    async def chat(self, session_id: str, user_message: str) -> str:
        history: list[dict[str, Any]] = self.store.get_history(session_id)
        tools = await self.mcp.list_tools()

        try:
            if hasattr(self.llm, "run_conversation"):
                final_text = await self._run_with_adapter(user_message, history, tools)
            else:
                final_text = await self._run_manual(user_message, history, tools)
        except Exception as e:
            return f"[Error] {type(e).__name__}: {e}"

        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": final_text})
        self.store.save_history(session_id, history)

        return final_text or "[No answer produced.]"

    async def _run_with_adapter(self, user_message, history, tools) -> str:
        """
        The LLM adapter (Gemini) drives the tool cycle synchronously.
        We run it in a worker thread, and bridge its sync tool calls back to
        our async MCP client using run_coroutine_threadsafe on THIS loop.
        """
        main_loop = asyncio.get_running_loop()

        def tool_executor(name: str, args: dict[str, Any]) -> str:
            # Schedule the async MCP call on the main loop from the worker thread
            future = asyncio.run_coroutine_threadsafe(
                self.mcp.call_tool(name, args), main_loop
            )
            return future.result(timeout=30)

        return await asyncio.to_thread(
            self.llm.run_conversation,
            SYSTEM_PROMPT,
            user_message,
            history,
            tools,
            tool_executor,
            MAX_TOOL_ROUNDS,
        )

    async def _run_manual(self, user_message, history, tools) -> str:
        """Fallback manual loop (e.g. for Bedrock)."""
        messages = history + [{"role": "user", "content": user_message}]
        final_text = ""
        for _ in range(MAX_TOOL_ROUNDS):
            response = self.llm.generate(SYSTEM_PROMPT, messages, tools)
            if response.wants_tools:
                if response.text:
                    messages.append({"role": "assistant", "content": response.text})
                for call in response.tool_calls:
                    result = await self.mcp.call_tool(call.name, call.arguments)
                    messages.append({
                        "role": "tool", "name": call.name,
                        "call_id": call.call_id, "content": result,
                    })
                continue
            final_text = response.text
            break
        return final_text
