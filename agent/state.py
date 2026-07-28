"""
Shared state type and utility functions used across all graph nodes.
"""
from __future__ import annotations
import asyncio
import json
from typing import Any, Literal

from typing_extensions import TypedDict

from interfaces.llm import LLMClient

Intent = Literal[
    "query",
    "create_issue",
    "send_slack_notification",
    "generate_ppt",
    "out_of_scope",
    "clarify_project",
    "default",
]


class AgentState(TypedDict):
    user_message: str
    history: list[dict[str, Any]]
    intent: Intent
    project_key: str | None
    result: str


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of an LLM response that may have extra text."""
    # Try whole string first (LLM returned clean JSON)
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    # Find the first { … } block, handling nested braces correctly
    start = text.find("{")
    if start == -1:
        return {"intent": "default", "project_key": None}
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    break

    return {"intent": "default", "project_key": None}


import logging as _logging
_llm_log = _logging.getLogger(__name__)

_RATE_LIMIT_KEYWORDS = ("429", "ResourceExhausted", "quota exceeded", "rate limit", "RESOURCE_EXHAUSTED")
_RETRY_WAITS = (20, 35, 60)  # seconds between attempts 1→2, 2→3, 3→fail


async def llm_generate(llm: LLMClient, system: str, user: str) -> str:
    """Run a single-turn LLM call with automatic retry on rate-limit errors."""
    import re as _re
    for attempt in range(len(_RETRY_WAITS) + 1):
        try:
            response = await asyncio.to_thread(
                llm.generate,
                system,
                [{"role": "user", "content": user}],
                [],
            )
            return response.text or "[No response produced.]"
        except Exception as e:
            err = str(e)
            is_rate_limit = any(kw in err for kw in _RATE_LIMIT_KEYWORDS)
            if is_rate_limit and attempt < len(_RETRY_WAITS):
                # Try to honour the suggested retry delay from the error
                m = _re.search(r'retry_delay\s*\{[^}]*seconds:\s*(\d+)', err)
                wait = int(m.group(1)) + 3 if m else _RETRY_WAITS[attempt]
                _llm_log.warning(
                    "[LLM] Rate limited (attempt %d/%d) — waiting %ds before retry",
                    attempt + 1, len(_RETRY_WAITS), wait,
                )
                await asyncio.sleep(wait)
                continue
            raise
