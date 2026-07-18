"""
Slack connector — posts messages via Incoming Webhook (HITL approvals)
and the Slack Web API (general notifications).

Uses httpx (cross-platform). If credentials aren't configured, returns a
preview marker so the system still runs without Slack.
"""
from __future__ import annotations
import os
from typing import Any

import httpx

_SLACK_API = "https://slack.com/api/chat.postMessage"


def _webhook() -> str:
    url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not url or "hooks.slack.com/services/xxx" in url:
        return ""
    return url


def _bot_token() -> str:
    return os.getenv("SLACK_BOT_TOKEN", "")


def _channel() -> str:
    return os.getenv("SLACK_CHANNEL_ID", "C0BJ56AD24V")


def post_approval(project: str, summary: str) -> dict[str, Any]:
    """Send a Human-in-the-Loop approval request to Slack via Incoming Webhook."""
    url = _webhook()
    if not url:
        return {
            "sent": False,
            "note": "Slack webhook not configured — would have sent an approval request.",
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


def send_notification(message: str, project: str | None = None) -> dict[str, Any]:
    """Send a general notification to the configured Slack channel via the Web API."""
    token = _bot_token()
    if not token or token.startswith("xoxb-your"):
        return {
            "sent": False,
            "note": "SLACK_BOT_TOKEN not configured — would have sent a notification.",
            "preview": message,
        }
    channel = _channel()
    header = f":bell: *PM Agent Notification{f' — {project}' if project else ''}*\n"
    payload = {
        "channel": channel,
        "text": header + message,
    }
    with httpx.Client(timeout=10) as client:
        resp = client.post(
            _SLACK_API,
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        result = resp.json()
    if not result.get("ok"):
        return {"sent": False, "error": result.get("error", "unknown")}
    return {"sent": True, "channel": channel, "ts": result.get("ts")}
