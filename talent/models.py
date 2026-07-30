"""
Pydantic models for the Talent Management module.

These models mirror the JSON schemas in data/ and are used for
validation in the repository, service, and route layers.
"""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class Employee(BaseModel):
    id: str
    name: str
    email: str
    designation: str
    department: str
    location: str
    country: str
    manager: Optional[str] = None
    primary_skills: list[str] = Field(default_factory=list)
    secondary_skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    experience_years: int
    join_date: str
    status: str  # available / allocated / bench / on_leave
    bench: bool = False
    billable: bool = True
    cost_center: str
    career_level: str  # L3 / L4 / L5
    preferred_projects: list[str] = Field(default_factory=list)
    current_project: Optional[str] = None
    allocation_pct: int = 0
    availability_pct: int = 100
    availability_date: str
    weekly_capacity: int = 40
    capacity_hours: int = 1600
    utilization_pct: int = 0

    @property
    def all_skills(self) -> list[str]:
        return list(dict.fromkeys(self.primary_skills + self.secondary_skills))


class EmployeeCreate(BaseModel):
    name: str
    email: str
    designation: str
    department: str
    location: str
    country: str
    manager: Optional[str] = None
    primary_skills: list[str] = Field(default_factory=list)
    secondary_skills: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    experience_years: int
    join_date: str
    status: str = "available"
    bench: bool = False
    billable: bool = True
    cost_center: str = "CC001"
    career_level: str = "L3"
    preferred_projects: list[str] = Field(default_factory=list)
    current_project: Optional[str] = None
    allocation_pct: int = 0
    availability_pct: int = 100
    availability_date: str
    weekly_capacity: int = 40
    capacity_hours: int = 1600
    utilization_pct: int = 0


class EmployeeUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    designation: Optional[str] = None
    department: Optional[str] = None
    location: Optional[str] = None
    country: Optional[str] = None
    manager: Optional[str] = None
    primary_skills: Optional[list[str]] = None
    secondary_skills: Optional[list[str]] = None
    certifications: Optional[list[str]] = None
    experience_years: Optional[int] = None
    join_date: Optional[str] = None
    status: Optional[str] = None
    bench: Optional[bool] = None
    billable: Optional[bool] = None
    cost_center: Optional[str] = None
    career_level: Optional[str] = None
    preferred_projects: Optional[list[str]] = None
    current_project: Optional[str] = None
    allocation_pct: Optional[int] = None
    availability_pct: Optional[int] = None
    availability_date: Optional[str] = None
    weekly_capacity: Optional[int] = None
    capacity_hours: Optional[int] = None
    utilization_pct: Optional[int] = None


class Project(BaseModel):
    id: str
    name: str
    client: str
    status: str  # active / planning / completed / on_hold
    delivery_manager: Optional[str] = None
    description: str = ""
    required_skills: list[str] = Field(default_factory=list)
    team_size: int = 0
    allocated_employees: list[str] = Field(default_factory=list)
    start_date: str
    end_date: str
    priority: str = "Medium"
    budget: Optional[float] = None
    health: str = "HEALTHY"
    tags: list[str] = Field(default_factory=list)


class Allocation(BaseModel):
    id: str
    employee_id: str
    project_id: str
    allocation_pct: int
    role: str
    start_date: str
    end_date: str
    billable: bool = True


class AllocationCreate(BaseModel):
    employee_id: str
    project_id: str
    allocation_pct: int
    role: str
    start_date: str
    end_date: str
    billable: bool = True


class Skill(BaseModel):
    name: str
    category: str  # Frontend / Backend / Data / Cloud / AI-ML / Security / Quality / Management
    description: str
    demand_level: str  # high / medium / low
    avg_experience_needed: int


class SkillMatrixEntry(BaseModel):
    employee_id: str
    employee_name: str
    department: str
    status: str
    career_level: str
    skill_levels: dict[str, str] = Field(default_factory=dict)  # skill_name -> "primary" | "secondary" | ""


class SkillMatrix(BaseModel):
    skills_by_category: dict[str, list[str]]
    employees: list[SkillMatrixEntry]


class MatchCandidate(BaseModel):
    employee: Employee
    match_score: float
    skill_match_pct: float
    availability_score: float
    experience_score: float
    certification_score: float
    location_score: float
    missing_skills: list[str]
    matching_skills: list[str]
    reasoning: str


class BenchEmployee(BaseModel):
    employee: Employee
    bench_duration_days: int
    suggested_projects: list[str]
    recommended_upskilling: list[str]


class ForecastBucket(BaseModel):
    period: str  # "30_days" | "60_days" | "90_days"
    employees_rolling_off: list[dict[str, Any]]
    projects_ending: list[dict[str, Any]]
    capacity_added_hours: int
    headcount_freed: int


class TalentInsights(BaseModel):
    top_skills: list[dict[str, Any]]
    skill_gaps: list[dict[str, Any]]
    critical_risks: list[dict[str, Any]]
    rare_skill_holders: list[dict[str, Any]]
    recommended_hiring: list[str]


class SimulationResult(BaseModel):
    feasible: bool
    employee_id: str
    project_id: str
    requested_allocation_pct: int
    current_allocation_pct: int
    new_total_allocation_pct: int
    remaining_capacity_pct: int
    conflicts: list[str]
    impact_summary: str


class DashboardStats(BaseModel):
    total_employees: int
    available_employees: int
    allocated_employees: int
    bench_count: int
    on_leave_count: int
    avg_utilization: float
    capacity_remaining_hours: int
    free_in_30_days: int
    rolling_off_soon: list[dict[str, Any]]
    bench_employees: list[dict[str, Any]]
    skill_coverage_pct: float
    overallocated_count: int
    top_skills: list[dict[str, Any]]
    status_breakdown: dict[str, int]
    department_breakdown: dict[str, int]
    level_breakdown: dict[str, int]


class SyncResult(BaseModel):
    synced_employees: int
    updated_allocations: int
    jira_projects_found: int
    matched_assignees: list[str]
    unmatched_assignees: list[str]
    errors: list[str]
