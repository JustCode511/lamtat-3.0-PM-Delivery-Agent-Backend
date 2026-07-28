"""
Aggregator node — final formatting pass before the response reaches the user.
Receives the raw draft from any specialized node and polishes it: professional
tone, structured layout, no redundancy.
"""
from __future__ import annotations
import logging

from agent.state import AgentState, llm_generate
from interfaces.llm import LLMClient

log = logging.getLogger(__name__)

AGGREGATOR_PROMPT = """You are a professional PM Delivery Agent. Your job is to review a draft response
and deliver it in the best possible way to the user.

Guidelines:
- Be professional, clear, and concise
- Be warm and humble in tone — you are here to assist, not to lecture
- Remove any redundancy or filler phrases
- Use structured formatting (bullet points, sections) where it improves readability
- If the response is a greeting or short conversational reply, keep it brief and natural — do not over-format it
- Never mention that you are "reviewing a draft" or reference the original response
- Speak directly to the user in first person

Return only the final polished response. Nothing else."""


_PASS_THROUGH = frozenset({"query", "out_of_scope", "clarify_project"})


def make_aggregator_node(llm: LLMClient):
    async def aggregator_node(state: AgentState) -> AgentState:
        if state["intent"] in _PASS_THROUGH:
            log.info("[NODE] aggregator skipped for %r", state["intent"])
            return state
        log.info("[NODE] aggregator  intent=%r  draft_len=%d", state["intent"], len(state["result"]))
        polished = await llm_generate(
            llm,
            AGGREGATOR_PROMPT,
            f"User asked: {state['user_message']}\n\nDraft response:\n{state['result']}",
        )
        log.info("[NODE] aggregator complete  final_len=%d", len(polished))
        return {**state, "result": polished}

    return aggregator_node
