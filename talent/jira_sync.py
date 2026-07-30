"""
Jira sync module for the Talent Management system.

Queries live Jira data, fuzzy-matches assignee display names to employees,
estimates allocation percentages from issue counts, and updates
employees.json and allocations.json accordingly.

Fuzzy matching rule:
  first 5 chars of first name (case-insensitive) AND last name substring
  must both appear in the Jira assignee displayName.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Optional

from mcp_servers.pm_server import jira_client
from talent.models import AllocationCreate, Employee, SyncResult
from talent.repository import AllocationRepository, EmployeeRepository

log = logging.getLogger(__name__)

_TODAY = date.today()


def _fuzzy_match(display_name: str, employee_name: str) -> bool:
    """Return True if the Jira display name likely maps to the given employee.

    Strategy: first 5 chars of first name + full last name must both appear
    in the display name (case-insensitive substring match).
    """
    dn_lower = display_name.lower()
    parts = employee_name.lower().split()
    if not parts:
        return False
    if len(parts) == 1:
        return parts[0] in dn_lower
    first_prefix = parts[0][:5]
    last_name = parts[-1]
    return first_prefix in dn_lower and last_name in dn_lower


def _estimate_allocation_pct(issue_count: int) -> int:
    """Estimate allocation percentage from issue count in a project."""
    if issue_count >= 5:
        return 100
    if issue_count >= 3:
        return 80
    if issue_count >= 1:
        return 60
    return 0


def _estimate_rolloff_date(issues: list[dict[str, Any]], project_id: str) -> str:
    """Estimate roll-off date for an assignee on a project.

    Priority:
    1. Max due_date of their open issues
    2. project end_date from our projects.json
    3. today + 90 days as fallback
    """
    from talent.repository import ProjectRepository
    proj_repo = ProjectRepository()

    due_dates: list[date] = []
    for issue in issues:
        dd = issue.get("due_date")
        if dd:
            try:
                d = date.fromisoformat(dd)
                if d >= _TODAY:
                    due_dates.append(d)
            except ValueError:
                pass

    if due_dates:
        return max(due_dates).isoformat()

    proj = proj_repo.get(project_id)
    if proj and proj.end_date:
        try:
            end = date.fromisoformat(proj.end_date)
            if end >= _TODAY:
                return proj.end_date
        except ValueError:
            pass

    fallback = _TODAY + timedelta(days=90)
    return fallback.isoformat()


def _collect_jira_assignments(
    project_key: str,
) -> dict[str, dict[str, Any]]:
    """Return {display_name: {issue_count, issues, project_key}} for all assignees in a project."""
    issues = jira_client.search_issues(project_key=project_key, max_results=100)
    assignee_data: dict[str, dict[str, Any]] = {}
    for issue in issues:
        assignee = issue.get("assignee")
        if not assignee or assignee == "Unassigned":
            continue
        if assignee not in assignee_data:
            assignee_data[assignee] = {
                "display_name": assignee,
                "project_key": project_key,
                "issues": [],
            }
        assignee_data[assignee]["issues"].append(issue)
    return assignee_data


def run_sync(
    employee_repo: EmployeeRepository,
    allocation_repo: AllocationRepository,
) -> SyncResult:
    """Main entry point: sync Jira assignments → employees + allocations."""
    errors: list[str] = []
    matched_assignees: list[str] = []
    unmatched_assignees: list[str] = []
    synced_count = 0
    alloc_count = 0

    # Step 1: Get all Jira projects
    try:
        projects_response = jira_client.list_projects()
    except Exception as exc:
        log.error("[JIRA_SYNC] list_projects failed: %s", exc)
        return SyncResult(
            synced_employees=0,
            updated_allocations=0,
            jira_projects_found=0,
            matched_assignees=[],
            unmatched_assignees=[],
            errors=[f"Jira list_projects failed: {exc}"],
        )

    if not projects_response.get("configured"):
        return SyncResult(
            synced_employees=0,
            updated_allocations=0,
            jira_projects_found=0,
            matched_assignees=[],
            unmatched_assignees=[],
            errors=["Jira is not configured (missing JIRA_EMAIL or JIRA_API_TOKEN)."],
        )

    jira_projects = projects_response.get("projects", [])
    log.info("[JIRA_SYNC] found %d Jira projects", len(jira_projects))

    # Step 2: Collect all assignees across all projects
    # {display_name -> {project_key, issues}}
    all_assignments: dict[str, dict[str, Any]] = {}
    for proj in jira_projects:
        pk = proj.get("key")
        if not pk:
            continue
        try:
            assignments = _collect_jira_assignments(pk)
            for dn, data in assignments.items():
                if dn not in all_assignments:
                    all_assignments[dn] = data
                else:
                    # Merge issues if same person on multiple projects — keep first project
                    all_assignments[dn]["issues"].extend(data["issues"])
        except Exception as exc:
            log.warning("[JIRA_SYNC] failed to collect issues for %s: %s", pk, exc)
            errors.append(f"Could not fetch issues for project {pk}: {exc}")

    # Step 3: Load employees and fuzzy-match
    employees = employee_repo.list_all()

    for display_name, assignment_data in all_assignments.items():
        project_key = assignment_data["project_key"]
        issues = assignment_data["issues"]

        matched_emp: Optional[Employee] = None
        for emp in employees:
            if _fuzzy_match(display_name, emp.name):
                matched_emp = emp
                break

        if matched_emp is None:
            log.info("[JIRA_SYNC] no match for Jira assignee: %r", display_name)
            unmatched_assignees.append(display_name)
            continue

        matched_assignees.append(f"{display_name} → {matched_emp.name} ({matched_emp.id})")
        log.info("[JIRA_SYNC] matched %r → %s", display_name, matched_emp.name)

        # Step 4: Estimate allocation and roll-off
        allocation_pct = _estimate_allocation_pct(len(issues))
        rolloff_date = _estimate_rolloff_date(issues, project_key)
        availability_pct = max(0, 100 - allocation_pct)
        weekly_capacity = int(40 * availability_pct / 100)

        # Step 5: Update employee record
        update = {
            "status": "allocated" if allocation_pct > 0 else "available",
            "current_project": project_key if allocation_pct > 0 else None,
            "allocation_pct": allocation_pct,
            "availability_pct": availability_pct,
            "availability_date": rolloff_date,
            "weekly_capacity": weekly_capacity,
            "capacity_hours": weekly_capacity,
            "utilization_pct": allocation_pct,
            "bench": False,
            "billable": True,
        }
        from talent.repository import _read, _write
        records = _read("employees.json")
        for i, r in enumerate(records):
            if r.get("id") == matched_emp.id:
                records[i] = {**r, **update}
                break
        _write("employees.json", records)
        synced_count += 1

        # Step 6: Upsert allocation record
        if allocation_pct > 0:
            try:
                alloc_payload = AllocationCreate(
                    employee_id=matched_emp.id,
                    project_id=project_key,
                    allocation_pct=allocation_pct,
                    role=matched_emp.designation,
                    start_date=_TODAY.isoformat(),
                    end_date=rolloff_date,
                    billable=True,
                )
                allocation_repo.upsert_for_employee_project(alloc_payload)
                alloc_count += 1
            except Exception as exc:
                log.warning("[JIRA_SYNC] allocation upsert failed for %s: %s", matched_emp.id, exc)
                errors.append(f"Allocation upsert failed for {matched_emp.id}: {exc}")

    log.info(
        "[JIRA_SYNC] done — synced=%d allocs=%d matched=%d unmatched=%d",
        synced_count, alloc_count, len(matched_assignees), len(unmatched_assignees),
    )

    return SyncResult(
        synced_employees=synced_count,
        updated_allocations=alloc_count,
        jira_projects_found=len(jira_projects),
        matched_assignees=matched_assignees,
        unmatched_assignees=unmatched_assignees,
        errors=errors,
    )
