"""
JSON-file-backed repositories for the Talent Management module.

All repositories resolve data paths relative to this file's parent directory:
  Path(__file__).parent.parent / "data"

Thread safety is ensured via per-file RLock instances so concurrent FastAPI
requests cannot corrupt the JSON files during write operations.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from talent.models import Allocation, AllocationCreate, Employee, EmployeeCreate, EmployeeUpdate, Project, Skill

_DATA_DIR = Path(__file__).parent.parent / "data"

# One lock per file — reentrant so the same thread can re-acquire without deadlock.
_LOCKS: dict[str, threading.RLock] = {
    "employees": threading.RLock(),
    "projects": threading.RLock(),
    "allocations": threading.RLock(),
    "skills": threading.RLock(),
}


def _read(filename: str) -> list[dict[str, Any]]:
    path = _DATA_DIR / filename
    if not path.exists():
        return []
    with _LOCKS[filename.split(".")[0]]:
        return json.loads(path.read_text(encoding="utf-8"))


def _write(filename: str, data: list[dict[str, Any]]) -> None:
    path = _DATA_DIR / filename
    key = filename.split(".")[0]
    with _LOCKS[key]:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# EmployeeRepository
# ---------------------------------------------------------------------------

class EmployeeRepository:
    """CRUD access to data/employees.json."""

    _FILE = "employees.json"

    def list_all(self) -> list[Employee]:
        return [Employee(**e) for e in _read(self._FILE)]

    def get(self, employee_id: str) -> Optional[Employee]:
        for e in _read(self._FILE):
            if e.get("id") == employee_id:
                return Employee(**e)
        return None

    def create(self, payload: EmployeeCreate) -> Employee:
        records = _read(self._FILE)
        # Generate next EMP id
        existing_ids = [r.get("id", "") for r in records]
        nums = []
        for eid in existing_ids:
            if eid.startswith("EMP"):
                try:
                    nums.append(int(eid[3:]))
                except ValueError:
                    pass
        next_num = max(nums, default=0) + 1
        new_id = f"EMP{next_num:03d}"
        new_record = {"id": new_id, **payload.model_dump()}
        records.append(new_record)
        _write(self._FILE, records)
        return Employee(**new_record)

    def update(self, employee_id: str, payload: EmployeeUpdate) -> Optional[Employee]:
        records = _read(self._FILE)
        updated: Optional[dict] = None
        for i, r in enumerate(records):
            if r.get("id") == employee_id:
                patch = {k: v for k, v in payload.model_dump().items() if v is not None}
                records[i] = {**r, **patch}
                updated = records[i]
                break
        if updated is None:
            return None
        _write(self._FILE, records)
        return Employee(**updated)

    def delete(self, employee_id: str) -> bool:
        records = _read(self._FILE)
        filtered = [r for r in records if r.get("id") != employee_id]
        if len(filtered) == len(records):
            return False
        _write(self._FILE, filtered)
        return True

    def list_by_status(self, status: str) -> list[Employee]:
        return [Employee(**e) for e in _read(self._FILE) if e.get("status") == status]

    def list_available(self) -> list[Employee]:
        return self.list_by_status("available")

    def list_bench(self) -> list[Employee]:
        return [
            Employee(**e)
            for e in _read(self._FILE)
            if e.get("status") == "bench" or e.get("bench") is True
        ]

    def list_by_department(self, department: str) -> list[Employee]:
        return [Employee(**e) for e in _read(self._FILE) if e.get("department") == department]

    def list_by_skill(self, skill: str) -> list[Employee]:
        skill_lower = skill.lower()
        result = []
        for e in _read(self._FILE):
            primary = [s.lower() for s in e.get("primary_skills", [])]
            secondary = [s.lower() for s in e.get("secondary_skills", [])]
            if skill_lower in primary or skill_lower in secondary:
                result.append(Employee(**e))
        return result

    def search(self, query: str) -> list[Employee]:
        q = query.lower()
        result = []
        for e in _read(self._FILE):
            haystack = " ".join([
                e.get("name", ""), e.get("designation", ""), e.get("location", ""),
                e.get("department", ""), e.get("status", ""),
                " ".join(e.get("primary_skills", [])),
            ]).lower()
            if q in haystack:
                result.append(Employee(**e))
        return result


# ---------------------------------------------------------------------------
# ProjectRepository
# ---------------------------------------------------------------------------

class ProjectRepository:
    """CRUD access to data/projects.json."""

    _FILE = "projects.json"

    def list_all(self) -> list[Project]:
        return [Project(**p) for p in _read(self._FILE)]

    def get(self, project_id: str) -> Optional[Project]:
        for p in _read(self._FILE):
            if p.get("id") == project_id:
                return Project(**p)
        return None

    def create(self, project: Project) -> Project:
        records = _read(self._FILE)
        records.append(project.model_dump())
        _write(self._FILE, records)
        return project

    def update(self, project_id: str, updates: dict[str, Any]) -> Optional[Project]:
        records = _read(self._FILE)
        updated: Optional[dict] = None
        for i, p in enumerate(records):
            if p.get("id") == project_id:
                records[i] = {**p, **updates}
                updated = records[i]
                break
        if updated is None:
            return None
        _write(self._FILE, records)
        return Project(**updated)

    def list_active(self) -> list[Project]:
        return [Project(**p) for p in _read(self._FILE) if p.get("status") == "active"]


# ---------------------------------------------------------------------------
# AllocationRepository
# ---------------------------------------------------------------------------

class AllocationRepository:
    """CRUD access to data/allocations.json."""

    _FILE = "allocations.json"

    def list_all(self) -> list[Allocation]:
        return [Allocation(**a) for a in _read(self._FILE)]

    def get(self, allocation_id: str) -> Optional[Allocation]:
        for a in _read(self._FILE):
            if a.get("id") == allocation_id:
                return Allocation(**a)
        return None

    def create(self, payload: AllocationCreate) -> Allocation:
        records = _read(self._FILE)
        existing_ids = [r.get("id", "") for r in records]
        nums = []
        for aid in existing_ids:
            if aid.startswith("ALLOC"):
                try:
                    nums.append(int(aid[5:]))
                except ValueError:
                    pass
        next_num = max(nums, default=0) + 1
        new_id = f"ALLOC{next_num:03d}"
        new_record = {"id": new_id, **payload.model_dump()}
        records.append(new_record)
        _write(self._FILE, records)
        return Allocation(**new_record)

    def delete(self, allocation_id: str) -> bool:
        records = _read(self._FILE)
        filtered = [r for r in records if r.get("id") != allocation_id]
        if len(filtered) == len(records):
            return False
        _write(self._FILE, filtered)
        return True

    def list_by_employee(self, employee_id: str) -> list[Allocation]:
        return [Allocation(**a) for a in _read(self._FILE) if a.get("employee_id") == employee_id]

    def list_by_project(self, project_id: str) -> list[Allocation]:
        return [Allocation(**a) for a in _read(self._FILE) if a.get("project_id") == project_id]

    def upsert_for_employee_project(self, payload: AllocationCreate) -> Allocation:
        """Create or replace an allocation for a given employee+project pair."""
        records = _read(self._FILE)
        existing_idx: Optional[int] = None
        for i, r in enumerate(records):
            if r.get("employee_id") == payload.employee_id and r.get("project_id") == payload.project_id:
                existing_idx = i
                break
        if existing_idx is not None:
            existing_id = records[existing_idx]["id"]
            records[existing_idx] = {"id": existing_id, **payload.model_dump()}
            _write(self._FILE, records)
            return Allocation(**records[existing_idx])
        return self.create(payload)


# ---------------------------------------------------------------------------
# SkillRepository
# ---------------------------------------------------------------------------

class SkillRepository:
    """Read-only access to data/skills.json."""

    _FILE = "skills.json"

    def list_all(self) -> list[Skill]:
        return [Skill(**s) for s in _read(self._FILE)]

    def get(self, name: str) -> Optional[Skill]:
        for s in _read(self._FILE):
            if s.get("name", "").lower() == name.lower():
                return Skill(**s)
        return None

    def list_by_category(self, category: str) -> list[Skill]:
        return [Skill(**s) for s in _read(self._FILE) if s.get("category") == category]

    def list_high_demand(self) -> list[Skill]:
        return [Skill(**s) for s in _read(self._FILE) if s.get("demand_level") == "high"]
