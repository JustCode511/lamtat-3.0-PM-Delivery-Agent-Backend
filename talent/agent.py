"""
TalentAgent — conversational AI agent for talent management queries.

Answers questions about team availability, skill coverage, allocation,
forecasting, and hiring recommendations by injecting live JSON data
into every LLM call via a structured context snapshot.

Never claims to be the PM agent; identifies itself as the Talent Agent.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta
from typing import Any

from interfaces.llm import LLMClient, LLMResponse, ToolSpec
from interfaces.storage import SessionStore
from talent.repository import (
    AllocationRepository,
    EmployeeRepository,
    ProjectRepository,
    SkillRepository,
)

log = logging.getLogger(__name__)

_TODAY = date.today()
_SYSTEM_PROMPT = """\
You are the Talent Agent, an AI assistant specialising in workforce management, \
resource allocation, and talent insights for a professional services team.

You have real-time access to the team's talent data injected below. \
Use this data to answer questions accurately and concisely.

CAPABILITIES:
- Who is available now, in the next 30/60/90 days, or on bench
- Skill coverage: who has Python, LangChain, Kubernetes, etc.
- Project allocations: who is on which project, at what %, rolling off when
- Skill matrix: who covers what skills by category
- Candidate matching: best person(s) for a given skill set or project need
- Capacity planning: total available hours, gaps, upcoming free bandwidth
- Bench analysis: who has been on bench and for how long
- Talent insights: skill gaps, single points of failure, hiring recommendations
- Team composition by department, location, career level

RESPONSE STYLE:
- Be specific and data-driven; cite names, percentages, and dates from the data
- Keep answers concise but complete; use bullet points for lists
- If asked for the "best match" for a role, rank 2-3 candidates with reasoning
- When relevant, flag risks (e.g. someone who is both the sole holder of a skill and rolling off soon)
- Do NOT make up data; if something is not in the context, say so
- Do NOT claim to be the PM agent or answer Jira/project-management questions outside talent scope

You are NOT the PM agent. For project status, Jira issues, or risk reports, \
tell the user to ask the PM agent.

--- LIVE TALENT DATA ---
{context}
--- END DATA ---
"""

_MAX_HISTORY = 16  # keep last 8 turns


class TalentAgent:
    def __init__(self, llm: LLMClient, store: SessionStore, memory: Any = None) -> None:
        self._llm = llm
        self._store = store
        self._memory = memory
        self._emp_repo = EmployeeRepository()
        self._proj_repo = ProjectRepository()
        self._alloc_repo = AllocationRepository()
        self._skill_repo = SkillRepository()

    # ------------------------------------------------------------------
    # Context builder
    # ------------------------------------------------------------------

    def _build_context(self) -> str:
        """Build a concise snapshot of live talent data for the LLM."""
        employees = self._emp_repo.list_all()
        projects = self._proj_repo.list_all()
        allocations = self._alloc_repo.list_all()

        lines: list[str] = []

        # -- Team overview --
        total = len(employees)
        available = sum(1 for e in employees if e.status == "available")
        allocated = sum(1 for e in employees if e.status == "allocated")
        bench = sum(1 for e in employees if e.status == "bench")
        on_leave = sum(1 for e in employees if e.status == "on_leave")

        lines.append(f"TEAM OVERVIEW: {total} employees — "
                     f"{available} available, {allocated} allocated, "
                     f"{bench} bench, {on_leave} on leave")
        lines.append(f"TODAY: {_TODAY.isoformat()}")
        lines.append("")

        # -- Per-employee snapshot --
        lines.append("EMPLOYEES:")
        for emp in employees:
            # Availability flag
            days_until = 9999
            avail_date = emp.availability_date
            try:
                avail_d = date.fromisoformat(avail_date)
                days_until = (avail_d - _TODAY).days
            except (ValueError, TypeError):
                pass

            if emp.status in ("available",):
                flag = "AVAILABLE NOW"
            elif emp.status == "bench":
                flag = "BENCH (available now)"
            elif emp.status == "on_leave":
                flag = f"ON LEAVE until {avail_date}"
            elif emp.status == "allocated" and days_until <= 30:
                flag = f"FREE IN {days_until}d (from {emp.current_project})"
            elif emp.status == "allocated":
                flag = f"ALLOCATED to {emp.current_project} until {avail_date} ({emp.allocation_pct}%)"
            else:
                flag = emp.status.upper()

            skills_str = ", ".join(emp.primary_skills[:5])
            certs_str = f" | certs: {', '.join(emp.certifications)}" if emp.certifications else ""
            lines.append(
                f"  {emp.id} | {emp.name} | {emp.designation} | {emp.career_level} | "
                f"{emp.location}, {emp.country} | {flag} | "
                f"skills: {skills_str}{certs_str} | "
                f"exp: {emp.experience_years}yr | capacity: {emp.weekly_capacity}h/wk"
            )

        lines.append("")

        # -- Projects --
        lines.append("ACTIVE PROJECTS:")
        for proj in projects:
            if proj.status in ("active", "planning"):
                skills_str = ", ".join(proj.required_skills[:5])
                team_str = ", ".join(proj.allocated_employees[:5])
                lines.append(
                    f"  {proj.id} | {proj.name} | {proj.client} | {proj.status.upper()} | "
                    f"end: {proj.end_date} | team: {team_str or 'TBD'} | "
                    f"required skills: {skills_str}"
                )

        lines.append("")

        # -- Skill coverage summary --
        from collections import Counter
        skill_counter: Counter = Counter()
        for emp in employees:
            for s in emp.primary_skills:
                skill_counter[s] += 1

        top_skills = skill_counter.most_common(15)
        top_str = ", ".join(f"{s}({c})" for s, c in top_skills)
        lines.append(f"TOP SKILLS (count of primary holders): {top_str}")
        lines.append("")

        # -- Upcoming availability (next 30 days) --
        rolling_off_soon: list[str] = []
        for emp in employees:
            if emp.status == "allocated":
                try:
                    d = date.fromisoformat(emp.availability_date)
                    days = (d - _TODAY).days
                    if 0 <= days <= 30:
                        rolling_off_soon.append(
                            f"{emp.name} ({emp.designation}) from {emp.current_project} in {days}d"
                        )
                except (ValueError, TypeError):
                    pass

        if rolling_off_soon:
            lines.append("ROLLING OFF IN 30 DAYS:")
            for r in rolling_off_soon:
                lines.append(f"  - {r}")
        else:
            lines.append("ROLLING OFF IN 30 DAYS: none")

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
        """Process one user turn and return (reply, intent="talent")."""
        history: list[dict[str, Any]] = self._store.get_history(session_id)
        trimmed = history[-_MAX_HISTORY:] if len(history) > _MAX_HISTORY else history

        # Prepend long-term memory context (session summary + user facts)
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
                [],  # no tools needed — all data is injected in context
            )
            reply = response.text.strip() or "[No response generated.]"
        except Exception as exc:
            log.exception("[TALENT_AGENT] session=%s llm error: %s", session_id, exc)
            reply = f"[Talent Agent Error] {type(exc).__name__}: {exc}"

        # Persist turn
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})
        self._store.save_history(session_id, history)

        # Fire-and-forget memory update
        if self._memory:
            asyncio.create_task(
                self._memory.update_after_turn(
                    session_id, user_id, user_message, reply, list(history)
                )
            )

        return reply, "talent"
