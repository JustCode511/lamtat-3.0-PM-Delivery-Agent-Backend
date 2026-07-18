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
    else:
        data = {"error": f"Unknown tool: {name}"}

    return [types.TextContent(type="text", text=json.dumps(data))]


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())