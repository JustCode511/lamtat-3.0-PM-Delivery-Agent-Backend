"""
FastAPI router for the Talent Management module.

All endpoints are prefixed with /talent and require a bearer token in the
Authorization header (same JWT tokens issued by /auth/login).

Auth is handled inline via verify_api_key which checks for the header;
the caller is responsible for wiring in full JWT validation at the app level.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel

from talent.models import (
    AllocationCreate,
    Employee,
    EmployeeCreate,
    EmployeeUpdate,
    SyncResult,
)
from talent.report_generator import (
    generate_talent_docx_bytes,
    generate_talent_pptx_bytes,
    generate_talent_xlsx_bytes,
)
from talent.services import TalentService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/talent", tags=["talent"])

# ---------------------------------------------------------------------------
# Auth dependency — bearer token check mirroring the main app pattern.
# Full JWT signature verification is done by the shared decode_jwt; here we
# just confirm the header is present and extract the raw token so callers
# can pass it to shared.auth.decode_jwt if needed.
# ---------------------------------------------------------------------------

async def verify_api_key(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. Expected: Bearer <token>.",
        )
    return authorization.removeprefix("Bearer ").strip()


# Single shared service instance (stateless; thread-safe JSON repos)
_svc = TalentService()


# ---------------------------------------------------------------------------
# Request / Response helpers
# ---------------------------------------------------------------------------

class SimulationRequest(BaseModel):
    employee_id: str
    project_id: str
    allocation_pct: int


class ChatRequest(BaseModel):
    session_id: str
    message: str


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/dashboard", dependencies=[Depends(verify_api_key)])
async def talent_dashboard() -> dict[str, Any]:
    """Aggregate talent stats: utilisation, bench, availability, top skills."""
    result = await asyncio.to_thread(_svc.get_dashboard)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

@router.get("/employees", dependencies=[Depends(verify_api_key)])
async def list_employees(
    status: Optional[str] = Query(default=None, description="Filter by status: available/allocated/bench/on_leave"),
    department: Optional[str] = Query(default=None, description="Filter by department"),
    skill: Optional[str] = Query(default=None, description="Filter by skill name"),
    search: Optional[str] = Query(default=None, description="Free-text search across name, role, location"),
) -> dict[str, Any]:
    """List employees with optional filters."""
    employees = await asyncio.to_thread(_svc.employee_repo.list_all)

    if status:
        employees = [e for e in employees if e.status == status]
    if department:
        employees = [e for e in employees if e.department.lower() == department.lower()]
    if skill:
        skill_lower = skill.lower()
        employees = [
            e for e in employees
            if skill_lower in [s.lower() for s in e.primary_skills + e.secondary_skills]
        ]
    if search:
        q = search.lower()
        employees = [
            e for e in employees
            if q in e.name.lower()
            or q in e.designation.lower()
            or q in e.location.lower()
            or any(q in s.lower() for s in e.primary_skills)
        ]

    return {"count": len(employees), "employees": [e.model_dump() for e in employees]}


@router.get("/employees/{employee_id}", dependencies=[Depends(verify_api_key)])
async def get_employee(employee_id: str) -> dict[str, Any]:
    """Get a single employee with their current allocations."""
    emp = await asyncio.to_thread(_svc.employee_repo.get, employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found.")
    allocations = await asyncio.to_thread(_svc.allocation_repo.list_by_employee, employee_id)
    return {
        "employee": emp.model_dump(),
        "allocations": [a.model_dump() for a in allocations],
    }


@router.post("/employees", dependencies=[Depends(verify_api_key)], status_code=201)
async def create_employee(payload: EmployeeCreate) -> dict[str, Any]:
    """Create a new employee record."""
    emp = await asyncio.to_thread(_svc.employee_repo.create, payload)
    return {"employee": emp.model_dump()}


@router.put("/employees/{employee_id}", dependencies=[Depends(verify_api_key)])
async def update_employee(employee_id: str, payload: EmployeeUpdate) -> dict[str, Any]:
    """Update an existing employee record (partial update)."""
    emp = await asyncio.to_thread(_svc.employee_repo.update, employee_id, payload)
    if emp is None:
        raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found.")
    return {"employee": emp.model_dump()}


@router.delete("/employees/{employee_id}", dependencies=[Depends(verify_api_key)])
async def delete_employee(employee_id: str) -> dict[str, Any]:
    """Delete an employee record."""
    deleted = await asyncio.to_thread(_svc.employee_repo.delete, employee_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found.")
    return {"deleted": True, "employee_id": employee_id}


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

@router.get("/projects", dependencies=[Depends(verify_api_key)])
async def list_projects(
    status: Optional[str] = Query(default=None, description="Filter by project status"),
) -> dict[str, Any]:
    """List all talent-tracked projects."""
    projects = await asyncio.to_thread(_svc.project_repo.list_all)
    if status:
        projects = [p for p in projects if p.status == status]
    return {"count": len(projects), "projects": [p.model_dump() for p in projects]}


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@router.get("/skills", dependencies=[Depends(verify_api_key)])
async def list_skills(
    category: Optional[str] = Query(default=None, description="Filter by category"),
    demand: Optional[str] = Query(default=None, description="Filter by demand_level: high/medium/low"),
) -> dict[str, Any]:
    """List all defined skills."""
    skills = await asyncio.to_thread(_svc.skill_repo.list_all)
    if category:
        skills = [s for s in skills if s.category.lower() == category.lower()]
    if demand:
        skills = [s for s in skills if s.demand_level.lower() == demand.lower()]
    return {"count": len(skills), "skills": [s.model_dump() for s in skills]}


# ---------------------------------------------------------------------------
# Skill Matrix
# ---------------------------------------------------------------------------

@router.get("/matrix", dependencies=[Depends(verify_api_key)])
async def skill_matrix(
    department: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """Return the skills×employees matrix in frontend-ready flat format."""
    matrix = await asyncio.to_thread(_svc.get_skill_matrix, department, status)

    # Flatten skills_by_category into two parallel structures the FE needs:
    #   skills: ["React","Python",...] (sorted flat list)
    #   skill_categories: {"React":"Frontend","Python":"Backend",...}
    skill_categories: dict[str, str] = {}
    all_skills: list[str] = []
    for cat, cat_skills in matrix.skills_by_category.items():
        for s in cat_skills:
            skill_categories[s] = cat
            all_skills.append(s)
    all_skills_sorted = sorted(all_skills)

    employees_out = []
    departments_set: set[str] = set()
    for entry in matrix.employees:
        # skill_levels uses "primary"/"secondary"/"" — map "" to None for FE
        skills_map = {s: (v if v else None) for s, v in entry.skill_levels.items()}
        # Also find the employee's extra fields from the repo
        emp = _svc.employee_repo.get(entry.employee_id)
        employees_out.append({
            "id": entry.employee_id,
            "name": entry.employee_name,
            "designation": emp.designation if emp else "",
            "department": entry.department,
            "location": emp.location if emp else "",
            "status": entry.status,
            "availability_pct": emp.availability_pct if emp else 0,
            "current_project": emp.current_project if emp else None,
            "career_level": entry.career_level,
            "skills": skills_map,
            "primary_count": sum(1 for v in skills_map.values() if v == "primary"),
            "total_skills": sum(1 for v in skills_map.values() if v),
        })
        departments_set.add(entry.department)

    # Sort by most skills first
    employees_out.sort(key=lambda e: e["total_skills"], reverse=True)

    return {
        "skills": all_skills_sorted,
        "skill_categories": skill_categories,
        "employees": employees_out,
        "total_employees": len(employees_out),
        "total_skills": len(all_skills_sorted),
        "departments": sorted(departments_set),
    }


# ---------------------------------------------------------------------------
# Matching / Recommendations
# ---------------------------------------------------------------------------

@router.get("/matching", dependencies=[Depends(verify_api_key)])
async def get_matching(
    skills: str = Query(..., description="Comma-separated list of required skills"),
    location: Optional[str] = Query(default=None, description="Preferred location or country"),
    min_experience: int = Query(default=0, ge=0, description="Minimum years of experience"),
    limit: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """Rank employees by fit for the given skill requirements."""
    required = [s.strip() for s in skills.split(",") if s.strip()]
    if not required:
        raise HTTPException(status_code=400, detail="At least one skill is required.")
    candidates = await asyncio.to_thread(_svc.get_matching, required, location, min_experience)
    result = candidates[:limit]
    return {
        "required_skills": required,
        "count": len(result),
        "candidates": [
            {
                **c.model_dump(exclude={"employee"}),
                "employee": c.employee.model_dump(),
            }
            for c in result
        ],
    }


# ---------------------------------------------------------------------------
# Bench
# ---------------------------------------------------------------------------

@router.get("/bench", dependencies=[Depends(verify_api_key)])
async def get_bench() -> dict[str, Any]:
    """List bench employees with duration, suggested projects, and upskilling recommendations."""
    bench = await asyncio.to_thread(_svc.get_bench)
    return {
        "count": len(bench),
        "bench_employees": [
            {
                "employee": b.employee.model_dump(),
                "bench_duration_days": b.bench_duration_days,
                "suggested_projects": b.suggested_projects,
                "recommended_upskilling": b.recommended_upskilling,
            }
            for b in bench
        ],
    }


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

@router.get("/forecast", dependencies=[Depends(verify_api_key)])
async def get_forecast() -> dict[str, Any]:
    """Return 30/60/90-day capacity and availability forecast."""
    buckets = await asyncio.to_thread(_svc.get_forecast)
    return {"forecast": [b.model_dump() for b in buckets]}


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------

@router.get("/insights", dependencies=[Depends(verify_api_key)])
async def get_insights() -> dict[str, Any]:
    """Return talent insights: skill gaps, critical risks, hiring recommendations."""
    insights = await asyncio.to_thread(_svc.get_insights)
    return insights.model_dump()


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@router.post("/simulation", dependencies=[Depends(verify_api_key)])
async def simulate_allocation(req: SimulationRequest) -> dict[str, Any]:
    """Preview what would happen if an employee were allocated to a project."""
    result = await asyncio.to_thread(
        _svc.simulate_allocation,
        req.employee_id,
        req.project_id,
        req.allocation_pct,
    )
    return result.model_dump()


# ---------------------------------------------------------------------------
# Jira sync
# ---------------------------------------------------------------------------

@router.post("/sync", dependencies=[Depends(verify_api_key)])
async def sync_from_jira() -> dict[str, Any]:
    """Sync talent allocations from live Jira data (fuzzy-matches assignees to employees)."""
    result: SyncResult = await asyncio.to_thread(_svc.sync_from_jira)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Allocations (list/create/delete)
# ---------------------------------------------------------------------------

@router.get("/allocations", dependencies=[Depends(verify_api_key)])
async def list_allocations(
    employee_id: Optional[str] = Query(default=None),
    project_id: Optional[str] = Query(default=None),
) -> dict[str, Any]:
    """List allocations, optionally filtered by employee or project."""
    if employee_id:
        allocs = await asyncio.to_thread(_svc.allocation_repo.list_by_employee, employee_id)
    elif project_id:
        allocs = await asyncio.to_thread(_svc.allocation_repo.list_by_project, project_id)
    else:
        allocs = await asyncio.to_thread(_svc.allocation_repo.list_all)
    return {"count": len(allocs), "allocations": [a.model_dump() for a in allocs]}


@router.post("/allocations", dependencies=[Depends(verify_api_key)], status_code=201)
async def create_allocation(payload: AllocationCreate) -> dict[str, Any]:
    """Create a new allocation record."""
    # Validate employee and project exist
    emp = await asyncio.to_thread(_svc.employee_repo.get, payload.employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail=f"Employee {payload.employee_id} not found.")
    proj = await asyncio.to_thread(_svc.project_repo.get, payload.project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail=f"Project {payload.project_id} not found.")
    alloc = await asyncio.to_thread(_svc.allocation_repo.create, payload)
    return {"allocation": alloc.model_dump()}


@router.delete("/allocations/{allocation_id}", dependencies=[Depends(verify_api_key)])
async def delete_allocation(allocation_id: str) -> dict[str, Any]:
    """Delete an allocation record."""
    deleted = await asyncio.to_thread(_svc.allocation_repo.delete, allocation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Allocation {allocation_id} not found.")
    return {"deleted": True, "allocation_id": allocation_id}


# ---------------------------------------------------------------------------
# Reports — download PPTX / XLSX / DOCX of the current talent state.
# All three read from the same shared context (see talent/report_generator.py).
# ---------------------------------------------------------------------------

from datetime import date as _date  # local alias — avoids clashing with fastapi date


def _load_report_inputs():
    """Fetch employees, projects, allocations in one shot."""
    return (
        _svc.employee_repo.list_all(),
        _svc.project_repo.list_all(),
        _svc.allocation_repo.list_all(),
    )


@router.get("/export/ppt", dependencies=[Depends(verify_api_key)])
async def export_talent_ppt() -> Response:
    """Generate and stream a .pptx talent report. No file is saved to disk."""
    employees, projects, allocations = await asyncio.to_thread(_load_report_inputs)
    pptx_bytes = await asyncio.to_thread(
        generate_talent_pptx_bytes, employees, projects, allocations
    )
    filename = f"Talent_Report_{_date.today().isoformat()}.pptx"
    return Response(
        content=pptx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/xlsx", dependencies=[Depends(verify_api_key)])
async def export_talent_xlsx() -> Response:
    """Generate and stream an .xlsx talent report."""
    employees, projects, allocations = await asyncio.to_thread(_load_report_inputs)
    xlsx_bytes = await asyncio.to_thread(
        generate_talent_xlsx_bytes, employees, projects, allocations
    )
    filename = f"Talent_Report_{_date.today().isoformat()}.xlsx"
    return Response(
        content=xlsx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/docx", dependencies=[Depends(verify_api_key)])
async def export_talent_docx() -> Response:
    """Generate and stream a .docx talent report."""
    employees, projects, allocations = await asyncio.to_thread(_load_report_inputs)
    docx_bytes = await asyncio.to_thread(
        generate_talent_docx_bytes, employees, projects, allocations
    )
    filename = f"Talent_Report_{_date.today().isoformat()}.docx"
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
