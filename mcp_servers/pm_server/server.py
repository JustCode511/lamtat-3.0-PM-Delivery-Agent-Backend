"""
PM MCP Server — low-level Server API (compatible with mcp 1.1.2).
Exposes PM delivery tools over the Model Context Protocol via stdio.
"""
from __future__ import annotations
import asyncio
import json

from dotenv import load_dotenv
load_dotenv()

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from mcp_servers.pm_server import jira_client, slack_client

server = Server("pm-delivery-tools")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_projects",
            description=(
                "List all projects available in Jira with their key and name. "
                "Use when the user asks what projects exist, or to discover "
                "projects before drilling into one."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="get_project_status",
            description=(
                "Get milestone and delivery status for a project from Jira. "
                "Use when the user asks about progress, status, deadlines, or "
                "how a project is going."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "The Jira project key, e.g. SCRUM",
                    }
                },
                "required": ["project_key"],
            },
        ),
        types.Tool(
            name="flag_risks",
            description=(
                "Identify and flag risks in a project based on Jira data "
                "(high-priority unresolved issues, overdue items, blockers). "
                "Use when the user asks about risks, blockers, or project health."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "The Jira project key, e.g. SCRUM",
                    }
                },
                "required": ["project_key"],
            },
        ),
        types.Tool(
            name="request_approval",
            description=(
                "Send a Human-in-the-Loop (HITL) approval request to Slack. "
                "Use ONLY when a high-risk finding needs human sign-off before "
                "proceeding."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "The Jira project key, e.g. SCRUM",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Short description of what needs approval and why",
                    },
                },
                "required": ["project_key", "summary"],
            },
        ),
        types.Tool(
            name="send_slack_notification",
            description=(
                "Send a general notification or update message to the Slack channel. "
                "Use when the user explicitly asks to notify the team, send a Slack "
                "message, or share a project update on Slack."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The notification message to send to Slack",
                    },
                    "project_key": {
                        "type": "string",
                        "description": "Optional Jira project key for context",
                    },
                },
                "required": ["message"],
            },
        ),
        types.Tool(
            name="search_issues",
            description="Search and filter Jira issues. Supports assignee name (partial match), status, project key, or raw JQL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {"type": "string", "description": "Jira project key (optional)"},
                    "assignee": {"type": "string", "description": "Filter by assignee display name (partial match)"},
                    "status": {"type": "string", "description": "Filter by status name, e.g. 'In Progress'"},
                    "jql": {"type": "string", "description": "Raw JQL query (overrides other filters)"},
                    "max_results": {"type": "integer", "description": "Max results (default 100)"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="create_issue",
            description=(
                "Create a new Jira issue (task, bug, story, etc.) in a project. "
                "Use when the user asks to create a ticket, raise an issue, log a bug, "
                "or add a task to a project."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "The Jira project key, e.g. SCRUM",
                    },
                    "summary": {
                        "type": "string",
                        "description": "One-line title of the issue",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional detailed description",
                    },
                    "issue_type": {
                        "type": "string",
                        "description": "Issue type: Task, Bug, Story, Epic (default: Task)",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Priority: Highest, High, Medium, Low, Lowest (default: Medium)",
                    },
                    "assignee_name": {
                        "type": "string",
                        "description": "Optional display name of the assignee (e.g. 'Parth Kansara')",
                    },
                    "assignee_email": {
                        "type": "string",
                        "description": "Optional email of the person to assign the issue to",
                    },
                },
                "required": ["project_key", "summary"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "list_projects":
        data = jira_client.list_projects()
    elif name == "get_project_status":
        data = jira_client.get_project_status(arguments["project_key"])
    elif name == "flag_risks":
        data = jira_client.get_risks(arguments["project_key"])
    elif name == "request_approval":
        data = slack_client.post_approval(
            arguments["project_key"], arguments["summary"]
        )
    elif name == "send_slack_notification":
        data = slack_client.send_notification(
            arguments["message"], arguments.get("project_key")
        )
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

    return [types.TextContent(type="text", text=json.dumps(data))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())