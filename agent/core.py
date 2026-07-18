"""
Agent Core — thin session wrapper around the LangGraph supervisor graph.

Loads conversation history from the session store, runs the graph for one
turn, then persists the updated history. All routing and tool-calling logic
lives in agent/graph.py.
"""
from __future__ import annotations
import logging
from typing import Any

from agent.graph import AgentState, build_graph
from interfaces.llm import LLMClient
from interfaces.storage import SessionStore

log = logging.getLogger(__name__)


class Agent:
    def __init__(self, llm: LLMClient, store: SessionStore, mcp: Any) -> None:
        self.store = store
        self._graph = build_graph(llm, mcp)

    async def chat(self, session_id: str, user_message: str) -> str:
        history: list[dict[str, Any]] = self.store.get_history(session_id)
        log.info("[AGENT] session=%s  invoking LangGraph graph", session_id)

        try:
            state: AgentState = await self._graph.ainvoke({
                "user_message": user_message,
                "history": history,
                "intent": "unknown",
                "project_key": None,
                "result": "",
            })
            reply = state.get("result") or "[No answer produced.]"
            log.info("[AGENT] session=%s  intent=%s  reply_len=%d", session_id, state.get("intent"), len(reply))
        except Exception as e:
            log.exception("[AGENT] session=%s  error: %s", session_id, e)
            return f"[Error] {type(e).__name__}: {e}"

        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})
        self.store.save_history(session_id, history)

        return reply
