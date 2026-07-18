"""
Specialized agent nodes — one per intent.

Each node calls exactly the Jira/Slack tools it needs, then asks the LLM to
synthesize a human-readable draft. The aggregator polishes the draft afterward.
"""
from __future__ import annotations
import asyncio
import json
import logging
from typing import Any

from agent.state import AgentState, llm_generate
from interfaces.llm import LLMClient

log = logging.getLogger(__name__)

DRAFT_DELIVERABLES_PROMPT = """You are a PM Delivery Agent specializing in deliverables planning.
Given the project data below, draft a clear, actionable list of key deliverables with owners and
target dates where available. Format as a concise bullet list. Never dump raw JSON."""

TRACK_MILESTONES_PROMPT = """You are a PM Delivery Agent specializing in milestone tracking.
Given the project data below, summarize milestone progress: what is on track, what is at risk,
what is complete, and what is overdue. Be concise and practical."""

FLAG_RISKS_PROMPT = """You are a PM Delivery Agent specializing in risk management.
Given the project data below, identify and explain all risks clearly: severity (HIGH / MEDIUM / LOW),
business impact, and a specific recommended action for each. Be direct and actionable."""

GENERATE_STATUS_REPORT_PROMPT = """You are a PM Delivery Agent specializing in executive reporting.
Given the project data below, write a concise status report covering:
1. Overall project health
2. Milestone summary (done / in-progress / overdue)
3. Top risks and mitigations
4. Recommended next steps

Keep it executive-friendly — no raw JSON, no internal keys."""

SLACK_NOTIFICATION_COMPOSE_PROMPT = """You are a PM Delivery Agent composing a Slack notification for the team.

Given the user's request and any available project context, write a clear, concise Slack message
that communicates the key information. The message should be:
- Professional but conversational (Slack tone, not email)
- Concise — 3-6 lines maximum
- Action-oriented when relevant (include next steps or a call to action)
- Plain text only — no markdown headers, no raw JSON

Return ONLY the message text that will be posted to Slack. Nothing else."""

DEFAULT_NODE_PROMPT = """You are a PM Delivery Agent assistant.

If the user is greeting you, greet them back warmly and briefly introduce yourself.

If the user is asking something outside your scope, politely explain what you can help with:
- Drafting project deliverables
- Tracking milestones and deadlines
- Flagging risks and blockers
- Generating project status reports
- Sending Slack notifications to your team

Keep your response concise and friendly."""


def make_nodes(llm: LLMClient, mcp: Any) -> dict[str, Any]:
    """Return a dict of node_name → async node function, all sharing llm and mcp."""

    async def draft_deliverables_node(state: AgentState) -> AgentState:
        pk = state["project_key"]
        log.info("[NODE] draft_deliverables  project_key=%r", pk)
        if not pk:
            return {**state, "result": "Please specify a project key (e.g. SCRUM) so I can draft deliverables for the right project."}

        log.info("[TOOL] get_project_status(%r)", pk)
        status_data = await mcp.call_tool("get_project_status", {"project_key": pk})
        result = await llm_generate(
            llm,
            DRAFT_DELIVERABLES_PROMPT,
            f"Project data:\n{status_data}\n\nUser request: {state['user_message']}",
        )
        log.info("[NODE] draft_deliverables complete")
        return {**state, "result": result}

    async def track_milestones_node(state: AgentState) -> AgentState:
        pk = state["project_key"]
        log.info("[NODE] track_milestones  project_key=%r", pk)
        if not pk:
            return {**state, "result": "Please specify a project key (e.g. SCRUM) to track milestones."}

        log.info("[TOOL] get_project_status(%r)", pk)
        status_data = await mcp.call_tool("get_project_status", {"project_key": pk})
        result = await llm_generate(
            llm,
            TRACK_MILESTONES_PROMPT,
            f"Project data:\n{status_data}\n\nUser request: {state['user_message']}",
        )
        log.info("[NODE] track_milestones complete")
        return {**state, "result": result}

    async def flag_risks_node(state: AgentState) -> AgentState:
        pk = state["project_key"]
        log.info("[NODE] flag_risks  project_key=%r", pk)
        if not pk:
            return {**state, "result": "Please specify a project key (e.g. SCRUM) to flag risks."}

        log.info("[TOOL] flag_risks(%r)", pk)
        risk_data = await mcp.call_tool("flag_risks", {"project_key": pk})
        risk_obj = json.loads(risk_data) if isinstance(risk_data, str) else risk_data
        severity = risk_obj.get("severity", "?")
        log.info("[NODE] flag_risks severity=%r  risk_count=%r", severity, risk_obj.get("risk_count"))

        if severity == "HIGH":
            summary = f"{risk_obj.get('risk_count', '?')} high-priority unresolved issues blocking delivery"
            log.info("[TOOL] request_approval — escalating to Slack")
            await mcp.call_tool("request_approval", {"project_key": pk, "summary": summary})

        result = await llm_generate(
            llm,
            FLAG_RISKS_PROMPT,
            f"Risk data:\n{risk_data}\n\nUser request: {state['user_message']}",
        )
        log.info("[NODE] flag_risks complete")
        return {**state, "result": result}

    async def generate_status_report_node(state: AgentState) -> AgentState:
        pk = state["project_key"]
        log.info("[NODE] generate_status_report  project_key=%r", pk)
        if not pk:
            return {**state, "result": "Please specify a project key (e.g. SCRUM) to generate a status report."}

        log.info("[TOOL] get_project_status + flag_risks (parallel)  project_key=%r", pk)
        status_data, risk_data = await asyncio.gather(
            mcp.call_tool("get_project_status", {"project_key": pk}),
            mcp.call_tool("flag_risks", {"project_key": pk}),
        )
        result = await llm_generate(
            llm,
            GENERATE_STATUS_REPORT_PROMPT,
            f"Status data:\n{status_data}\n\nRisk data:\n{risk_data}\n\nUser request: {state['user_message']}",
        )
        log.info("[NODE] generate_status_report complete")
        return {**state, "result": result}

    async def send_slack_notification_node(state: AgentState) -> AgentState:
        pk = state.get("project_key")
        log.info("[NODE] send_slack_notification  project_key=%r", pk)

        context = f"User request: {state['user_message']}"
        if pk:
            log.info("[TOOL] get_project_status(%r) for notification context", pk)
            try:
                status_data = await mcp.call_tool("get_project_status", {"project_key": pk})
                context += f"\n\nProject context:\n{status_data}"
            except Exception as exc:
                log.warning("[NODE] could not fetch project context: %s", exc)

        slack_message = await llm_generate(llm, SLACK_NOTIFICATION_COMPOSE_PROMPT, context)

        log.info("[TOOL] send_slack_notification")
        send_result = await mcp.call_tool(
            "send_slack_notification",
            {"message": slack_message, **({"project_key": pk} if pk else {})},
        )

        send_obj = json.loads(send_result) if isinstance(send_result, str) else send_result
        if send_obj.get("sent"):
            result = f"Notification sent to Slack successfully.\n\n**Message sent:**\n{slack_message}"
        else:
            note = send_obj.get("note", "")
            preview = send_obj.get("preview", slack_message)
            result = (
                f"Slack is not configured yet — here is the message that would have been sent:\n\n"
                f"{preview}\n\n_{note}_"
            )

        log.info("[NODE] send_slack_notification complete  sent=%r", send_obj.get("sent"))
        return {**state, "result": result}

    async def default_node(state: AgentState) -> AgentState:
        log.info("[NODE] default  message=%r", state["user_message"])
        result = await llm_generate(llm, DEFAULT_NODE_PROMPT, state["user_message"])
        log.info("[NODE] default complete")
        return {**state, "result": result}

    return {
        "draft_deliverables": draft_deliverables_node,
        "track_milestones": track_milestones_node,
        "flag_risks": flag_risks_node,
        "generate_status_report": generate_status_report_node,
        "send_slack_notification": send_slack_notification_node,
        "default": default_node,
    }
