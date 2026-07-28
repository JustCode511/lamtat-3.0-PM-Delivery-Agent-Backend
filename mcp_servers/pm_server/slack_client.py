"""
Slack connector — posts messages via Incoming Webhook (HITL approvals)
and the Slack Web API (general notifications).

Uses httpx (cross-platform). If credentials aren't configured, returns a
preview marker so the system still runs without Slack.
"""
from __future__ import annotations
import os
import re
from typing import Any

import httpx


def _mermaid_image_block(code: str) -> dict:
    """
    Render a Mermaid diagram as a PNG via mermaid.ink and return a Slack image block.
    mermaid.ink is a free public rendering service — no API key required.
    """
    import base64
    import json as _json

    # Extract a human-readable title for alt text
    title_m = re.search(r'(?:pie\s+)?title\s+"?([^"\n]+)"?', code)
    alt = title_m.group(1).strip() if title_m else "Chart"

    # mermaid.ink accepts a JSON payload: {"code": "...", "mermaid": {config}}
    payload = _json.dumps({"code": code, "mermaid": {"theme": "dark"}}, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    url = f"https://mermaid.ink/img/{encoded}"

    return {
        "type": "image",
        "image_url": url,
        "alt_text": alt,
        "title": {"type": "plain_text", "text": f"📊  {alt}"},
    }


def _md_to_blocks(text: str) -> list[dict]:  # noqa: C901
    """
    Convert a markdown PM report into rich Slack Block Kit blocks.

    - ## Heading      → header block
    - ### Heading     → bold mrkdwn
    - **bold**        → *bold*
    - - / * bullets   → • bullets
    - | table |       → formatted text table with bold headers
    - ```mermaid …``` → real PNG image via mermaid.ink
    - other code      → stripped
    """

    def _mrkdwn(s: str) -> str:
        s = re.sub(r"\*\*(.+?)\*\*", r"*\1*", s)
        s = re.sub(r"^[*-]\s+", "• ", s, flags=re.MULTILINE)
        return s.strip()

    def _parse_table(lines: list[str]) -> str:
        rows: list[list[str]] = []
        for ln in lines:
            if re.match(r"^[\s|:\-]+$", ln):   # skip separator rows
                continue
            cells = [re.sub(r"\*\*(.+?)\*\*", r"*\1*", c.strip())
                     for c in ln.strip().strip("|").split("|")]
            if any(cells):
                rows.append(cells)
        if not rows:
            return ""
        header, data = rows[0], rows[1:]
        lines_out = [" │ ".join(f"*{h}*" for h in header if h)]
        lines_out.append("─" * 55)
        for row in data[:25]:
            lines_out.append(" │ ".join(c for c in row if c))
        if len(data) > 25:
            lines_out.append(f"_…and {len(data) - 25} more_")
        return "\n".join(lines_out)

    blocks: list[dict] = []
    current: list[str] = []
    table_buf: list[str] = []
    code_buf: list[str] = []
    in_table = False
    in_code = False
    code_lang = ""
    first_heading = True

    def _flush_text() -> None:
        if not current:
            return
        body = _mrkdwn("\n".join(current))
        if body:
            for i in range(0, len(body), 2900):
                blocks.append({"type": "section",
                                "text": {"type": "mrkdwn", "text": body[i:i + 2900]}})
        current.clear()

    def _flush_table() -> None:
        nonlocal in_table
        if table_buf:
            rendered = _parse_table(table_buf)
            if rendered:
                blocks.append({"type": "section",
                                "text": {"type": "mrkdwn", "text": rendered[:2900]}})
        table_buf.clear()
        in_table = False

    for raw in text.splitlines():
        line = raw.rstrip()

        # ── fenced code blocks ──────────────────────────────────────────
        if line.startswith("```"):
            if not in_code:
                _flush_text()
                if in_table:
                    _flush_table()
                code_lang = line[3:].strip().lower()
                in_code = True
                code_buf = []
            else:
                if code_lang == "mermaid" and code_buf:
                    blocks.append(_mermaid_image_block("\n".join(code_buf)))
                # all other code blocks are silently dropped
                in_code = False
                code_lang = ""
                code_buf = []
            continue

        if in_code:
            code_buf.append(line)
            continue

        # ── table rows ─────────────────────────────────────────────────
        is_table_row = bool(re.match(r"^\s*\|.+\|", line))
        if is_table_row:
            if not in_table:
                _flush_text()
                in_table = True
            table_buf.append(line)
            continue
        elif in_table:
            _flush_table()

        # ── headings ───────────────────────────────────────────────────
        if line.startswith("## "):
            _flush_text()
            if not first_heading:
                blocks.append({"type": "divider"})
            first_heading = False
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": line[3:].strip()[:150], "emoji": True},
            })
        elif line.startswith("### "):
            _flush_text()
            sub = re.sub(r"\*\*(.+?)\*\*", r"*\1*", line[4:].strip())
            current.append(f"*{sub}*")
        else:
            current.append(line)

    _flush_text()
    _flush_table()
    return blocks[:50]

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


def send_dm_with_approval_buttons(
    user_id: str,
    report_text: str,
    approval_id: str,
    attempt: int,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """DM the PM with a report preview and Approve/Reject Block Kit buttons."""
    token = _bot_token()
    if not token or token.startswith("xoxb-your"):
        return {"sent": False, "note": "SLACK_BOT_TOKEN not configured"}

    with httpx.Client(timeout=10) as client:
        open_resp = client.post(
            "https://slack.com/api/conversations.open",
            json={"users": user_id},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        open_data = open_resp.json()
    if not open_data.get("ok"):
        return {"sent": False, "error": f"conversations.open: {open_data.get('error')}"}

    dm_channel = open_data["channel"]["id"]
    preview = report_text[:2800] + ("…" if len(report_text) > 2800 else "")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📋 Weekly PM Report — Approval Required"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Attempt *{attempt} of {max_attempts}* · You have *5 minutes* to respond",
                }
            ],
        },
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": preview}},
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "Send this report to the *Leadership channel*?"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅  Approve & Send"},
                    "style": "primary",
                    "action_id": "approve_report",
                    "value": approval_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌  Reject"},
                    "style": "danger",
                    "action_id": "reject_report",
                    "value": approval_id,
                },
            ],
        },
    ]

    payload = {
        "channel": dm_channel,
        "text": f"Weekly PM Report ready for approval (attempt {attempt}/{max_attempts})",
        "blocks": blocks,
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
    return {"sent": True, "channel": dm_channel, "ts": result.get("ts", "")}


def send_dm(user_id: str, message: str) -> dict[str, Any]:
    """Send a plain text DM to a Slack user by their user ID."""
    token = _bot_token()
    if not token or token.startswith("xoxb-your"):
        return {"sent": False, "note": "SLACK_BOT_TOKEN not configured"}

    with httpx.Client(timeout=10) as client:
        open_resp = client.post(
            "https://slack.com/api/conversations.open",
            json={"users": user_id},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        open_data = open_resp.json()
        if not open_data.get("ok"):
            return {"sent": False, "error": f"conversations.open: {open_data.get('error')}"}
        dm_channel = open_data["channel"]["id"]

        resp = client.post(
            _SLACK_API,
            json={"channel": dm_channel, "text": message},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        result = resp.json()

    if not result.get("ok"):
        return {"sent": False, "error": result.get("error")}
    return {"sent": True}


def post_to_channel(channel_id: str, message: str) -> dict[str, Any]:
    """Post a markdown report to a Slack channel using Block Kit for rich rendering."""
    token = _bot_token()
    if not token or token.startswith("xoxb-your"):
        return {"sent": False, "note": "SLACK_BOT_TOKEN not configured"}

    blocks = _md_to_blocks(message)
    # fallback_text is shown in notifications / accessibility — first 300 chars, no markdown
    fallback = re.sub(r'[*_`#|>]', '', message)[:300].strip()

    payload = {
        "channel": channel_id,
        "text": fallback,   # required by Slack; shown when blocks can't render
        "blocks": blocks,
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
    return {"ok": True, "ts": result.get("ts")}


def update_message_with_status(channel_id: str, ts: str, approved: bool) -> None:
    """Replace the approve/reject buttons in a DM with a status stamp."""
    token = _bot_token()
    if not token or ts == "" or channel_id == "":
        return
    status_text = "✅ Approved — Report sent to leadership!" if approved else "❌ Rejected — Report dropped for this week."
    payload = {
        "channel": channel_id,
        "ts": ts,
        "text": status_text,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": status_text}}],
    }
    with httpx.Client(timeout=10) as client:
        client.post(
            "https://slack.com/api/chat.update",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
