"""
PM MCP Server — exposes PM delivery tools over the Model Context Protocol.

This runs as a SEPARATE process. The agent connects to it as an MCP client
(over stdio locally). This is the real, production-grade MCP pattern:
the agent knows nothing about Jira/Slack — it only speaks MCP.

Tools exposed:
  - get_project_status : milestone / delivery status from Jira
  - flag_risks         : identify project risks from Jira
  - request_approval   : send a Human-in-the-Loop approval to Slack

Run standalone (for testing) with:
    python -m mcp_servers.pm_server.server
"""
from __future__ import annotations
import json

from mcp.server.fastmcp import FastMCP

from mcp_servers.pm_server import jira_client, slack_client

# The MCP server instance. The name shows up to any MCP client that connects.
mcp = FastMCP("pm-delivery-tools")


@mcp.tool()
def get_project_status(project_key: str) -> str:
    """
    Get milestone and delivery status for a project from Jira.
    Use when the user asks about progress, status, deadlines, or how a project is going.

    Args:
        project_key: The Jira project key, e.g. "EPM".
    """
    data = jira_client.get_project_status(project_key)
    return json.dumps(data)


@mcp.tool()
def flag_risks(project_key: str) -> str:
    """
    Identify and flag risks in a project based on Jira data
    (high-priority unresolved issues, overdue items, blockers).
    Use when the user asks about risks, blockers, or project health.

    Args:
        project_key: The Jira project key, e.g. "EPM".
    """
    data = jira_client.get_risks(project_key)
    return json.dumps(data)


@mcp.tool()
def request_approval(project_key: str, summary: str) -> str:
    """
    Send a Human-in-the-Loop (HITL) approval request to Slack.
    Use ONLY when a high-risk finding needs human sign-off before proceeding.

    Args:
        project_key: The Jira project key, e.g. "EPM".
        summary: A short description of what needs approval and why.
    """
    data = slack_client.post_approval(project_key, summary)
    return json.dumps(data)


if __name__ == "__main__":
    # Runs the MCP server over stdio (the local transport).
    mcp.run()
