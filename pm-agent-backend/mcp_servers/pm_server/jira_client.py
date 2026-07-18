"""
Jira connector — thin wrapper over the Jira REST API.

Called by the MCP server's tools. Uses httpx (works identically on Win/Mac).
If Jira env vars aren't set yet, calls return an empty/marker result so the
rest of the system still runs during local development.
"""
from __future__ import annotations
import base64
import os
from datetime import date
from typing import Any

import httpx


def _auth_header() -> dict[str, str] | None:
    email = os.getenv("JIRA_EMAIL", "")
    token = os.getenv("JIRA_API_TOKEN", "")
    if not email or not token or "your-jira-token" in token:
        return None  # not configured yet
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}


def _base_url() -> str:
    return os.getenv("JIRA_BASE_URL", "").rstrip("/")


def _search(jql: str, max_results: int = 50) -> list[dict[str, Any]]:
    headers = _auth_header()
    if headers is None or not _base_url():
        return []  # Jira not configured — return empty so system still runs
    url = f"{_base_url()}/rest/api/3/search"
    params = {
        "jql": jql,
        "maxResults": str(max_results),
        "fields": "summary,status,duedate,priority,assignee",
    }
    with httpx.Client(timeout=15) as client:
        resp = client.get(url, headers=headers, params=params)
        resp.raise_for_status()
        return resp.json().get("issues", [])


def _simplify(issue: dict[str, Any]) -> dict[str, Any]:
    f = issue.get("fields", {})
    assignee = f.get("assignee")
    return {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "status": (f.get("status") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "due_date": f.get("duedate"),
        "assignee": assignee.get("displayName") if assignee else "Unassigned",
    }


def get_project_status(project_key: str) -> dict[str, Any]:
    """Milestone / status summary for a project."""
    issues = [_simplify(i) for i in _search(f"project={project_key} ORDER BY duedate ASC")]
    if not issues:
        return {
            "project": project_key,
            "configured": _auth_header() is not None,
            "total": 0,
            "issues": [],
            "note": "No Jira data (project empty or Jira not configured yet).",
        }
    today = date.today().isoformat()
    done = [i for i in issues if (i["status"] or "").lower() == "done"]
    overdue = [
        i for i in issues
        if i["due_date"] and i["due_date"] < today and (i["status"] or "").lower() != "done"
    ]
    return {
        "project": project_key,
        "configured": True,
        "total": len(issues),
        "done": len(done),
        "in_progress": len([i for i in issues if (i["status"] or "").lower() == "in progress"]),
        "todo": len([i for i in issues if (i["status"] or "").lower() in ("to do", "todo")]),
        "overdue": overdue,
        "issues": issues,
    }


def get_risks(project_key: str) -> dict[str, Any]:
    """High/Highest priority unresolved issues = risks."""
    jql = (
        f"project={project_key} AND priority in (High, Highest) "
        f"AND statusCategory != Done ORDER BY priority DESC"
    )
    issues = [_simplify(i) for i in _search(jql)]
    today = date.today().isoformat()
    for i in issues:
        i["overdue"] = bool(i["due_date"] and i["due_date"] < today)
    severity = "HIGH" if len(issues) > 2 else "MEDIUM" if issues else "LOW"
    return {
        "project": project_key,
        "configured": _auth_header() is not None,
        "risk_count": len(issues),
        "severity": severity,
        "risks": issues,
    }
