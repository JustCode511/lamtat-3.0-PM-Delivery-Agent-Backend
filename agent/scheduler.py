"""
APScheduler setup — weekly PM report job.

Registered in the FastAPI lifespan so it starts/stops with the server.
Uses AsyncIOScheduler so jobs run on the same event loop as FastAPI.
"""
from __future__ import annotations
import logging
import os
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from interfaces.llm import LLMClient

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def build(llm: LLMClient, mcp: Any) -> AsyncIOScheduler:
    """Create and configure the scheduler. Call once at app startup."""
    global _scheduler
    from agent.report_automation import run_weekly_report_job

    tz = os.getenv("SCHEDULER_TIMEZONE", "Asia/Kolkata")
    _scheduler = AsyncIOScheduler(timezone=tz)

    # ── Production job: every Friday at 15:00 ────────────────────────────
    _scheduler.add_job(
        run_weekly_report_job,
        CronTrigger(day_of_week="fri", hour=15, minute=0, timezone=tz),
        args=[llm, mcp],
        id="weekly_report_friday",
        replace_existing=True,
        name="Weekly PM Leadership Report",
    )

    log.info("[SCHEDULER] Registered weekly report job — every Friday 15:00 %s", tz)
    return _scheduler


def get() -> AsyncIOScheduler | None:
    return _scheduler
