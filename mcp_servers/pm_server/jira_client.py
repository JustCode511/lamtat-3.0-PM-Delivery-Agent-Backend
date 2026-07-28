"""
Jira connector — thin wrapper over the Jira REST API.
Reads and writes live ticket data for the MCP server's tools.

Reliability: all HTTP calls use retry logic (3 attempts, exponential backoff).
Cost:        read responses are cached in-memory for 60 seconds so repeated
             queries within a session don't hammer the Jira API.
"""
from __future__ import annotations
import base64
import concurrent.futures
import logging
import os
import re
import time
from datetime import date, datetime, timezone, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple in-memory TTL cache  (60 s)
# Keyed by (function_name, *args).  Lambda-safe: cost is per-container, not
# per-invocation, so cold starts always fetch fresh data.
# ---------------------------------------------------------------------------
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 60  # seconds


def _cache_get(key: str) -> Any:
    entry = _CACHE.get(key)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.monotonic(), value)


def _cache_invalidate_prefix(prefix: str) -> None:
    for k in list(_CACHE.keys()):
        if k.startswith(prefix):
            del _CACHE[k]


# ---------------------------------------------------------------------------
# Auth / base URL helpers
# ---------------------------------------------------------------------------

def _auth_header() -> dict[str, str] | None:
    email = os.getenv("JIRA_EMAIL", "")
    token = os.getenv("JIRA_API_TOKEN", "")
    if not email or not token or "your-jira-token" in token:
        return None
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}


def _base_url() -> str:
    return os.getenv("JIRA_BASE_URL", "").rstrip("/")


# ---------------------------------------------------------------------------
# HTTP helper with retry
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict, params: dict | None = None) -> dict[str, Any]:
    """GET with 3 retries and exponential backoff on 429 / 5xx."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(url, headers=headers, params=params or {})
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    log.warning("[JIRA] rate-limited, retrying in %ds", wait)
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise
            last_exc = e
            time.sleep(2 ** attempt)
        except Exception as e:
            last_exc = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Jira GET failed after 3 attempts: {last_exc}") from last_exc


def _post(url: str, headers: dict, payload: dict) -> dict[str, Any]:
    """POST with 3 retries on 5xx."""
    last_exc: Exception | None = None
    content_headers = {**headers, "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(url, headers=content_headers, json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500:
                raise
            last_exc = e
            time.sleep(2 ** attempt)
        except Exception as e:
            last_exc = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Jira POST failed after 3 attempts: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_project_name(project_key: str) -> str:
    """Fetch the human-readable project name, cached."""
    headers = _auth_header()
    if headers is None or not _base_url():
        return project_key
    cache_key = f"project_name:{project_key}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    try:
        data = _get(f"{_base_url()}/rest/api/3/project/{project_key}", headers)
        name = data.get("name", project_key)
        _cache_set(cache_key, name)
        return name
    except Exception:
        return project_key


def list_projects() -> dict[str, Any]:
    """List all projects the user can access, with key and name. Cached."""
    headers = _auth_header()
    if headers is None or not _base_url():
        return {"configured": False, "projects": [], "note": "Jira not configured."}

    cache_key = "list_projects"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        data = _get(
            f"{_base_url()}/rest/api/3/project/search",
            headers,
            {"maxResults": "50", "expand": "lead"},
        )
        values = data.get("values", [])
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
    result = {"configured": True, "count": len(projects), "projects": projects}
    _cache_set(cache_key, result)
    return result


def search_issues(
    project_key: str = "",
    assignee: str = "",
    status: str = "",
    jql: str = "",
    max_results: int = 100,
) -> list[dict[str, Any]]:
    """Search Jira issues with optional filters. Supports assignee name (partial match), status, or raw JQL."""
    if jql:
        query = jql
    elif project_key:
        parts = [f"project={project_key}"]
        if status:
            parts.append(f'status="{status}"')
        query = " AND ".join(parts) + " ORDER BY updated DESC"
    else:
        # "ORDER BY ... " alone is invalid JQL — need at least one filter condition
        parts = []
        if status:
            parts.append(f'status="{status}"')
        base = " AND ".join(parts) if parts else "project is not EMPTY"
        query = base + " ORDER BY updated DESC"

    issues = [_simplify(i) for i in _search(query, max_results)]

    # Client-side assignee filter — works on display names, no accountId needed
    if assignee:
        a_lower = assignee.lower()
        issues = [i for i in issues if a_lower in (i.get("assignee") or "").lower()]

    return issues


def _search(jql: str, max_results: int = 50) -> list[dict[str, Any]]:
    """JQL search, cached, with full error handling."""
    headers = _auth_header()
    if headers is None or not _base_url():
        return []
    cache_key = f"search:{jql}:{max_results}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    params = {
        "jql": jql,
        "maxResults": str(max_results),
        "fields": "summary,status,duedate,priority,assignee,issuetype,created,resolutiondate",
    }
    try:
        data = _get(f"{_base_url()}/rest/api/3/search/jql", headers, params)
        issues = data.get("issues", [])
        _cache_set(cache_key, issues)
        return issues
    except Exception as e:
        log.error("[JIRA] search failed: %s", e)
        return []


def _simplify(issue: dict[str, Any]) -> dict[str, Any]:
    f = issue.get("fields", {})
    assignee = f.get("assignee")
    status_obj = f.get("status") or {}
    return {
        "key": issue.get("key"),
        "summary": f.get("summary"),
        "status": status_obj.get("name"),
        "status_category": (status_obj.get("statusCategory") or {}).get("key", ""),
        "issue_type": (f.get("issuetype") or {}).get("name"),
        "priority": (f.get("priority") or {}).get("name"),
        "due_date": f.get("duedate"),
        "assignee": assignee.get("displayName") if assignee else "Unassigned",
        # ISO timestamps — used for velocity calculation in delivery forecasts
        "created_date": (f.get("created") or "")[:10],        # "2026-06-15"
        "resolved_date": (f.get("resolutiondate") or "")[:10], # "2026-07-20" or ""
    }


def get_project_status(project_key: str) -> dict[str, Any]:
    """Milestone / status summary for a project. Cached."""
    cache_key = f"project_status:{project_key}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    project_name = get_project_name(project_key)
    issues = [_simplify(i) for i in _search(f"project={project_key} ORDER BY duedate ASC")]
    if not issues:
        result = {
            "project_key": project_key,
            "project_name": project_name,
            "configured": _auth_header() is not None,
            "total": 0,
            "issues": [],
            "note": "No Jira data (project empty or Jira not configured yet).",
        }
        _cache_set(cache_key, result)
        return result

    today = date.today().isoformat()

    def is_done(i: dict[str, Any]) -> bool:
        # Use statusCategory.key ("done") — works regardless of custom status names like "Closed", "Resolved", etc.
        if i.get("status_category") == "done":
            return True
        return (i.get("status") or "").strip().lower() in ("done", "closed", "resolved", "complete", "finished")

    def status_name_is(i: dict[str, Any], *names: str) -> bool:
        return (i.get("status") or "").strip().lower() in names

    done = [i for i in issues if is_done(i)]
    overdue = [
        i for i in issues
        if i["due_date"] and i["due_date"] < today and not is_done(i)
    ]

    result = {
        "project_key": project_key,
        "project_name": project_name,
        "configured": True,
        "today": today,
        "total": len(issues),
        "done": len(done),
        "in_review":    len([i for i in issues if status_name_is(i, "in review")]),
        "in_progress":  len([i for i in issues if status_name_is(i, "in progress")]),
        "todo":         len([i for i in issues if status_name_is(i, "to do", "todo")]),
        "idea":         len([i for i in issues if status_name_is(i, "idea")]),
        "overdue": overdue,
        "issues": issues,
    }
    _cache_set(cache_key, result)
    return result


# ---------------------------------------------------------------------------
# Comment-based risk detection
# ---------------------------------------------------------------------------

_RISK_PATTERNS: list[tuple[str, list[str]]] = [
    ("blocker",      ["blocked", "blocking", "blocker", "can't proceed", "cannot proceed",
                      "stuck", "impediment", "on hold because", "waiting for approval"]),
    ("out_of_scope", ["out of scope", "not in scope", "out-of-scope", "not planned",
                      "new requirement", "scope creep", "not part of", "never discussed"]),
    ("unclear",      ["unclear", "ambiguous", "no spec", "no design", "no requirement",
                      "not defined", "need clarification", "no clarity",
                      "missing documentation", "no documentation"]),
    ("dependency",   ["depends on", "dependency", "dependent on", "waiting on",
                      "need input from", "blocked by team", "blocked by another"]),
    ("risk",         ["at risk", "might miss", "won't be done", "cannot finish",
                      "delay", "delayed", "won't make", "going to miss"]),
]


def _extract_adf_text(node: Any) -> str:
    """Recursively extract plain text from Atlassian Document Format (ADF) JSON."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    return " ".join(
        _extract_adf_text(child)
        for child in node.get("content", [])
        if child
    )


def _detect_risk(text: str) -> tuple[str | None, list[str]]:
    """Return (risk_type, matched_keywords) or (None, []) if no risk signal found."""
    low = text.lower()
    for risk_type, keywords in _RISK_PATTERNS:
        matched = [kw for kw in keywords if kw in low]
        if matched:
            return risk_type, matched
    return None, []


def get_comment_risks(project_key: str) -> list[dict]:
    """
    Scan recent comments on unresolved issues in the project and return those
    that contain developer-flagged risk signals (blockers, scope issues, etc.).

    Fetches up to 25 recently-updated open issues, pulls their comments in
    parallel, and keyword-classifies each comment.
    """
    headers = _auth_header()
    if headers is None or not _base_url():
        return []

    cache_key = f"comment_risks:{project_key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Only unresolved, recently active issues
    jql = (
        f"project={project_key} AND statusCategory != Done "
        f"AND updated >= -30d ORDER BY updated DESC"
    )
    issues = _search(jql, max_results=25)
    if not issues:
        _cache_set(cache_key, [])
        return []

    def _fetch_comments(issue_key: str) -> tuple[str, list]:
        try:
            data = _get(
                f"{_base_url()}/rest/api/3/issue/{issue_key}/comment",
                headers,
                {"maxResults": "20", "orderBy": "-created"},
            )
            return issue_key, data.get("comments", [])
        except Exception:
            return issue_key, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        comment_map: dict[str, list] = dict(
            pool.map(_fetch_comments, [i["key"] for i in issues if i.get("key")])
        )

    risk_comments: list[dict] = []
    for issue in issues:
        key = issue.get("key", "")
        for raw in comment_map.get(key, []):
            body = raw.get("body", {})
            text = body if isinstance(body, str) else _extract_adf_text(body)
            text = text.strip()
            if len(text) < 10:
                continue
            risk_type, keywords = _detect_risk(text)
            if risk_type:
                risk_comments.append({
                    "issue_key":     key,
                    "issue_summary": issue.get("summary", ""),
                    "author":        (raw.get("author") or {}).get("displayName", "Unknown"),
                    "comment":       text[:400],
                    "risk_type":     risk_type,
                    "risk_keywords": keywords[:3],
                    "created":       raw.get("created", ""),
                })

    risk_comments.sort(key=lambda c: c["created"], reverse=True)
    result = risk_comments[:15]
    _cache_set(cache_key, result)
    return result


def get_risks(project_key: str) -> dict[str, Any]:
    """High/Highest priority unresolved issues = risks. Cached."""
    cache_key = f"risks:{project_key}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

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
    comment_risks = get_comment_risks(project_key)
    result = {
        "project_key": project_key,
        "project_name": project_name,
        "configured": _auth_header() is not None,
        "today": today,
        "risk_count": len(issues),
        "severity": severity,
        "risks": issues,
        "comment_risks": comment_risks,
    }
    _cache_set(cache_key, result)
    return result


def get_issue_types(project_key: str) -> list[str]:
    """Return the issue type names available in a project. Cached.
    Uses createmeta endpoint which works for both classic and team-managed projects.
    """
    headers = _auth_header()
    if headers is None or not _base_url():
        return ["Task", "Bug", "Story", "Epic"]
    cache_key = f"issue_types:{project_key}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    types: list[str] = []
    # Try createmeta first (works for team-managed + classic)
    try:
        data = _get(
            f"{_base_url()}/rest/api/3/issue/createmeta",
            headers,
            {"projectKeys": project_key, "expand": "projects.issuetypes"},
        )
        for proj in (data.get("projects") or []):
            if proj.get("key") == project_key:
                types = [t.get("name") for t in proj.get("issuetypes", []) if t.get("name")]
                break
    except Exception:
        pass
    # Fallback: direct project issuetypes endpoint (classic projects)
    if not types:
        try:
            data = _get(f"{_base_url()}/rest/api/3/project/{project_key}/issuetypes", headers)
            types = [t.get("name") for t in (data if isinstance(data, list) else []) if t.get("name")]
        except Exception:
            pass
    if not types:
        types = ["Task", "Bug", "Story", "Epic"]
    _cache_set(cache_key, types)
    log.info("[JIRA] issue types for %s: %s", project_key, types)
    return types


def lookup_user_by_name(name: str) -> str | None:
    """Search Jira users by display name or email; return accountId. Cached."""
    headers = _auth_header()
    if headers is None or not _base_url() or not name:
        return None
    cache_key = f"user_search:{name.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        data = _get(
            f"{_base_url()}/rest/api/3/user/search",
            headers,
            {"query": name, "maxResults": "5"},
        )
        if isinstance(data, list) and data:
            account_id = data[0].get("accountId")
            log.info("[JIRA] resolved user %r → accountId=%r", name, account_id)
            _cache_set(cache_key, account_id)
            return account_id
    except Exception as e:
        log.warning("[JIRA] user lookup failed for %r: %s", name, e)
    return None


def _transition_to_todo(issue_key: str, headers: dict) -> bool:
    """Transition a newly created issue to 'To Do' status if available. Best-effort."""
    try:
        data = _get(f"{_base_url()}/rest/api/3/issue/{issue_key}/transitions", headers)
        transitions = data.get("transitions", [])
        # Match "to do", "todo", "backlog", "open" — whatever is closest to not-started
        _TODO_NAMES = {"to do", "todo", "backlog", "open", "new", "ready"}
        match = next(
            (t for t in transitions if t.get("name", "").lower() in _TODO_NAMES),
            None,
        )
        if not match:
            log.info("[JIRA] no 'To Do' transition found for %s — leaving as-is", issue_key)
            return False
        _post(
            f"{_base_url()}/rest/api/3/issue/{issue_key}/transitions",
            headers,
            {"transition": {"id": match["id"]}},
        )
        log.info("[JIRA] transitioned %s → %r", issue_key, match["name"])
        return True
    except Exception as e:
        log.warning("[JIRA] transition failed for %s: %s", issue_key, e)
        return False


_ACTIVITY_CACHE_TTL = 5 * 60 * 60  # 5 hours; change to 30 for demo


def _parse_jira_dt(s: str) -> datetime | None:
    """Parse Jira timestamp strings like '2026-07-26T10:30:00.000+0000'."""
    if not s:
        return None
    try:
        # Normalize +0530 → +05:30 so fromisoformat() works on Python < 3.11
        normalized = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', s.split(".")[0])
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_recent_activity(project_keys: list[str], limit: int = 25) -> list[dict]:
    """
    Return recent Jira activity events (creates, status/assignee/priority changes)
    across the given projects, sorted newest-first.

    Strategy: /search/jql to get recently-updated issues, then fetch each issue's
    changelog via /issue/{key}/changelog in parallel (5 workers).
    This is necessary because Jira Cloud's /search/jql does not support
    expand=changelog, and the older /search endpoint is deprecated/broken on
    some Jira Cloud instances.
    """
    import concurrent.futures

    headers = _auth_header()
    if headers is None or not _base_url() or not project_keys:
        return []

    cache_key = f"activity:{','.join(sorted(project_keys))}:{limit}"
    entry = _CACHE.get(cache_key)
    if entry and (time.monotonic() - entry[0]) < _ACTIVITY_CACHE_TTL:
        return entry[1]

    # Step 1 — get the most recently updated issues
    keys_jql = ", ".join(project_keys)
    try:
        data = _get(f"{_base_url()}/rest/api/3/search/jql", headers, {
            "jql": f"project in ({keys_jql}) ORDER BY updated DESC",
            "maxResults": "15",
            "fields": "summary,created,updated,reporter,project,issuetype",
        })
    except Exception as exc:
        log.error("[JIRA] activity search failed: %s", exc)
        return []

    issues = data.get("issues", [])
    if not issues:
        _CACHE[cache_key] = (time.monotonic(), [])
        return []

    # Step 2 — fetch changelogs in parallel
    def _fetch_cl(issue_key: str) -> tuple[str, list]:
        try:
            cl = _get(
                f"{_base_url()}/rest/api/3/issue/{issue_key}/changelog",
                headers,
                {"maxResults": "50"},
            )
            return issue_key, cl.get("values", [])
        except Exception:
            return issue_key, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        cl_map: dict[str, list] = dict(pool.map(_fetch_cl, [i["key"] for i in issues]))

    # Step 3 — build events
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    events: list[dict] = []

    _FIELD_TO_TYPE = {
        "status":   "status_changed",
        "assignee": "assignee_changed",
        "priority": "priority_changed",
        "summary":  "renamed",
    }

    for issue in issues:
        key = issue["key"]
        fields = issue.get("fields", {})
        summary = fields.get("summary") or "—"
        proj = fields.get("project") or {}
        p_key = proj.get("key", "")
        p_name = proj.get("name", "")
        i_type = (fields.get("issuetype") or {}).get("name", "Task")
        reporter = (fields.get("reporter") or {}).get("displayName") or "Someone"

        # "created" event when the issue was recently created
        created_dt = _parse_jira_dt(fields.get("created", ""))
        if created_dt and created_dt > cutoff:
            events.append({
                "type": "created",
                "issue_key": key, "issue_summary": summary, "issue_type": i_type,
                "project_key": p_key, "project_name": p_name,
                "author": reporter,
                "from_value": None, "to_value": None,
                "timestamp": fields.get("created", ""),
            })

        # changelog events
        for history in cl_map.get(key, []):
            author = (history.get("author") or {}).get("displayName") or "Someone"
            ts = history.get("created", "")
            hist_dt = _parse_jira_dt(ts)
            if not hist_dt or hist_dt < cutoff:
                continue
            for item in history.get("items", []):
                ev_type = _FIELD_TO_TYPE.get(item.get("field", ""))
                if ev_type:
                    events.append({
                        "type": ev_type,
                        "issue_key": key, "issue_summary": summary, "issue_type": i_type,
                        "project_key": p_key, "project_name": p_name,
                        "author": author,
                        "from_value": item.get("fromString") or "",
                        "to_value":   item.get("toString")   or "",
                        "timestamp": ts,
                    })

    # Surface developer comment risks as activity events
    for pk in project_keys:
        for cr in get_comment_risks(pk):
            events.append({
                "type":          "comment_risk",
                "issue_key":     cr["issue_key"],
                "issue_summary": cr["issue_summary"],
                "issue_type":    "Task",
                "project_key":   pk,
                "project_name":  "",
                "author":        cr["author"],
                "from_value":    None,
                "to_value":      cr["risk_type"],
                "comment":       cr["comment"],
                "risk_keywords": cr["risk_keywords"],
                "timestamp":     cr["created"],
            })

    events.sort(key=lambda e: e["timestamp"], reverse=True)
    result = events[:limit]
    _CACHE[cache_key] = (time.monotonic(), result)
    return result


def create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Task",
    priority: str = "Medium",
    assignee_email: str | None = None,
    assignee_name: str | None = None,
) -> dict[str, Any]:
    """Create a new Jira issue and return its key + URL."""
    headers = _auth_header()
    if headers is None or not _base_url():
        return {"created": False, "note": "Jira not configured — cannot create issue."}

    # Validate + auto-correct the issue type against what the project actually supports
    available_types = get_issue_types(project_key)
    available_lower = {t.lower(): t for t in available_types}
    resolved_type = available_lower.get(issue_type.lower(), available_types[0] if available_types else "Task")
    if resolved_type != issue_type:
        log.info("[JIRA] issue_type %r not found in project — using %r instead", issue_type, resolved_type)

    payload: dict[str, Any] = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": resolved_type},
            "priority": {"name": priority},
        }
    }
    if description:
        payload["fields"]["description"] = {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": description}]}],
        }
    # Jira v3 requires accountId for assignee — resolve by name or email
    account_id: str | None = None
    if assignee_name:
        account_id = lookup_user_by_name(assignee_name)
    if not account_id and assignee_email:
        account_id = lookup_user_by_name(assignee_email)
    if account_id:
        payload["fields"]["assignee"] = {"accountId": account_id}

    try:
        data = _post(f"{_base_url()}/rest/api/3/issue", headers, payload)
        issue_key = data.get("key", "")
        issue_url = f"{_base_url()}/browse/{issue_key}" if issue_key else ""
        # Immediately transition to "To Do" (creation always lands on first workflow status)
        if issue_key:
            _transition_to_todo(issue_key, headers)
        _cache_invalidate_prefix(f"project_status:{project_key}")
        _cache_invalidate_prefix(f"search:project={project_key}")
        return {
            "created": True,
            "key": issue_key,
            "url": issue_url,
            "summary": summary,
            "project_key": project_key,
            "issue_type": resolved_type,
            "priority": priority,
        }
    except httpx.HTTPStatusError as e:
        # Parse Jira's structured error body for a human-readable message
        try:
            body = e.response.json()
            field_errors = body.get("errors", {})
            messages = body.get("errorMessages", [])
            detail = "; ".join(
                list(messages) + [f"{k}: {v}" for k, v in field_errors.items()]
            ) or e.response.text[:300]
        except Exception:
            detail = e.response.text[:300]
        log.error("[JIRA] create_issue failed %d: %s", e.response.status_code, detail)
        return {
            "created": False,
            "http_status": e.response.status_code,
            "error": detail,
            "available_issue_types": available_types,
        }
    except Exception as e:
        return {"created": False, "error": str(e)}
