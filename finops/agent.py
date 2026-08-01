"""
FinOpsAgent — conversational AI agent for Cloud FinOps questions.

Answers questions about AWS cost visibility, anomalies, and rightsizing by
injecting a live snapshot (built from real Cost Explorer / Compute Optimizer
data via finops/services.py) into every LLM call — same context-injection
pattern as talent/agent.py.

Never claims to be the PM or Talent agent; identifies itself as the
Cloud FinOps Agent.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

from interfaces.llm import LLMClient, LLMResponse
from interfaces.storage import SessionStore
from finops.services import FinOpsService

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are the Cloud FinOps Agent, an AI assistant specialising in AWS cost visibility, \
anomaly detection, and rightsizing for cloud infrastructure spend.

You have real-time access to this account's live AWS Cost Explorer, Compute Optimizer, \
and EC2/EBS inventory data, injected below. All numbers come directly from AWS APIs — \
if a signal isn't available (e.g. Compute Optimizer not enrolled, no anomaly monitors \
configured, insufficient forecast history), the data says so explicitly; do not invent \
numbers to fill the gap.

CAPABILITIES:
- Total spend: month-to-date, trailing 30 days, daily average, by service
- Cost anomalies: statistically significant spend spikes, with the day and scope flagged
- Rightsizing: Compute Optimizer EC2 recommendations, stopped/idle instances, unattached EBS volumes, with $/mo savings estimates
- Budget: target vs. actual spend, forecast month-end total, on-track/at-risk/over-budget status
- There is a companion AWS FinOps Agent web app the user can open directly for deeper AWS-native analysis \
(a link is provided in the UI) — you can mention it exists but you don't have access to it yourself.

RESPONSE STYLE:
- Be specific and data-driven; cite real dollar amounts, service names, and dates from the data
- Keep answers concise; use bullet points for lists of services/anomalies/recommendations
- If asked "why did costs spike," walk through the anomaly's expected vs actual amount and the affected scope
- If a panel has no data (e.g. "not enrolled in Compute Optimizer"), say so plainly rather than guessing
- Do NOT make up cost figures; only use what's in the context below

You are NOT the PM agent or the Talent agent. For project status or staffing questions, \
tell the user to ask the relevant agent.

--- LIVE AWS FINOPS DATA ---
{context}
--- END DATA ---
"""

_MAX_HISTORY = 16


class FinOpsAgent:
    def __init__(self, llm: LLMClient, store: SessionStore, memory: Any = None) -> None:
        self._llm = llm
        self._store = store
        self._memory = memory
        self._svc = FinOpsService()

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    def _build_context(self) -> str:
        stats = self._svc.get_dashboard()
        cs = stats.cost_summary
        lines: list[str] = []

        lines.append(f"TODAY: {date.today().isoformat()}")
        lines.append(f"ACCOUNT: {cs.account_id or 'unknown'}")
        lines.append(f"PERIOD: {cs.period_start} to {cs.period_end}")
        lines.append(
            f"SPEND: ${cs.total_mtd:.2f} month-to-date, ${cs.total_30d:.2f} trailing 30 days, "
            f"${cs.daily_avg:.2f}/day average"
        )
        lines.append("")

        lines.append("SPEND BY SERVICE (30d):")
        if cs.by_service:
            for s in cs.by_service[:15]:
                lines.append(f"  {s.service}: ${s.amount:.2f} ({s.pct_of_total:.1f}%)")
        else:
            lines.append("  (no service-level spend in this window)")
        lines.append("")

        lines.append(f"ACTIVE AWS SERVICES ({len(cs.active_services)} with usage in this window, cost or not):")
        lines.append("  " + (", ".join(cs.active_services) if cs.active_services else "(none)"))
        lines.append("")

        lines.append("DAILY TREND (most recent 10 days):")
        for p in cs.daily_trend[-10:]:
            lines.append(f"  {p.date}: ${p.amount:.2f}")
        lines.append("")

        lines.append(f"ANOMALY MONITORS CONFIGURED IN AWS: {stats.anomalies.monitors_configured}")
        lines.append(f"ANOMALIES DETECTED: {len(stats.anomalies.anomalies)}")
        for a in stats.anomalies.anomalies[:10]:
            lines.append(
                f"  [{a.severity.upper()}] {a.date} — {a.scope}: actual ${a.actual_amount:.2f} "
                f"vs expected ${a.expected_amount:.2f} ({a.delta_pct:+.1f}%). "
                f"Root cause: {a.root_cause} Source: {a.source}."
            )
        if not stats.anomalies.anomalies:
            lines.append(f"  {stats.anomalies.note or 'None.'}")
        lines.append("")

        lines.append(f"COMPUTE OPTIMIZER ENROLLED: {stats.rightsizing.compute_optimizer_enrolled}")
        lines.append(f"EC2 INSTANCE COUNT: {stats.rightsizing.ec2_instance_count}")
        lines.append(f"RIGHTSIZING RECOMMENDATIONS: {len(stats.rightsizing.recommendations)}")
        for r in stats.rightsizing.recommendations[:15]:
            lines.append(
                f"  {r.resource_type} {r.resource_id}: {r.current_spec} -> {r.recommended_spec} "
                f"| est. savings ${r.estimated_monthly_savings:.2f}/mo | {r.reason}"
            )
        if not stats.rightsizing.recommendations:
            lines.append(f"  {stats.rightsizing.note or 'None found.'}")
        lines.append(f"TOTAL POTENTIAL MONTHLY SAVINGS: ${stats.rightsizing.total_estimated_monthly_savings:.2f}")
        lines.append("")

        b = stats.budget
        if b.target_monthly_budget:
            lines.append(
                f"BUDGET: target ${b.target_monthly_budget:.2f}/mo | MTD ${b.mtd_spend:.2f} "
                f"({b.pct_used:.1f}% used) | forecast month-end ${b.forecast_month_end or 0:.2f} "
                f"({b.forecast_source}) | status: {b.status}"
            )
        else:
            lines.append("BUDGET: no target monthly budget configured")
        if b.aws_budgets:
            lines.append("AWS BUDGETS (configured in AWS Budgets console):")
            for ab in b.aws_budgets:
                lines.append(f"  {ab['name']}: limit ${ab['limit']:.2f}, actual ${ab['actual_spend']:.2f}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    async def chat(
        self,
        session_id: str,
        user_message: str,
        user_id: str | None = None,
    ) -> tuple[str, str]:
        """Process one user turn and return (reply, intent="finops")."""
        history: list[dict[str, Any]] = self._store.get_history(session_id)
        trimmed = history[-_MAX_HISTORY:] if len(history) > _MAX_HISTORY else history

        if self._memory:
            prefix = self._memory.build_context(session_id, history, user_id)
            messages_for_llm = (prefix + list(trimmed) if prefix else list(trimmed)) + [
                {"role": "user", "content": user_message}
            ]
        else:
            messages_for_llm = list(trimmed) + [{"role": "user", "content": user_message}]

        context = await asyncio.to_thread(self._build_context)
        system_prompt = _SYSTEM_PROMPT.format(context=context)

        try:
            response: LLMResponse = await asyncio.to_thread(
                self._llm.generate,
                system_prompt,
                messages_for_llm,
                [],
            )
            reply = response.text.strip() or "[No response generated.]"
        except Exception as exc:
            log.exception("[FINOPS_AGENT] session=%s llm error: %s", session_id, exc)
            reply = f"[FinOps Agent Error] {type(exc).__name__}: {exc}"

        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})
        self._store.save_history(session_id, history)

        if self._memory:
            asyncio.create_task(
                self._memory.update_after_turn(
                    session_id, user_id, user_message, reply, list(history)
                )
            )

        return reply, "finops"
