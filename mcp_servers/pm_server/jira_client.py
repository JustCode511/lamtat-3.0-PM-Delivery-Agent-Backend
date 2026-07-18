"""
Jira connector — thin wrapper over the Jira REST API.
Reads live ticket data for the MCP server's tools. Cross-platform (httpx).
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
        return None
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}


def _base_url() -> str:
    return os.getenv("JIRA_BASE_URL", "").rstrip("/")


def get_project_name(project_key: str) -> str:
    """Fetch the human-readable project name (e.g. 'Enterprise Portal Migration')."""
    headers = _auth_header()
    if headers is None or not _base_url():
        return project_key
    url = f"{_base_url()}/rest/api/3/project/{project_key}"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json().get("name", project_key)
    except Exception:
        return project_key


def list_projects() -> dict[str, Any]:
    """List all projects the user can access, with key and name."""
    headers = _auth_header()
    if headers is None or not _base_url():
        return {"configured": False, "projects": [], "note": "Jira not configured."}
    url = f"{_base_url()}/rest/api/3/project/search"
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, headers=headers, params={"maxResults": "50"})
            resp.raise_for_status()
            values = resp.json().get("values", [])
    except Exception as e:
        return {"configured": True, "projects": [], "error": str(e)}
    projects = [
        {
            "key": p.get("key"),
            "name": p.get("name"),
            "type": p.get("projectTypeKey"),
            "lead": (p.get("lead") or {}).get("displayName"),
        }
        for p in values
    ]
    return {"configured": True, "count": len(projects), "projects": projects}


def _search(jql: str, max_results: int = 50) -> list[dict[str, Any]]:
    headers = _auth_header()
    if headers is None or not _base_url():
        return []
    url = f"{_base_url()}/rest/api/3/search/jql"
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
    project_name = get_project_name(project_key)
    issues = [_simplify(i) for i in _search(f"project={project_key} ORDER BY duedate ASC")]
    if not issues:
        return {
            "project_key": project_key,
            "project_name": project_name,
            "configured": _auth_header() is not None,
            "total": 0,
            "issues": [],
            "note": "No Jira data (project empty or Jira not configured yet).",
        }
    today = date.today().isoformat()

    def status_is(i: dict[str, Any], *names: str) -> bool:
        return (i["status"] or "").strip().lower() in names

    done = [i for i in issues if status_is(i, "done")]
    overdue = [
        i for i in issues
        if i["due_date"] and i["due_date"] < today and not status_is(i, "done")
    ]

    return {
        "project_key": project_key,
        "project_name": project_name,
        "configured": True,
        "total": len(issues),
        "done": len(done),
        "in_review": len([i for i in issues if status_is(i, "in review")]),
        "in_progress": len([i for i in issues if status_is(i, "in progress")]),
        "todo": len([i for i in issues if status_is(i, "to do", "todo")]),
        "idea": len([i for i in issues if status_is(i, "idea")]),
        "overdue": overdue,
        "issues": issues,
    }


def get_risks(project_key: str) -> dict[str, Any]:
    """High/Highest priority unresolved issues = risks."""
    project_name = get_project_name(project_key)
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
        "project_key": project_key,
        "project_name": project_name,
        "configured": _auth_header() is not None,
        "risk_count": len(issues),
        "severity": severity,
        "risks": issues,
    }