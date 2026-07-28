"""
Inline MCP client — Lambda-compatible alternative to MCPClient.

Calls the Jira/Slack tool functions directly instead of spawning a subprocess
over stdio. Implements the same interface as MCPClient so the agent and graph
code work without any changes — only the config switches which client is used.

Used when APP_ENV=aws. The stdio subprocess used locally cannot reliably
survive across Lambda invocations.
"""
from __future__ import annotations
import json
from typing import Any

from interfaces.llm import ToolSpec
from mcp_servers.pm_server import jira_client, slack_client


class InlineMCPClient:
    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="list_projects",
                description=(
                    "List all projects available in Jira with their key and name. "
                    "Use when the user asks what projects exist."
                ),
                parameters={},
                required=[],
            ),
            ToolSpec(
                name="get_project_status",
                description=(
                    "Get milestone and delivery status for a project from Jira. "
                    "Use when the user asks about progress, status, or deadlines."
                ),
                parameters={
                    "project_key": {"type": "string", "description": "The Jira project key, e.g. SCRUM"}
                },
                required=["project_key"],
            ),
            ToolSpec(
                name="flag_risks",
                description=(
                    "Identify and flag risks in a project based on Jira data. "
                    "Use when the user asks about risks, blockers, or project health."
                ),
                parameters={
                    "project_key": {"type": "string", "description": "The Jira project key, e.g. SCRUM"}
                },
                required=["project_key"],
            ),
            ToolSpec(
                name="request_approval",
                description=(
                    "Send a Human-in-the-Loop approval request to Slack. "
                    "Use ONLY when a high-risk finding needs human sign-off."
                ),
                parameters={
                    "project_key": {"type": "string", "description": "The Jira project key"},
                    "summary": {"type": "string", "description": "What needs approval and why"},
                },
                required=["project_key", "summary"],
            ),
            ToolSpec(
                name="send_slack_notification",
                description=(
                    "Send a general notification or update message to the Slack channel. "
                    "Use when the user explicitly asks to notify the team or send a Slack update."
                ),
                parameters={
                    "message": {"type": "string", "description": "The notification message to send"},
                    "project_key": {"type": "string", "description": "Optional Jira project key for context"},
                },
                required=["message"],
            ),
            ToolSpec(
                name="search_issues",
                description=(
                    "Search and filter Jira issues. Supports filtering by assignee name "
                    "(partial display name match — no accountId needed), status, and project. "
                    "Use for: 'tickets assigned to X', 'show all bugs', 'what is X working on', "
                    "'list in-progress items', 'show all tickets in project Y'."
                ),
                parameters={
                    "project_key": {"type": "string", "description": "Jira project key (optional — omit for all projects)"},
                    "assignee": {"type": "string", "description": "Filter by assignee display name, e.g. 'Chaithanya' or 'Parth Kansara'"},
                    "status": {"type": "string", "description": "Filter by status name, e.g. 'In Progress', 'To Do'"},
                    "jql": {"type": "string", "description": "Raw JQL query string (overrides other filters when provided)"},
                    "max_results": {"type": "integer", "description": "Maximum number of issues to return (default: 100)"},
                },
                required=[],
            ),
            ToolSpec(
                name="create_issue",
                description=(
                    "Create a new Jira issue (task, bug, story, etc.) in a project. "
                    "Use when the user asks to create a ticket, raise an issue, log a bug, "
                    "or add a task to a project."
                ),
                parameters={
                    "project_key": {"type": "string", "description": "The Jira project key, e.g. SCRUM"},
                    "summary": {"type": "string", "description": "One-line title of the issue"},
                    "description": {"type": "string", "description": "Optional detailed description"},
                    "issue_type": {"type": "string", "description": "Task, Bug, Story, Epic (default: Task)"},
                    "priority": {"type": "string", "description": "Highest, High, Medium, Low, Lowest (default: Medium)"},
                    "assignee_name": {"type": "string", "description": "Optional display name of the assignee (e.g. 'Parth Kansara')"},
                    "assignee_email": {"type": "string", "description": "Optional email to assign the issue to"},
                },
                required=["project_key", "summary"],
            ),
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "list_projects":
            data = jira_client.list_projects()
        elif name == "get_project_status":
            data = jira_client.get_project_status(arguments["project_key"])
        elif name == "flag_risks":
            data = jira_client.get_risks(arguments["project_key"])
        elif name == "request_approval":
            data = slack_client.post_approval(arguments["project_key"], arguments["summary"])
        elif name == "send_slack_notification":
            data = slack_client.send_notification(arguments["message"], arguments.get("project_key"))
        elif name == "search_issues":
            data = jira_client.search_issues(
                project_key=arguments.get("project_key", ""),
                assignee=arguments.get("assignee", ""),
                status=arguments.get("status", ""),
                jql=arguments.get("jql", ""),
                max_results=arguments.get("max_results", 100),
            )
        elif name == "create_issue":
            data = jira_client.create_issue(
                project_key=arguments["project_key"],
                summary=arguments["summary"],
                description=arguments.get("description", ""),
                issue_type=arguments.get("issue_type", "Task"),
                priority=arguments.get("priority", "Medium"),
                assignee_email=arguments.get("assignee_email"),
                assignee_name=arguments.get("assignee_name"),
            )
        else:
            data = {"error": f"Unknown tool: {name}"}
        return json.dumps(data)
