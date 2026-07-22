"""
Shared state type and utility functions used across all graph nodes.
"""
from __future__ import annotations
import asyncio
import json
import re
from typing import Any, Literal

from typing_extensions import TypedDict

from interfaces.llm import LLMClient

Intent = Literal[
    "draft_deliverables",
    "track_milestones",
    "flag_risks",
    "generate_status_report",
    "send_slack_notification",
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
    match = re.search(r'\{[^}]+\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {"intent": "default", "project_key": None}


async def llm_generate(llm: LLMClient, system: str, user: str) -> str:
    """Run a single-turn, no-tool LLM call in a thread (generate() is sync)."""
    response = await asyncio.to_thread(
        llm.generate,
        system,
        [{"role": "user", "content": user}],
        [],
    )
    return response.text or "[No response produced.]"
