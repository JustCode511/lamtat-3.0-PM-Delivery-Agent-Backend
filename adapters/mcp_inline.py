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
        else:
            data = {"error": f"Unknown tool: {name}"}
        return json.dumps(data)
