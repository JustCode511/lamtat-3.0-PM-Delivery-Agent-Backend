"""
Agent Core — thin session wrapper around the LangGraph supervisor graph.

Loads conversation history from the session store, runs the graph for one
turn, then persists the updated history.

Optimisations:
- Graph is built ONCE per Agent instance (not per chat() call).
- History is trimmed to MAX_HISTORY_MESSAGES before being sent to LLM
  to keep token costs bounded across long sessions.
- LongTermMemory (optional) prepends a rolling session summary and/or
  cross-session user facts so the agent is never cold on a long session.
"""
from __future__ import annotations
import asyncio
import logging
from typing import Any

from agent.graph import AgentState, build_graph
from interfaces.llm import LLMClient
from interfaces.storage import SessionStore

log = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 20  # keep last 10 turns (user + assistant pairs)


class Agent:
    def __init__(self, llm: LLMClient, store: SessionStore, mcp: Any, memory: Any = None) -> None:
        self.store = store
        self.memory = memory
        self._graph = build_graph(llm, mcp)

    async def chat(
        self,
        session_id: str,
        user_message: str,
        user_id: str | None = None,
    ) -> tuple[str, str]:
        """Returns (reply, intent) so callers can drive UI rendering from the intent."""
        full_history: list[dict[str, Any]] = self.store.get_history(session_id)

        # Trim to last N messages for LLM — full history still persisted to storage
        trimmed = full_history[-MAX_HISTORY_MESSAGES:] if len(full_history) > MAX_HISTORY_MESSAGES else full_history

        # Prepend long-term memory context (session summary + user facts)
        if self.memory:
            prefix = self.memory.build_context(session_id, full_history, user_id)
            history_for_llm = prefix + list(trimmed) if prefix else list(trimmed)
        else:
            history_for_llm = list(trimmed)

        log.info("[AGENT] session=%s  history_len=%d  invoking graph", session_id, len(history_for_llm))

        try:
            state: AgentState = await self._graph.ainvoke({
                "user_message": user_message,
                "history": history_for_llm,
                "intent": "unknown",
                "project_key": None,
                "result": "",
            })
            reply = state.get("result") or "[No answer produced.]"
            intent = str(state.get("intent") or "default")
            log.info(
                "[AGENT] session=%s  intent=%s  reply_len=%d",
                session_id, intent, len(reply),
            )
        except Exception as e:
            log.exception("[AGENT] session=%s  error: %s", session_id, e)
            return f"[Error] {type(e).__name__}: {e}", "default"

        full_history.append({"role": "user", "content": user_message})
        full_history.append({"role": "assistant", "content": reply})
        self.store.save_history(session_id, full_history)

        # Fire-and-forget memory update — does not block the response
        if self.memory:
            asyncio.create_task(
                self.memory.update_after_turn(
                    session_id, user_id, user_message, reply, list(full_history)
                )
            )

        return reply, intent
