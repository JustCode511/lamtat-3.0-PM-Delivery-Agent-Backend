"""
Specialized agent nodes.

The heavy lifting for all PM read/analysis queries is now done by
pm_query_agent (free-form tool calling). This file only contains the
action nodes: create_issue, slack notification, PPT generation, plus
utility nodes (out_of_scope, clarify_project, default).
"""
from __future__ import annotations
import json
import logging
import os
from typing import Any

from agent.state import AgentState, llm_generate
from interfaces.llm import LLMClient

log = logging.getLogger(__name__)



CREATE_ISSUE_EXTRACT_PROMPT = """You are a PM Delivery Agent. The user wants to create EXACTLY ONE Jira issue.

CRITICAL RULE: No matter how long or structured the message is — even if it has headers like
"Steps to Reproduce", "RCA", "Fix", numbered lists, or bullet points — treat it as describing
ONE single issue. Never split it into multiple tickets.

Extraction rules:
- summary: the main title (use the "Title:" line if present, or the first sentence — max 255 chars)
- description: put ALL remaining details here — steps, root cause, fix suggestions, context, everything
- issue_type: infer from content (Bug → error/crash/null/broken/exception; Story → feature/user story/"as a";
  Epic → large initiative; Task → everything else). Default: Task
- priority: infer from severity (Highest/High → crash/critical/null return/production issue/urgent;
  Medium → default; Low → minor/cosmetic)
- project_key: Jira project key if explicitly mentioned, otherwise null
- assignee_name: full display name of the assignee if mentioned (e.g. "Parth Kansara"), otherwise null
- assignee_email: email address only if explicitly stated, otherwise null

Return EXACTLY ONE JSON object — not an array, no extra text, no markdown fences:
{{
  "project_key": "<KEY or null>",
  "summary": "<one-line title>",
  "description": "<all supporting details from the message>",
  "issue_type": "<Bug|Story|Epic|Task>",
  "priority": "<Highest|High|Medium|Low|Lowest>",
  "assignee_name": "<display name or null>",
  "assignee_email": "<email or null>"
}}"""

CREATE_ISSUE_RESULT_PROMPT = """You are a PM Delivery Agent.
A Jira issue was just created. Given the result data below, inform the user in a friendly,
concise way: confirm success with the issue key and link, or explain the error clearly.
No raw JSON."""

GENERATE_PPT_PROMPT = """You are a PM Delivery Agent.
A PowerPoint presentation has been prepared for the user. Given the context below, write a
short, friendly confirmation message (2-3 sentences max) telling the user:
1. The presentation is ready with the number of projects covered.
2. The exact download URL they should use.
3. One sentence on what slides are included.
No raw JSON. No markdown headers."""

SLACK_NOTIFICATION_COMPOSE_PROMPT = """You are a PM Delivery Agent composing a Slack notification for the team.
Given the user's request and any available project context, write a clear, concise Slack message.
The message should be professional but conversational (Slack tone), concise (3-6 lines max),
and action-oriented. Plain text only — no markdown headers, no raw JSON.
Return ONLY the message text that will be posted to Slack."""

DEFAULT_NODE_PROMPT = """You are a PM Delivery Agent assistant.

If the user is greeting you, greet them back warmly and briefly introduce yourself.

If the user is asking something outside your scope, politely explain what you can help with:
- Searching and analysing Jira projects, tickets, risks, and milestones
- Tracking team workload and assignments
- Comparing projects
- Creating Jira issues (tasks, bugs, stories, epics)
- Generating project status reports and PowerPoint presentations
- Sending Slack notifications to the team

Keep your response concise and friendly."""


def make_nodes(llm: LLMClient, mcp: Any) -> dict[str, Any]:

    # ------------------------------------------------------------------ #
    # PM Query node — delegates to the tool-calling agent                 #
    # ------------------------------------------------------------------ #
    async def query_node(state: AgentState) -> AgentState:
        from agent.pm_query_agent import run_pm_query
        log.info("[NODE] query  message=%r", state["user_message"][:100])
        result = await run_pm_query(
            llm, mcp,
            state["user_message"],
            state.get("history", []),
        )
        return {**state, "result": result}

    # ------------------------------------------------------------------ #
    # Out-of-scope guard — no LLM call                                    #
    # ------------------------------------------------------------------ #
    async def out_of_scope_node(state: AgentState) -> AgentState:
        log.info("[NODE] out_of_scope  message=%r", state["user_message"][:80])
        return {**state, "result": (
            "Hey! I'm your **PM Delivery Agent** — an AI built to keep your projects on track.\n\n"
            "Here's what I can do for you:\n\n"
            "- **Project Intelligence** — delivery forecasts, risk detection, milestone tracking, health assessments\n"
            "- **Team Insights** — workload analysis, ticket assignments, blocker identification\n"
            "- **Jira Actions** — create bugs, stories, tasks, and epics with smart field extraction\n"
            "- **Reports** — status reports, project comparisons, portfolio views\n"
            "- **Slack** — send team notifications and updates\n\n"
            "Just ask naturally — *\"Are we going to make the Aug 14 deadline?\"*, "
            "*\"Who's overloaded this week?\"*, *\"Flag all risks across projects\"* — I'll figure out the rest."
        )}

    # ------------------------------------------------------------------ #
    # Clarify project — used only when PPT is requested without a key     #
    # ------------------------------------------------------------------ #
    async def clarify_project_node(state: AgentState) -> AgentState:
        log.info("[NODE] clarify_project")
        projects_data = await mcp.call_tool("list_projects", {})
        obj = json.loads(projects_data) if isinstance(projects_data, str) else projects_data
        keys = [p["key"] for p in obj.get("projects", []) if p.get("key")]
        key_list = " · ".join(f"`{k}`" for k in keys) if keys else "none found"
        return {**state, "result": (
            f"Sure! Which project should I generate the PowerPoint for?\n\n"
            f"**Available projects:** {key_list}\n\n"
            "Or reply **all** to cover all projects at once."
        )}

    # ------------------------------------------------------------------ #
    # Generate PPT                                                        #
    # ------------------------------------------------------------------ #
    async def generate_ppt_node(state: AgentState) -> AgentState:
        pk = state.get("project_key")
        if pk == "__ALL__":
            pk = None
        log.info("[NODE] generate_ppt  project_key=%r", pk)
        base_url = os.getenv("APP_BASE_URL", "http://localhost:8000")
        download_url = f"{base_url}/export/ppt" + (f"?project_key={pk}" if pk else "")
        scope = f"project {pk}" if pk else "all projects"
        context = (
            f"Download URL: {download_url}\n"
            f"Scope: {scope}\n"
            f"Slides included: Title, Executive Summary, per-project Status & Risk slides, Next Steps."
        )
        result = await llm_generate(llm, GENERATE_PPT_PROMPT, context)
        return {**state, "result": result}

    # ------------------------------------------------------------------ #
    # Create Issue                                                        #
    # ------------------------------------------------------------------ #
    async def _resolve_project_key(raw_key: str) -> tuple[str | None, list[str]]:
        data = await mcp.call_tool("list_projects", {})
        obj = json.loads(data) if isinstance(data, str) else data
        keys = [p["key"] for p in obj.get("projects", []) if p.get("key")]
        upper = raw_key.upper()
        if upper in keys:
            return upper, keys
        matches = [k for k in keys if k.startswith(upper) or upper.startswith(k)]
        if len(matches) == 1:
            return matches[0], keys
        return None, keys

    async def create_issue_node(state: AgentState) -> AgentState:
        log.info("[NODE] create_issue  message=%r", state["user_message"][:100])

        projects_data = await mcp.call_tool("list_projects", {})
        projects_obj = json.loads(projects_data) if isinstance(projects_data, str) else projects_data
        all_projects = projects_obj.get("projects", [])
        project_list = ", ".join(
            f"{p['key']} ({p.get('name', '')})" for p in all_projects if p.get("key")
        )
        all_keys = [p["key"] for p in all_projects if p.get("key")]

        history_block = ""
        if state.get("history"):
            recent = state["history"][-6:]
            history_block = "\n\nRecent conversation:\n" + "\n".join(
                f'{m["role"].upper()}: {m["content"]}' for m in recent
            )

        context = (
            f"Current message: {state['user_message']}"
            f"{history_block}"
            f"\n\nAvailable Jira projects: {project_list}"
        )
        if state.get("project_key") and state["project_key"] not in ("__ALL__", None):
            context += f"\nPreviously resolved project_key: {state['project_key']}"

        raw_fields = await llm_generate(llm, CREATE_ISSUE_EXTRACT_PROMPT, context)

        try:
            fields = json.loads(raw_fields.strip())
        except json.JSONDecodeError:
            from agent.state import extract_json
            fields = extract_json(raw_fields)

        if isinstance(fields, list):
            log.warning("[NODE] create_issue: LLM returned list — using first only")
            fields = fields[0] if fields else {}

        if not fields.get("project_key") and state.get("project_key") not in ("__ALL__", None):
            fields["project_key"] = state["project_key"]

        issue_type = fields.get("issue_type", "Task")
        assignee = fields.get("assignee_name") or fields.get("assignee_email")
        has_project = bool(fields.get("project_key"))
        has_summary = bool(fields.get("summary", "").strip())

        if not has_project or not has_summary:
            lines = [f"I'll create this **{issue_type}** for you! Please fill in the missing details:\n"]
            if has_project:
                lines.append(f"- **Project:** {fields['project_key']} ✓")
            else:
                lines.append(f"- **Project** *(required)* — pick one: `{'`, `'.join(all_keys)}`")
            if has_summary:
                lines.append(f"- **Title:** {fields['summary']} ✓")
            else:
                lines.append("- **Title** *(required)*: [your one-line summary here]")
            if issue_type == "Bug":
                lines.append("- **Steps to Reproduce** *(optional)*: ")
                lines.append("- **Root Cause / RCA** *(optional)*: ")
                lines.append("- **Fix / Workaround** *(optional)*: ")
            else:
                lines.append("- **Description** *(optional)*: ")
            lines.append(f"- **Priority:** {fields.get('priority', 'Medium')} *(override: Highest / High / Medium / Low / Lowest)*")
            if assignee:
                lines.append(f"- **Assign to:** {assignee} ✓")
            else:
                lines.append("- **Assign to** *(optional)*: [name or email]")
            lines.append("\nReply with the details above and I'll create the ticket immediately!")
            return {**state, "result": "\n".join(lines)}

        resolved_key, available_keys = await _resolve_project_key(fields["project_key"])
        if not resolved_key:
            return {**state, "result": (
                f"Project '{fields['project_key']}' not found in Jira.\n"
                f"Available: {', '.join(available_keys)}."
            )}
        fields["project_key"] = resolved_key

        tool_args: dict = {
            "project_key": fields["project_key"],
            "summary": fields["summary"],
            "description": fields.get("description", ""),
            "issue_type": issue_type,
            "priority": fields.get("priority", "Medium"),
        }
        if fields.get("assignee_name"):
            tool_args["assignee_name"] = fields["assignee_name"]
        if fields.get("assignee_email"):
            tool_args["assignee_email"] = fields["assignee_email"]

        log.info("[TOOL] create_issue  %r", tool_args)
        create_result = await mcp.call_tool("create_issue", tool_args)
        result = await llm_generate(
            llm, CREATE_ISSUE_RESULT_PROMPT,
            f"Create result:\n{create_result}\n\nUser request: {state['user_message']}",
        )
        return {**state, "result": result}

    # ------------------------------------------------------------------ #
    # Slack notification                                                  #
    # ------------------------------------------------------------------ #
    async def send_slack_notification_node(state: AgentState) -> AgentState:
        pk = state.get("project_key")
        if pk == "__ALL__":
            pk = None
        log.info("[NODE] slack  project_key=%r", pk)

        context_parts = [f"User request: {state['user_message']}"]
        if pk:
            try:
                status_data = await mcp.call_tool("get_project_status", {"project_key": pk})
                context_parts.append(f"Project status data:\n{status_data}")
            except Exception:
                pass

        message = await llm_generate(llm, SLACK_NOTIFICATION_COMPOSE_PROMPT, "\n\n".join(context_parts))

        kwargs: dict[str, Any] = {"message": message}
        if pk:
            kwargs["project_key"] = pk
        result_data = await mcp.call_tool("send_slack_notification", kwargs)
        result_obj = json.loads(result_data) if isinstance(result_data, str) else result_data

        if result_obj.get("sent"):
            return {**state, "result": f"Done! I've posted the following to Slack:\n\n_{message}_"}
        else:
            err = result_obj.get("error", "unknown error")
            return {**state, "result": f"I couldn't send the Slack notification: {err}"}

    # ------------------------------------------------------------------ #
    # Default                                                             #
    # ------------------------------------------------------------------ #
    async def default_node(state: AgentState) -> AgentState:
        result = await llm_generate(llm, DEFAULT_NODE_PROMPT, state["user_message"])
        return {**state, "result": result}

    return {
        "query": query_node,
        "create_issue": create_issue_node,
        "generate_ppt": generate_ppt_node,
        "send_slack_notification": send_slack_notification_node,
        "out_of_scope": out_of_scope_node,
        "clarify_project": clarify_project_node,
        "default": default_node,
    }
