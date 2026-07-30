"""
FastAPI app — entry point for both local dev and AWS Lambda.

Local:   uvicorn main:app --reload --port 8000
Lambda:  the `handler` export is the Mangum-wrapped ASGI handler;
         set handler=main.handler in Lambda function config.

Endpoints:
  GET  /health                  → liveness check
  POST /auth/login              → { "token": "...", "username": "..." }
  POST /auth/register           → { "token": "...", "username": "..." }
  GET  /pm/projects             → { "projects": [...] }
  GET  /pm/dashboard/{key}      → { project detail + risks }
  POST /pm/chat                 → { "session_id": "...", "message": "..." } → { "reply": "..." }
  POST /chat                    → { "session_id": "...", "message": "..." } → { "reply": "..." }
  GET  /export/ppt              → .pptx binary

Security:
  - JWT auth: /auth/register + /auth/login issue signed tokens; every other
    endpoint requires Authorization: Bearer <jwt>. Passwords are stored hashed
    (PBKDF2) in the UserStore, never in plaintext.
  - Message length capped at 2000 characters
  - CORS locked to known origins and methods only
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from agent.core import Agent
from shared.auth import create_jwt, decode_jwt, hash_password, verify_password
from shared.config import (
    get_conversation_store,
    get_llm,
    get_mcp_client,
    get_session_store,
    get_token_denylist,
    get_user_store,
)

log = logging.getLogger(__name__)

_llm = get_llm()
_store = get_session_store()
_mcp = get_mcp_client()
_users = get_user_store()
_denylist = get_token_denylist()
_conversations = get_conversation_store()

# Singleton Agent — graph is built once and reused across all requests
_agent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent
    await _mcp.connect()
    _agent = Agent(_llm, _store, _mcp)
    yield
    await _mcp.close()


def _is_reportable(user_message: str) -> bool:
    """True only when the user explicitly requests a formal report or risk flagging.
    General queries that happen to mention risks should NOT trigger approve/send buttons."""
    msg = user_message.lower()
    return "report" in msg and "send" in msg


app = FastAPI(title="PM Delivery Agent", lifespan=lifespan)

# ---------------------------------------------------------------------------
# CORS — only the known frontends, only the methods we actually use
# ---------------------------------------------------------------------------
_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)

# ---------------------------------------------------------------------------
# JWT authentication — every protected endpoint depends on get_current_user,
# which verifies the `Authorization: Bearer <jwt>` header and returns the
# authenticated username. Identity always comes from the signed token, never
# from a client-supplied field.
# ---------------------------------------------------------------------------

async def get_token_claims(authorization: str | None = Header(default=None)) -> dict:
    """Verify the bearer JWT — signature, expiry, and that it hasn't been
    revoked via sign-out — and return its claims."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token. Send Authorization: Bearer <token>.",
        )
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = decode_jwt(token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
        )
    if not payload.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed token payload.",
        )
    jti = payload.get("jti")
    if jti and await asyncio.to_thread(_denylist.is_revoked, jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been signed out. Please log in again.",
        )
    return payload


async def get_current_user(claims: dict = Depends(get_token_claims)) -> str:
    """Return the authenticated username from the verified token."""
    return claims["sub"]


async def _log_turn(user: str, session_id: str, user_message: str, reply: str, intent: str) -> None:
    """Archive one chat turn (user + assistant) for the history sidebar.
    Best-effort — a logging failure must never break the chat response."""
    try:
        await asyncio.to_thread(_conversations.append, user, session_id, "user", user_message)
        await asyncio.to_thread(_conversations.append, user, session_id, "assistant", reply, intent)
    except Exception as exc:  # noqa: BLE001
        log.warning("conversation archive failed for session=%s: %s", session_id, exc)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
MAX_MESSAGE_LEN = 2000


class ChatRequest(BaseModel):
    session_id: str = Field(default_factory=lambda: f"session-{uuid.uuid4().hex[:8]}")
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    ui_hint: str | None = None
    reportable: bool = False


class AuthRequest(BaseModel):
    # Registration enforces the credential policy (3+/6+).
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=128)


class LoginRequest(BaseModel):
    # Login only checks whether the credentials match — it does NOT enforce the
    # length policy, so a wrong/short password returns 401 "invalid credentials"
    # rather than a validation error meant for the registration form.
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=128)


class AuthResponse(BaseModel):
    token: str
    username: str


class ConversationSummary(BaseModel):
    session_id: str
    title: str
    updated_at: str | None = None
    message_count: int = 0


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummary]


class ConversationMessage(BaseModel):
    role: str
    content: str
    ui_hint: str | None = None
    created_at: str | None = None


class ConversationDetailResponse(BaseModel):
    session_id: str
    messages: list[ConversationMessage]


class ProjectSummary(BaseModel):
    key: str
    name: str
    health: str
    completion_pct: int
    overdue_count: int
    total: int
    done: int


class ProjectsResponse(BaseModel):
    projects: list[ProjectSummary]


class RiskItem(BaseModel):
    key: str
    summary: str
    priority: str
    days_overdue: int
    unassigned: bool
    root_cause: str
    fix: str
    effort: str


class StatusCount(BaseModel):
    status: str
    count: int
    pct: int


class TypeCount(BaseModel):
    type: str
    count: int
    pct: int


class TeamMember(BaseModel):
    assignee: str
    count: int
    pct: int


class DashboardResponse(BaseModel):
    project_key: str
    project_name: str
    health: str
    completion_pct: int
    total: int
    done: int
    in_progress_count: int
    todo_count: int
    overdue_count: int
    unassigned_count: int
    critical_risks: list[RiskItem]
    status_breakdown: list[StatusCount]
    priority_breakdown: dict[str, int]
    type_breakdown: list[TypeCount]
    team_workload: list[TeamMember]


# ---------------------------------------------------------------------------
# PM helper functions
# ---------------------------------------------------------------------------

def _compute_health(overdue_count: int, completion_pct: int) -> str:
    if overdue_count >= 3 or completion_pct < 20:
        return "AT_RISK"
    if overdue_count >= 1 or completion_pct < 50:
        return "NEEDS_ATTENTION"
    return "HEALTHY"


def _days_overdue(due_date: str | None) -> int:
    if not due_date:
        return 0
    try:
        delta = (date.today() - date.fromisoformat(due_date)).days
        return max(0, delta)
    except ValueError:
        return 0


def _derive_risk_insight(risk: dict) -> tuple[str, str, str]:
    priority = risk.get("priority", "Medium") or "Medium"
    is_unassigned = (risk.get("assignee") or "") in ("Unassigned", "")
    days = _days_overdue(risk.get("due_date"))

    causes = []
    if is_unassigned:
        causes.append("no owner assigned")
    if days > 0:
        causes.append(f"overdue by {days} day{'s' if days != 1 else ''}")
    if priority in ("Highest", "High"):
        causes.append("high business priority")
    root_cause = ("Issue has " + " and ".join(causes) + ".") if causes else "Requires immediate attention."

    if is_unassigned:
        fix = "Assign an owner immediately and set a concrete deadline."
    elif days > 0:
        fix = "Escalate to the team lead. Reschedule or unblock within 24 hours."
    else:
        fix = "Review progress and ensure any blockers are cleared."

    effort = {"Highest": "High", "High": "High", "Medium": "Medium", "Low": "Low"}.get(priority, "Medium")
    return root_cause, fix, effort


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: str = Depends(get_current_user)) -> ChatResponse:
    reply, intent = await _agent.chat(req.session_id, req.message)
    await _log_turn(user, req.session_id, req.message, reply, intent)
    return ChatResponse(reply=reply, session_id=req.session_id, ui_hint=intent, reportable=_is_reportable(req.message))


# ---------------------------------------------------------------------------
# PPT export — GET /export/ppt?project_key=SCRUM  (omit for all projects)
# ---------------------------------------------------------------------------

@app.get("/export/ppt", dependencies=[Depends(get_current_user)])
async def export_ppt(
    project_key: str | None = Query(default=None, description="Jira project key; omit for all projects"),
) -> Response:
    """Generate and stream a .pptx status report. No file is saved on disk."""
    from mcp_servers.pm_server import jira_client
    from adapters.ppt_generator import generate_ppt_bytes

    # Fetch data — run IO-bound Jira calls in a thread pool
    if project_key:
        keys = [project_key.upper()]
    else:
        projects_raw = await asyncio.to_thread(jira_client.list_projects)
        keys = [p["key"] for p in projects_raw.get("projects", []) if p.get("key")]

    if not keys:
        raise HTTPException(status_code=404, detail="No projects found in Jira.")

    statuses, risks = await asyncio.gather(
        asyncio.gather(*[asyncio.to_thread(jira_client.get_project_status, k) for k in keys]),
        asyncio.gather(*[asyncio.to_thread(jira_client.get_risks, k) for k in keys]),
    )

    pptx_bytes = await asyncio.to_thread(
        generate_ppt_bytes, list(statuses), list(risks)
    )

    today = date.today().strftime("%Y-%m-%d")
    filename = f"PM_Report_{project_key or 'Portfolio'}_{today}.pptx"
    return Response(
        content=pptx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Auth endpoints — real credentials stored (hashed) in the UserStore.
# Register hashes the password with PBKDF2 and rejects duplicate usernames;
# login verifies the hash and issues a signed JWT. The password is never
# stored or returned in plaintext.
# ---------------------------------------------------------------------------

@app.post("/auth/register", response_model=AuthResponse)
async def auth_register(req: AuthRequest) -> AuthResponse:
    pw_hash, salt = hash_password(req.password)
    created = await asyncio.to_thread(_users.create_user, req.username, pw_hash, salt)
    if not created:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists.")
    return AuthResponse(token=create_jwt(req.username), username=req.username)


@app.post("/auth/login", response_model=AuthResponse)
async def auth_login(req: LoginRequest) -> AuthResponse:
    user = await asyncio.to_thread(_users.get_user, req.username)
    if not user or not verify_password(req.password, user["password_hash"], user["salt"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password.")
    return AuthResponse(token=create_jwt(req.username), username=req.username)


@app.post("/auth/logout")
async def auth_logout(claims: dict = Depends(get_token_claims)) -> dict:
    """Voluntary sign-out — revoke the current token so it cannot be reused,
    even though it hasn't expired yet."""
    jti = claims.get("jti")
    exp = claims.get("exp")
    if jti:
        await asyncio.to_thread(_denylist.revoke, jti, int(exp) if exp else None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# PM REST endpoints — thin wrappers over the Jira client
# ---------------------------------------------------------------------------

@app.get("/pm/projects", response_model=ProjectsResponse, dependencies=[Depends(get_current_user)])
async def pm_projects() -> ProjectsResponse:
    from mcp_servers.pm_server import jira_client

    raw = await asyncio.to_thread(jira_client.list_projects)
    keys = [p["key"] for p in raw.get("projects", []) if p.get("key")]

    if not keys:
        return ProjectsResponse(projects=[])

    statuses = await asyncio.gather(*[asyncio.to_thread(jira_client.get_project_status, k) for k in keys])

    projects: list[ProjectSummary] = []
    for s in statuses:
        total = s.get("total", 0)
        done = s.get("done", 0)
        overdue_count = len(s.get("overdue", []))
        completion_pct = int(done / total * 100) if total > 0 else 0
        raw_proj = next((p for p in raw.get("projects", []) if p.get("key") == s.get("project_key")), {})
        projects.append(ProjectSummary(
            key=s.get("project_key", ""),
            name=s.get("project_name") or raw_proj.get("name", s.get("project_key", "")),
            health=_compute_health(overdue_count, completion_pct),
            completion_pct=completion_pct,
            overdue_count=overdue_count,
            total=total,
            done=done,
        ))

    return ProjectsResponse(projects=projects)


@app.get("/pm/dashboard/{project_key}", response_model=DashboardResponse, dependencies=[Depends(get_current_user)])
async def pm_dashboard(project_key: str) -> DashboardResponse:
    from mcp_servers.pm_server import jira_client

    key = project_key.upper()
    status_data, risks_data = await asyncio.gather(
        asyncio.to_thread(jira_client.get_project_status, key),
        asyncio.to_thread(jira_client.get_risks, key),
    )

    total = status_data.get("total", 0)
    done = status_data.get("done", 0)
    overdue_list = status_data.get("overdue", [])
    overdue_count = len(overdue_list)
    completion_pct = int(done / total * 100) if total > 0 else 0
    all_issues = status_data.get("issues", [])
    n = len(all_issues) or 1

    # ── breakdowns ────────────────────────────────────────────────
    status_counts: dict[str, int] = {}
    priority_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    team_counts: dict[str, int] = {}

    for issue in all_issues:
        st = (issue.get("status") or "Unknown").strip()
        status_counts[st] = status_counts.get(st, 0) + 1

        pr = (issue.get("priority") or "Medium").strip()
        priority_counts[pr] = priority_counts.get(pr, 0) + 1

        tp = (issue.get("issue_type") or issue.get("type") or "Task").strip()
        type_counts[tp] = type_counts.get(tp, 0) + 1

        ag = (issue.get("assignee") or "").strip()
        ag = ag if ag and ag != "Unassigned" else "Unassigned"
        team_counts[ag] = team_counts.get(ag, 0) + 1

    unassigned_count = team_counts.get("Unassigned", 0)

    in_progress_count = sum(
        v for k, v in status_counts.items()
        if any(w in k.lower() for w in ("progress", "review", "doing", "active"))
    )
    todo_count = sum(
        v for k, v in status_counts.items()
        if any(w in k.lower() for w in ("to do", "todo", "open", "backlog", "new", "not started"))
    )

    status_breakdown = [
        StatusCount(status=k, count=v, pct=round(v / n * 100))
        for k, v in sorted(status_counts.items(), key=lambda x: -x[1])
    ]
    type_breakdown = [
        TypeCount(type=k, count=v, pct=round(v / n * 100))
        for k, v in sorted(type_counts.items(), key=lambda x: -x[1])
    ]
    team_workload = [
        TeamMember(assignee=k, count=v, pct=round(v / n * 100))
        for k, v in sorted(team_counts.items(), key=lambda x: -x[1])
    ]

    # ── priority canonical order ───────────────────────────────────
    prio_order = ["Highest", "High", "Medium", "Low", "Lowest"]
    priority_breakdown = {p: priority_counts.get(p, 0) for p in prio_order}

    critical_risks: list[RiskItem] = []
    for r in risks_data.get("risks", []):
        root_cause, fix, effort = _derive_risk_insight(r)
        days = _days_overdue(r.get("due_date"))
        critical_risks.append(RiskItem(
            key=r.get("key", ""),
            summary=r.get("summary", ""),
            priority=r.get("priority") or "Medium",
            days_overdue=days,
            unassigned=(r.get("assignee") or "") in ("Unassigned", ""),
            root_cause=root_cause,
            fix=fix,
            effort=effort,
        ))

    return DashboardResponse(
        project_key=key,
        project_name=status_data.get("project_name", key),
        health=_compute_health(overdue_count, completion_pct),
        completion_pct=completion_pct,
        total=total,
        done=done,
        in_progress_count=in_progress_count,
        todo_count=todo_count,
        overdue_count=overdue_count,
        unassigned_count=unassigned_count,
        critical_risks=critical_risks,
        status_breakdown=status_breakdown,
        priority_breakdown=priority_breakdown,
        type_breakdown=type_breakdown,
        team_workload=team_workload,
    )


# ---------------------------------------------------------------------------
# PM module chat — delegates to the same agent as /chat
# ---------------------------------------------------------------------------

@app.post("/pm/chat", response_model=ChatResponse)
async def pm_chat(req: ChatRequest, user: str = Depends(get_current_user)) -> ChatResponse:
    reply, intent = await _agent.chat(req.session_id, req.message)
    await _log_turn(user, req.session_id, req.message, reply, intent)
    return ChatResponse(reply=reply, session_id=req.session_id, ui_hint=intent, reportable=_is_reportable(req.message))


# ---------------------------------------------------------------------------
# Conversation history — powers the Claude-style sidebar. Scoped to the
# authenticated user; you can only list and replay your own conversations.
# ---------------------------------------------------------------------------

@app.get("/pm/conversations", response_model=ConversationListResponse)
async def list_conversations(user: str = Depends(get_current_user)) -> ConversationListResponse:
    items = await asyncio.to_thread(_conversations.list_conversations, user)
    return ConversationListResponse(conversations=[ConversationSummary(**it) for it in items])


@app.get("/pm/conversations/{session_id}", response_model=ConversationDetailResponse)
async def get_conversation(session_id: str, user: str = Depends(get_current_user)) -> ConversationDetailResponse:
    msgs = await asyncio.to_thread(_conversations.get_messages, user, session_id)
    if msgs is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
    return ConversationDetailResponse(
        session_id=session_id,
        messages=[ConversationMessage(**m) for m in msgs],
    )


# ---------------------------------------------------------------------------
# Streaming chat — SSE endpoint that fakes character-level streaming for
# a live-typing effect while we wait for the agent (which runs to completion).
# ---------------------------------------------------------------------------

@app.post("/pm/chat/stream")
async def pm_chat_stream(req: ChatRequest, user: str = Depends(get_current_user)) -> StreamingResponse:
    """Stream the agent reply as SSE so the UI can render text as it arrives."""

    async def generate():
        try:
            reply, intent = await _agent.chat(req.session_id, req.message)
        except Exception as exc:
            reply, intent = f"Agent error: {exc}", "default"

        # Archive the turn for the history sidebar before streaming it out.
        await _log_turn(user, req.session_id, req.message, reply, intent)

        # Tell the client the intent first so it can prepare the right renderer
        yield f"data: {json.dumps({'type': 'start', 'intent': intent})}\n\n"

        # Stream in small chunks for a smooth typing effect
        chunk = 6
        for i in range(0, len(reply), chunk):
            yield f"data: {json.dumps({'type': 'delta', 'delta': reply[i : i + chunk]})}\n\n"

        yield f"data: {json.dumps({'type': 'done', 'intent': intent, 'full': reply, 'reportable': _is_reportable(req.message)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# CopilotKit AG-UI protocol endpoint — streams agent replies as SSE
# so the @copilotkit/react-ui CopilotChat component works out of the box.
# ---------------------------------------------------------------------------

@app.post("/copilotkit")
async def copilotkit_runtime(request: Request) -> StreamingResponse:
    """Minimal AG-UI protocol SSE endpoint consumed by the CopilotKit frontend."""
    body = await request.json()
    messages = body.get("messages", [])
    thread_id = body.get("threadId", str(uuid.uuid4()))

    last_user = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "",
    )

    async def stream():
        run_id = str(uuid.uuid4())
        msg_id = str(uuid.uuid4())

        def e(obj: dict) -> str:
            return f"data: {json.dumps(obj)}\n\n"

        yield e({"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id})

        if not last_user or not _agent:
            yield e({"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id})
            return

        try:
            reply, intent = await _agent.chat(thread_id, last_user)
        except Exception as exc:
            reply, intent = f"Agent error: {exc}", "default"

        # Embed the intent as a hidden prefix so the frontend picks the right
        # rich UI component without needing useCoAgent state wiring.
        content = f"{{intent:{intent}}}\n{reply}"

        yield e({"type": "TEXT_MESSAGE_START", "messageId": msg_id, "role": "assistant"})

        chunk = 8
        for i in range(0, len(content), chunk):
            yield e({"type": "TEXT_MESSAGE_CONTENT", "messageId": msg_id, "delta": content[i : i + chunk]})

        yield e({"type": "TEXT_MESSAGE_END", "messageId": msg_id})
        yield e({"type": "STATE_SNAPSHOT", "snapshot": {"intent": intent, "result": reply}})
        yield e({"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )




# ---------------------------------------------------------------------------
# HITL — Inline "Send to Leadership" approval
# ---------------------------------------------------------------------------

class LeadershipRequest(BaseModel):
    report_text: str = Field(..., min_length=1)


@app.get("/pm/activity", dependencies=[Depends(get_current_user)])
async def pm_activity(limit: int = Query(default=25, ge=1, le=50)) -> dict:
    """Recent Jira activity events (creates, status/assignee/priority changes)."""
    from mcp_servers.pm_server import jira_client
    raw = await asyncio.to_thread(jira_client.list_projects)
    keys = [p["key"] for p in raw.get("projects", []) if p.get("key")]
    events = await asyncio.to_thread(jira_client.get_recent_activity, keys, limit)
    return {"events": events, "count": len(events)}


@app.post("/pm/send-to-leadership", dependencies=[Depends(get_current_user)])
async def send_to_leadership(req: LeadershipRequest) -> dict:
    """PM clicked 'Send to Leadership' — post the report directly to Slack channel."""
    from mcp_servers.pm_server.slack_client import post_to_channel
    channel_id = os.getenv("LEADERSHIP_SLACK_CHANNEL_ID", "")
    if not channel_id:
        raise HTTPException(status_code=500, detail="LEADERSHIP_SLACK_CHANNEL_ID not configured")
    result = await asyncio.to_thread(post_to_channel, channel_id, req.report_text)
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error", "Slack post failed"))
    return {"ok": True, "ts": result.get("ts")}


# ---------------------------------------------------------------------------
# Lambda handler — Mangum wraps the FastAPI ASGI app.
# InlineMCPClient.connect/close are no-ops so lifespan works fine on Lambda.
# ---------------------------------------------------------------------------
try:
    from mangum import Mangum
    handler = Mangum(app, lifespan="auto")
except ImportError:
    handler = None  # mangum not installed locally; uvicorn is used instead
