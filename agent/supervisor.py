"""
Supervisor node — classifies user intent and extracts project key.
Uses conversation history for context when the current message is ambiguous.
Never calls tools; routing only.
"""
from __future__ import annotations
import logging
from typing import Any

from agent.state import AgentState, extract_json, llm_generate
from interfaces.llm import LLMClient

log = logging.getLogger(__name__)

SUPERVISOR_PROMPT = """You are a routing supervisor for a PM Delivery Agent.
Classify the user's CURRENT message into one of these intents, using the conversation
history for context when the current message alone is ambiguous.

Intents:
- draft_deliverables: user wants to create, list, or draft project deliverables
- track_milestones: user asks about milestones, deadlines, progress, or schedule
- flag_risks: user asks about risks, blockers, issues, or project health
- generate_status_report: user wants a status update, summary, or overview
- send_slack_notification: user explicitly asks to send a Slack message, notify the team,
  share an update on Slack, ping the channel, or alert stakeholders via Slack
- default: greetings, small talk, out-of-scope questions, or unclear requests

Rules for project key:
1. Check the current message first for a Jira project key (e.g. SCRUM, PROJ, AABG-HACKATHON-FY26)
2. If not in current message, scan the conversation history for a previously mentioned key
3. Project keys are often hyphenated strings or short uppercase words

Rules for intent:
- If the current message is a short follow-up answer (e.g. just a project name or key) to the
  agent's previous question, inherit the intent from the most recent user turn in history
- If the user is explicitly changing topic, use the new intent
- Prefer send_slack_notification when the user's primary goal is to notify/alert others on Slack,
  even if a project is also mentioned

Reply with JSON only — no other text:
{"intent": "<one of the 6 intents>", "project_key": "<KEY or null>"}"""


def make_supervisor_node(llm: LLMClient):
    async def supervisor_node(state: AgentState) -> AgentState:
        log.info("[SUPERVISOR] Classifying message: %r", state["user_message"])

        history_block = ""
        if state["history"]:
            recent = state["history"][-10:]  # last 5 turns (user + assistant pairs)
            lines = "\n".join(f'{m["role"].upper()}: {m["content"]}' for m in recent)
            history_block = f"\n\nConversation so far:\n{lines}"

        raw = await llm_generate(
            llm,
            SUPERVISOR_PROMPT,
            f"Current message: {state['user_message']}{history_block}",
        )
        parsed = extract_json(raw)
        intent = parsed.get("intent", "default")
        project_key = parsed.get("project_key") or state.get("project_key")
        log.info("[SUPERVISOR] → intent=%r  project_key=%r", intent, project_key)
        return {**state, "intent": intent, "project_key": project_key}

    return supervisor_node
