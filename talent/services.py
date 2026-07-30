"""
Service layer for the Talent Management module.

All business logic lives here — repositories handle raw persistence,
routes handle HTTP concerns, and this layer handles everything in between.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Optional

from talent.models import (
    AllocationCreate,
    BenchEmployee,
    DashboardStats,
    Employee,
    EmployeeUpdate,
    ForecastBucket,
    MatchCandidate,
    SimulationResult,
    SkillMatrix,
    SkillMatrixEntry,
    SyncResult,
    TalentInsights,
)
from talent.repository import (
    AllocationRepository,
    EmployeeRepository,
    ProjectRepository,
    SkillRepository,
)

log = logging.getLogger(__name__)

_TODAY = date.today()


def _parse_date(ds: str) -> Optional[date]:
    if not ds:
        return None
    try:
        return date.fromisoformat(ds)
    except (ValueError, TypeError):
        return None


def _days_until(ds: str) -> int:
    d = _parse_date(ds)
    if d is None:
        return 9999
    return (d - _TODAY).days


class TalentService:
    def __init__(self) -> None:
        self.employee_repo = EmployeeRepository()
        self.project_repo = ProjectRepository()
        self.allocation_repo = AllocationRepository()
        self.skill_repo = SkillRepository()

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------

    def get_dashboard(self) -> DashboardStats:
        employees = self.employee_repo.list_all()
        projects = self.project_repo.list_active()

        status_breakdown: dict[str, int] = defaultdict(int)
        department_breakdown: dict[str, int] = defaultdict(int)
        level_breakdown: dict[str, int] = defaultdict(int)
        skill_counter: Counter = Counter()
        total_util = 0
        total_capacity = 0
        free_in_30 = 0
        overallocated = 0
        rolling_off_soon: list[dict] = []
        bench_employees: list[dict] = []

        for emp in employees:
            status_breakdown[emp.status] += 1
            department_breakdown[emp.department] += 1
            level_breakdown[emp.career_level] += 1
            total_util += emp.utilization_pct
            total_capacity += emp.capacity_hours
            for s in emp.primary_skills:
                skill_counter[s] += 1
            # Free in 30 days: currently allocated but rolling off within 30 days
            if emp.status == "allocated":
                days = _days_until(emp.availability_date)
                if 0 <= days <= 30:
                    free_in_30 += 1
                    rolling_off_soon.append({
                        "id": emp.id,
                        "name": emp.name,
                        "designation": emp.designation,
                        "department": emp.department,
                        "current_project": emp.current_project,
                        "availability_date": emp.availability_date,
                        "days_left": days,
                        "primary_skills": emp.primary_skills[:6],
                        "secondary_skills": emp.secondary_skills[:4],
                        "location": emp.location,
                    })
            # Bench employees
            if emp.status == "bench":
                bench_employees.append({
                    "id": emp.id,
                    "name": emp.name,
                    "designation": emp.designation,
                    "department": emp.department,
                    "location": emp.location,
                    "primary_skills": emp.primary_skills[:6],
                    "secondary_skills": emp.secondary_skills[:4],
                    "availability_pct": emp.availability_pct,
                    "career_level": emp.career_level,
                })
            # Overallocated: allocation_pct > 100
            if emp.allocation_pct > 100:
                overallocated += 1

        rolling_off_soon.sort(key=lambda x: x["days_left"])

        avg_util = round(total_util / len(employees), 1) if employees else 0.0

        # Skill coverage: what % of all active project required skills are covered by the team
        all_project_skills: set[str] = set()
        for proj in projects:
            all_project_skills.update(s.lower() for s in proj.required_skills)

        team_skills: set[str] = set()
        for emp in employees:
            team_skills.update(s.lower() for s in emp.primary_skills)
            team_skills.update(s.lower() for s in emp.secondary_skills)

        skill_coverage = (
            round(len(all_project_skills & team_skills) / len(all_project_skills) * 100, 1)
            if all_project_skills
            else 100.0
        )

        top_skills = [
            {"skill": skill, "count": count}
            for skill, count in skill_counter.most_common(10)
        ]

        return DashboardStats(
            total_employees=len(employees),
            available_employees=status_breakdown.get("available", 0),
            allocated_employees=status_breakdown.get("allocated", 0),
            bench_count=status_breakdown.get("bench", 0),
            on_leave_count=status_breakdown.get("on_leave", 0),
            avg_utilization=avg_util,
            capacity_remaining_hours=total_capacity,
            free_in_30_days=free_in_30,
            rolling_off_soon=rolling_off_soon,
            bench_employees=bench_employees,
            skill_coverage_pct=skill_coverage,
            overallocated_count=overallocated,
            top_skills=top_skills,
            status_breakdown=dict(status_breakdown),
            department_breakdown=dict(department_breakdown),
            level_breakdown=dict(level_breakdown),
        )

    # ------------------------------------------------------------------
    # Skill Matrix
    # ------------------------------------------------------------------

    def get_skill_matrix(
        self,
        department: Optional[str] = None,
        status: Optional[str] = None,
    ) -> SkillMatrix:
        employees = self.employee_repo.list_all()
        if department:
            employees = [e for e in employees if e.department == department]
        if status:
            employees = [e for e in employees if e.status == status]

        skills = self.skill_repo.list_all()

        # Build skills_by_category map
        skills_by_category: dict[str, list[str]] = defaultdict(list)
        for skill in skills:
            skills_by_category[skill.category].append(skill.name)

        entries: list[SkillMatrixEntry] = []
        for emp in employees:
            primary_set = {s.lower() for s in emp.primary_skills}
            secondary_set = {s.lower() for s in emp.secondary_skills}
            skill_levels: dict[str, str] = {}
            for skill in skills:
                sl = skill.name.lower()
                if sl in primary_set:
                    skill_levels[skill.name] = "primary"
                elif sl in secondary_set:
                    skill_levels[skill.name] = "secondary"
                else:
                    skill_levels[skill.name] = ""
            entries.append(SkillMatrixEntry(
                employee_id=emp.id,
                employee_name=emp.name,
                department=emp.department,
                status=emp.status,
                career_level=emp.career_level,
                skill_levels=skill_levels,
            ))

        return SkillMatrix(
            skills_by_category={k: v for k, v in skills_by_category.items()},
            employees=entries,
        )

    # ------------------------------------------------------------------
    # Matching / Recommendation
    # ------------------------------------------------------------------

    def get_matching(
        self,
        required_skills: list[str],
        location: Optional[str] = None,
        min_experience: int = 0,
    ) -> list[MatchCandidate]:
        """Return ranked candidate list with multi-factor match scores.

        Weights:
          skill_match      40%
          availability     25%
          experience       15%
          certification    10%
          location         10%
        """
        employees = self.employee_repo.list_all()
        req_lower = [s.lower() for s in required_skills]

        candidates: list[MatchCandidate] = []
        for emp in employees:
            if emp.experience_years < min_experience:
                continue
            if emp.status in ("on_leave",):
                continue

            # Skill match
            primary_lower = {s.lower() for s in emp.primary_skills}
            secondary_lower = {s.lower() for s in emp.secondary_skills}
            all_lower = primary_lower | secondary_lower

            matched = [s for s in required_skills if s.lower() in all_lower]
            missing = [s for s in required_skills if s.lower() not in all_lower]
            skill_pct = len(matched) / len(required_skills) * 100 if required_skills else 100.0
            skill_score = skill_pct / 100.0

            # Availability score
            days_until_free = _days_until(emp.availability_date)
            if emp.status in ("available", "bench") or emp.allocation_pct == 0:
                avail_score = 1.0
            elif days_until_free <= 0:
                avail_score = 1.0
            elif days_until_free <= 30:
                avail_score = 0.8
            elif days_until_free <= 60:
                avail_score = 0.5
            elif days_until_free <= 90:
                avail_score = 0.3
            else:
                avail_score = 0.1

            # Experience score (normalised to 10 years max)
            exp_score = min(emp.experience_years / 10.0, 1.0)

            # Certification score
            cert_count = len(emp.certifications)
            cert_score = min(cert_count / 2.0, 1.0)

            # Location score
            if location:
                loc_score = 1.0 if emp.location.lower() == location.lower() or emp.country.lower() == location.lower() else 0.3
            else:
                loc_score = 1.0

            match_score = (
                skill_score * 0.40
                + avail_score * 0.25
                + exp_score * 0.15
                + cert_score * 0.10
                + loc_score * 0.10
            ) * 100

            # Build reasoning string
            parts: list[str] = []
            if skill_pct >= 80:
                parts.append(f"strong skill match ({skill_pct:.0f}%)")
            elif skill_pct >= 50:
                parts.append(f"partial skill match ({skill_pct:.0f}%)")
            else:
                parts.append(f"limited skill match ({skill_pct:.0f}%)")

            if emp.status in ("available", "bench"):
                parts.append("immediately available")
            elif days_until_free <= 30:
                parts.append(f"free in {days_until_free} days")
            else:
                parts.append(f"allocated until {emp.availability_date}")

            if cert_count > 0:
                parts.append(f"{cert_count} relevant certification(s)")

            reasoning = "; ".join(parts)

            candidates.append(MatchCandidate(
                employee=emp,
                match_score=round(match_score, 1),
                skill_match_pct=round(skill_pct, 1),
                availability_score=round(avail_score * 100, 1),
                experience_score=round(exp_score * 100, 1),
                certification_score=round(cert_score * 100, 1),
                location_score=round(loc_score * 100, 1),
                missing_skills=missing,
                matching_skills=matched,
                reasoning=reasoning,
            ))

        candidates.sort(key=lambda c: c.match_score, reverse=True)
        return candidates

    # ------------------------------------------------------------------
    # Bench management
    # ------------------------------------------------------------------

    def get_bench(self) -> list[BenchEmployee]:
        bench_employees = self.employee_repo.list_bench()
        projects = self.project_repo.list_all()
        allocations = self.allocation_repo.list_all()
        skills = self.skill_repo.list_all()

        high_demand_skill_names = {s.name.lower() for s in skills if s.demand_level == "high"}

        result: list[BenchEmployee] = []
        for emp in bench_employees:
            # Compute bench duration
            employee_allocs = [a for a in allocations if a.employee_id == emp.id]
            if employee_allocs:
                last_end = max(
                    _parse_date(a.end_date) or _TODAY
                    for a in employee_allocs
                )
                bench_days = max((_TODAY - last_end).days, 0)
            else:
                join = _parse_date(emp.join_date) or _TODAY
                bench_days = (_TODAY - join).days

            # Suggested projects: projects whose required skills overlap with employee's skills
            emp_skills_lower = {s.lower() for s in emp.primary_skills + emp.secondary_skills}
            suggested: list[str] = []
            for proj in projects:
                if proj.status not in ("active", "planning"):
                    continue
                proj_skills_lower = {s.lower() for s in proj.required_skills}
                overlap = emp_skills_lower & proj_skills_lower
                if overlap:
                    suggested.append(f"{proj.id} ({proj.name}) — matches: {', '.join(s for s in emp.primary_skills if s.lower() in proj_skills_lower)}")

            # Recommended upskilling: high-demand skills the employee doesn't have
            missing_high_demand = [
                s.name for s in skills
                if s.demand_level == "high" and s.name.lower() not in emp_skills_lower
            ][:5]

            result.append(BenchEmployee(
                employee=emp,
                bench_duration_days=bench_days,
                suggested_projects=suggested[:5],
                recommended_upskilling=missing_high_demand,
            ))

        result.sort(key=lambda b: b.bench_duration_days, reverse=True)
        return result

    # ------------------------------------------------------------------
    # Forecast
    # ------------------------------------------------------------------

    def get_forecast(self) -> list[ForecastBucket]:
        employees = self.employee_repo.list_all()
        projects = self.project_repo.list_all()

        buckets: list[ForecastBucket] = []
        for label, days in [("30_days", 30), ("60_days", 60), ("90_days", 90)]:
            cutoff = _TODAY + timedelta(days=days)

            rolling_off = []
            for emp in employees:
                if emp.status == "allocated":
                    avail_date = _parse_date(emp.availability_date)
                    if avail_date and _TODAY < avail_date <= cutoff:
                        rolling_off.append({
                            "employee_id": emp.id,
                            "name": emp.name,
                            "designation": emp.designation,
                            "project": emp.current_project,
                            "availability_date": emp.availability_date,
                            "skills": emp.primary_skills[:3],
                        })

            ending_projects = []
            for proj in projects:
                if proj.status == "active":
                    end = _parse_date(proj.end_date)
                    if end and _TODAY < end <= cutoff:
                        ending_projects.append({
                            "project_id": proj.id,
                            "name": proj.name,
                            "end_date": proj.end_date,
                            "team_size": proj.team_size,
                            "client": proj.client,
                        })

            capacity_added = sum(
                int((emp.allocation_pct / 100) * 40)
                for emp in employees
                if emp.status == "allocated"
                and _parse_date(emp.availability_date) is not None
                and _TODAY < (_parse_date(emp.availability_date) or _TODAY) <= cutoff
            )

            buckets.append(ForecastBucket(
                period=label,
                employees_rolling_off=rolling_off,
                projects_ending=ending_projects,
                capacity_added_hours=capacity_added,
                headcount_freed=len(rolling_off),
            ))

        return buckets

    # ------------------------------------------------------------------
    # Insights
    # ------------------------------------------------------------------

    def get_insights(self) -> TalentInsights:
        employees = self.employee_repo.list_all()
        projects = self.project_repo.list_active()
        skills = self.skill_repo.list_all()

        # Top skills across primary skills
        skill_counter: Counter = Counter()
        skill_holders: dict[str, list[str]] = defaultdict(list)
        for emp in employees:
            for s in emp.primary_skills:
                skill_counter[s] += 1
                skill_holders[s.lower()].append(emp.name)

        top_skills = [
            {"skill": skill, "count": count, "holders": skill_holders[skill.lower()][:3]}
            for skill, count in skill_counter.most_common(15)
        ]

        # Skill gaps: required by active projects but weak or missing in team
        all_project_skills: dict[str, list[str]] = defaultdict(list)  # skill -> project names
        for proj in projects:
            for s in proj.required_skills:
                all_project_skills[s.lower()].append(proj.name)

        # Build a case-insensitive -> canonical skill name map
        skill_lower_map: dict[str, int] = {}
        for skill, count in skill_counter.items():
            skill_lower_map[skill.lower()] = skill_lower_map.get(skill.lower(), 0) + count

        skill_gaps = []
        for skill_name, proj_names in all_project_skills.items():
            holders = skill_lower_map.get(skill_name.lower(), 0)
            if holders < 2:
                skill_gaps.append({
                    "skill": skill_name,
                    "used_in_projects": proj_names[:3],
                    "holder_count": holders,
                    "severity": "critical" if holders == 0 else "high",
                })
        skill_gaps.sort(key=lambda g: g["holder_count"])

        # Critical risks: employees who are the ONLY holder of a required skill
        critical_risks = []
        for emp in employees:
            sole_skills = []
            for s in emp.primary_skills:
                if skill_counter[s] == 1 and s.lower() in all_project_skills:
                    sole_skills.append(s)
            if sole_skills:
                critical_risks.append({
                    "employee_id": emp.id,
                    "employee_name": emp.name,
                    "status": emp.status,
                    "sole_skills": sole_skills,
                    "risk": f"Single point of failure for: {', '.join(sole_skills)}",
                })

        # Rare skill holders: employees with very specialised skills (count == 1 across team)
        rare_skill_holders = []
        for skill_name, holder_names in skill_holders.items():
            if len(holder_names) == 1:
                rare_skill_holders.append({
                    "skill": skill_name,
                    "holder": holder_names[0],
                    "in_demand": skill_name in [s.name.lower() for s in skills if s.demand_level == "high"],
                })

        # Recommended hiring: high-demand skills with fewer than 2 holders
        high_demand_names = {s.name for s in skills if s.demand_level == "high"}
        recommended_hiring = [
            sname for sname in high_demand_names
            if skill_counter.get(sname, 0) < 2
        ][:10]

        return TalentInsights(
            top_skills=top_skills,
            skill_gaps=skill_gaps[:10],
            critical_risks=critical_risks,
            rare_skill_holders=rare_skill_holders[:10],
            recommended_hiring=recommended_hiring,
        )

    # ------------------------------------------------------------------
    # Simulation
    # ------------------------------------------------------------------

    def simulate_allocation(
        self,
        employee_id: str,
        project_id: str,
        allocation_pct: int,
    ) -> SimulationResult:
        """Preview the impact of allocating an employee to a project.

        Does NOT write to disk — returns what would happen if the allocation
        were applied. Use POST /talent/employees/{id} to persist changes.
        """
        emp = self.employee_repo.get(employee_id)
        if emp is None:
            return SimulationResult(
                feasible=False,
                employee_id=employee_id,
                project_id=project_id,
                requested_allocation_pct=allocation_pct,
                current_allocation_pct=0,
                new_total_allocation_pct=allocation_pct,
                remaining_capacity_pct=0,
                conflicts=["Employee not found"],
                impact_summary=f"Employee {employee_id} does not exist.",
            )

        proj = self.project_repo.get(project_id)
        if proj is None:
            return SimulationResult(
                feasible=False,
                employee_id=employee_id,
                project_id=project_id,
                requested_allocation_pct=allocation_pct,
                current_allocation_pct=emp.allocation_pct,
                new_total_allocation_pct=emp.allocation_pct + allocation_pct,
                remaining_capacity_pct=emp.availability_pct - allocation_pct,
                conflicts=["Project not found"],
                impact_summary=f"Project {project_id} does not exist.",
            )

        existing_allocs = self.allocation_repo.list_by_employee(employee_id)
        current_total = sum(a.allocation_pct for a in existing_allocs)
        new_total = current_total + allocation_pct
        remaining = 100 - new_total

        conflicts: list[str] = []
        if emp.status == "on_leave":
            conflicts.append(f"{emp.name} is currently on leave.")
        if new_total > 100:
            conflicts.append(
                f"Over-allocation: {new_total}% total exceeds 100% capacity."
            )
        if emp.status == "allocated" and emp.current_project:
            conflicts.append(
                f"Already allocated to {emp.current_project} at {emp.allocation_pct}%."
            )

        feasible = len(conflicts) == 0 or (
            len(conflicts) == 1 and "Already allocated" in conflicts[0] and new_total <= 100
        )

        summary_parts = [
            f"{emp.name} ({emp.designation}) would be {new_total}% allocated.",
            f"Remaining capacity: {max(remaining, 0)}%.",
        ]
        if conflicts:
            summary_parts.append("Conflicts: " + "; ".join(conflicts))

        return SimulationResult(
            feasible=feasible,
            employee_id=employee_id,
            project_id=project_id,
            requested_allocation_pct=allocation_pct,
            current_allocation_pct=current_total,
            new_total_allocation_pct=new_total,
            remaining_capacity_pct=max(remaining, 0),
            conflicts=conflicts,
            impact_summary=" ".join(summary_parts),
        )

    # ------------------------------------------------------------------
    # Jira Sync
    # ------------------------------------------------------------------

    def sync_from_jira(self) -> SyncResult:
        """Sync talent allocations from real Jira project data.

        Delegates to talent.jira_sync which handles all Jira API interactions
        and fuzzy-matches Jira assignees back to our employees.json.
        """
        from talent.jira_sync import run_sync
        return run_sync(self.employee_repo, self.allocation_repo)
