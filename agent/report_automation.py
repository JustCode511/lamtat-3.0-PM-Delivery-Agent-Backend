"""
Weekly report automation — HITL in-chat approval loop.

Flow (every Friday 15:00 IST):
  1. Fetch all Jira project data via MCP tools
  2. Generate a leadership-ready summary via LLM
  3. Store as a PendingApproval (15-min TTL)
  4. Send Slack DM to PM (one-way heads-up, no callback buttons needed)
  5. Background task sends reminder DMs at +5 min and +10 min if still pending
  6. At +15 min anything still pending is auto-expired

Approval execution (called by the /pm/approvals/{id}/approve endpoint):
  - Posts the approved report text to the Leadership Slack channel
  - Marks the approval as "approved" in the store
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any

from agent import pending_store
from agent.state import llm_generate
from interfaces.llm import LLMClient

log = logging.getLogger(__name__)

_REPORT_PROMPT = """You are a PM Delivery Agent preparing a weekly leadership briefing.

Given the Jira project data below, write a concise, professional summary.
This will be sent directly to leadership stakeholders via Slack.

Required format — use exactly these sections:

## Weekly PM Status Report — {date}

### Portfolio Health
2-3 sentences: overall health signal, trend, and the single biggest concern.

### Project Summary
One bullet per project:
  • **PROJECT_KEY — Project Name**: Health status | X% complete | N overdue items | Key highlight or risk.

### Critical Actions Required
Top 3 items needing leadership attention. Be specific — name the project, the issue, and what decision is needed.

### Recommended Next Steps
2-3 concrete actions for the coming week.

---
Rules:
- Under 500 words total
- No raw JSON, no jargon
- Professional tone — this goes to directors / VPs
"""


async def generate_weekly_report(llm: LLMClient, mcp: Any) -> str:
    """Fetch live Jira data and produce a leadership-ready markdown report."""
    log.info("[REPORT] Fetching project data for weekly report")

    raw = await mcp.call_tool("list_projects", {})
    projects_data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    keys = [p["key"] for p in projects_data.get("projects", []) if p.get("key")]

    if not keys:
        return "No Jira projects found. Please check the Jira configuration."

    # Fetch status for all projects in parallel
    results = await asyncio.gather(
        *[mcp.call_tool("get_project_status", {"project_key": k}) for k in keys],
        return_exceptions=True,
    )

    context_parts: list[str] = []
    for k, result in zip(keys, results):
        if isinstance(result, Exception):
            context_parts.append(f"Project {k}: Unable to fetch data ({result})")
        else:
            context_parts.append(f"=== {k} ===\n{result}")

    context = "\n\n".join(context_parts)
    today = datetime.now().strftime("%B %d, %Y")
    prompt = _REPORT_PROMPT.format(date=today)

    log.info("[REPORT] Generating LLM summary for %d projects", len(keys))
    report = await llm_generate(llm, prompt, context)
    log.info("[REPORT] Report generated, length=%d", len(report))
    return report


async def run_weekly_report_job(llm: LLMClient, mcp: Any) -> None:
    """
    Entry point called by the scheduler every Friday at 15:00.
    Also callable directly via POST /pm/trigger-report for demos.
    """
    log.info("[SCHEDULER] Weekly report job started")

    try:
        report_text = await generate_weekly_report(llm, mcp)
    except Exception as exc:
        log.error("[SCHEDULER] Report generation failed: %s", exc)
        return

    # Expire any leftover pending approvals from previous weeks
    stale = pending_store.expire_stale()
    if stale:
        log.info("[SCHEDULER] Expired %d stale approvals before creating new one", stale)

    approval = pending_store.create(report_text)
    log.info("[SCHEDULER] Pending approval created: %s (expires %s)", approval.id, approval.expires_at)

    # Send first Slack reminder (one-way heads-up, no buttons)
    pm_user_id = os.getenv("PM_SLACK_USER_ID", "")
    _send_reminder_dm(pm_user_id, approval.id, attempt=1)

    # Fire-and-forget background reminders at 5 min and 10 min
    asyncio.create_task(_reminder_loop(approval.id, pm_user_id))

    log.info("[SCHEDULER] Job complete — approval_id=%s", approval.id)


async def _reminder_loop(approval_id: str, pm_user_id: str) -> None:
    """
    Background task: send up to 2 more Slack reminders at 5-min intervals.
    Stops early if the PM has already approved or rejected.
    """
    for attempt in (2, 3):
        await asyncio.sleep(pending_store.ATTEMPT_WINDOW_MINUTES * 60)

        approval = pending_store.get(approval_id)
        if not approval or approval.status != "pending":
            log.info("[REMINDER] Approval %s already %s — stopping reminders",
                     approval_id, getattr(approval, "status", "gone"))
            return

        pending_store.bump_attempt(approval_id)
        log.info("[REMINDER] Sending attempt %d for approval %s", attempt, approval_id)
        _send_reminder_dm(pm_user_id, approval_id, attempt=attempt)

    # Final wait — after this the TTL will have expired
    await asyncio.sleep(pending_store.ATTEMPT_WINDOW_MINUTES * 60)
    expired_count = pending_store.expire_stale()
    if expired_count:
        log.info("[REMINDER] Auto-expired %d unanswered approval(s) after 15 min", expired_count)


def _send_reminder_dm(pm_user_id: str, approval_id: str, attempt: int) -> None:
    """Send a plain Slack DM telling the PM to open the chat and approve."""
    if not pm_user_id:
        log.warning("[REMINDER] PM_SLACK_USER_ID not set — skipping Slack DM")
        return

    messages = {
        1: (
            "📋 *Weekly PM Report Ready* — Your project summary for this week has been generated.\n\n"
            "Open the *PM Agent* and approve it to send to the leadership channel.\n"
            "> _You have 15 minutes before this request expires._"
        ),
        2: (
            "⏰ *Reminder (2/3)* — Your weekly PM report is still waiting for approval.\n\n"
            "Open the *PM Agent* chat to review and approve it.\n"
            "> _10 minutes remaining._"
        ),
        3: (
            "🔔 *Final Reminder (3/3)* — Last chance to approve this week's PM report.\n\n"
            "Open the *PM Agent* now — if not approved in 5 minutes, the report will be dropped.\n"
            "> _5 minutes remaining._"
        ),
    }

    try:
        from mcp_servers.pm_server.slack_client import send_dm
        result = send_dm(pm_user_id, messages.get(attempt, messages[1]))
        if result.get("sent"):
            log.info("[REMINDER] Slack DM sent (attempt %d)", attempt)
        else:
            log.warning("[REMINDER] Slack DM failed: %s", result.get("error") or result.get("note"))
    except Exception as exc:
        log.warning("[REMINDER] Exception sending Slack DM: %s", exc)


async def execute_approval(approval_id: str) -> dict:
    """
    Execute an approved report: post to the Leadership Slack channel.
    Called by POST /pm/approvals/{id}/approve.
    """
    approval = pending_store.get(approval_id)
    if not approval:
        return {"success": False, "error": "Approval not found"}
    if approval.status == "approved":
        return {"success": False, "error": "Already approved"}
    if approval.status in ("rejected", "expired"):
        return {"success": False, "error": f"Approval is {approval.status}"}

    leadership_channel = os.getenv("LEADERSHIP_SLACK_CHANNEL_ID", "")
    if not leadership_channel:
        return {"success": False, "error": "LEADERSHIP_SLACK_CHANNEL_ID not configured"}

    try:
        from mcp_servers.pm_server.slack_client import post_to_channel
        result = post_to_channel(leadership_channel, approval.report_text)
    except Exception as exc:
        log.error("[APPROVE] Slack post failed: %s", exc)
        return {"success": False, "error": str(exc)}

    if not result.get("sent"):
        return {"success": False, "error": result.get("error", "Slack post failed")}

    pending_store.update_status(approval_id, "approved")
    log.info("[APPROVE] Report %s approved and posted to %s", approval_id, leadership_channel)
    return {"success": True, "channel": leadership_channel}
