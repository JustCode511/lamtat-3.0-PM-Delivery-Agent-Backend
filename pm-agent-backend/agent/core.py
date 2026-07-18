"""
Agent Core — the reasoning loop. THIS IS THE BRAIN.

Domain-agnostic: it knows how to run an LLM conversation, call tools via MCP,
and remember history. It does NOT know what PM, Jira, or Slack are — those
come from the MCP server's tools. That's what lets us add FinOps/Talent/Code
later without touching this file.

The loop:
  1. Load history for the session
  2. Add the user's new message
  3. Ask the LLM (with available tools)
  4. If the LLM wants a tool -> call it via MCP -> feed result back -> repeat
  5. When the LLM returns text -> that's the answer
  6. Save history
"""
from __future__ import annotations
from typing import Any

from agent.mcp_client import MCPClient
from interfaces.llm import LLMClient
from interfaces.storage import SessionStore

SYSTEM_PROMPT = """You are a PM Delivery Agent for enterprise software projects.

You help project managers by reading live project data and answering clearly.
You have tools available — use them to get real data rather than guessing.

Guidelines:
- To answer about status, progress, or deadlines, use get_project_status.
- To answer about risks, blockers, or project health, use flag_risks.
- If a risk is HIGH severity, use request_approval to escalate for human sign-off
  before presenting your final recommendation.
- If the user doesn't specify a project key, ask which project (e.g. "EPM").
- Keep answers concise and practical. Summarize data in plain English;
  don't dump raw JSON at the user.
"""

# Safety cap so a misbehaving loop can't call tools forever.
MAX_TOOL_ROUNDS = 5


class Agent:
    def __init__(self, llm: LLMClient, store: SessionStore, mcp: MCPClient) -> None:
        self.llm = llm
        self.store = store
        self.mcp = mcp

    async def chat(self, session_id: str, user_message: str) -> str:
        # 1. Load history + append the new user turn
        messages: list[dict[str, Any]] = self.store.get_history(session_id)
        messages.append({"role": "user", "content": user_message})

        # 2. Discover tools the MCP server offers
        tools = await self.mcp.list_tools()

        final_text = ""

        # 3. Reasoning loop
        for _ in range(MAX_TOOL_ROUNDS):
            response = self.llm.generate(SYSTEM_PROMPT, messages, tools)

            if response.wants_tools:
                # Record that the assistant chose to call tools
                if response.text:
                    messages.append({"role": "assistant", "content": response.text})

                # 4. Execute each requested tool via MCP, feed results back
                for call in response.tool_calls:
                    result = await self.mcp.call_tool(call.name, call.arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "name": call.name,
                            "call_id": call.call_id,
                            "content": result,
                        }
                    )
                # loop again so the LLM can use the tool results
                continue

            # 5. No tools requested -> this is the final answer
            final_text = response.text
            messages.append({"role": "assistant", "content": final_text})
            break

        # 6. Persist and return
        self.store.save_history(session_id, messages)
        return final_text or "I wasn't able to produce a response. Please try again."
