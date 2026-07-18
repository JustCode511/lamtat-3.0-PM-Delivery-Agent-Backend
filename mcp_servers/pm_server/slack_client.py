"""
Slack connector — posts messages to a channel via Incoming Webhook.

Called by the MCP server's HITL tool. Uses httpx (cross-platform).
If the webhook isn't configured, returns a marker so the system still runs.
"""
from __future__ import annotations
import os
from typing import Any

import httpx


def _webhook() -> str:
    url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not url or "hooks.slack.com/services/xxx" in url:
        return ""  # not configured yet
    return url


def post_approval(project: str, summary: str) -> dict[str, Any]:
    """Send a Human-in-the-Loop approval request to Slack."""
    url = _webhook()
    if not url:
        return {
            "sent": False,
            "note": "Slack not configured yet — would have sent an approval request.",
            "preview": f"[{project}] {summary}",
        }
    message = {
        "text": (
            f":warning: *Approval required — {project}*\n"
            f"{summary}\n\n"
            f"_Reply `approve` or `reject` in the chat to proceed._"
        )
    }
    with httpx.Client(timeout=10) as client:
        resp = client.post(url, json=message)
        resp.raise_for_status()
    return {"sent": True, "channel": "configured webhook channel"}
