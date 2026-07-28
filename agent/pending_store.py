"""
In-memory store for pending weekly report approvals.

Each approval lives here from creation until approved / rejected / expired.
Keyed by a UUID so the frontend and Slack can reference it safely.
"""
from __future__ import annotations
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]

ATTEMPT_WINDOW_MINUTES = 5   # how long each attempt waits
MAX_ATTEMPTS           = 3   # total attempts before auto-expiry
TTL_MINUTES            = ATTEMPT_WINDOW_MINUTES * MAX_ATTEMPTS  # 15 min total


@dataclass
class PendingApproval:
    id:          str
    report_text: str
    status:      ApprovalStatus
    created_at:  datetime
    expires_at:  datetime
    attempt:     int = 1     # which reminder was last sent (1-3)


# ── module-level singleton ────────────────────────────────────────────────
_store: dict[str, PendingApproval] = {}


def create(report_text: str) -> PendingApproval:
    approval_id = str(uuid.uuid4())
    now = datetime.utcnow()
    approval = PendingApproval(
        id=approval_id,
        report_text=report_text,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(minutes=TTL_MINUTES),
    )
    _store[approval_id] = approval
    return approval


def get(approval_id: str) -> PendingApproval | None:
    return _store.get(approval_id)


def get_all_pending() -> list[PendingApproval]:
    now = datetime.utcnow()
    return [a for a in _store.values() if a.status == "pending" and a.expires_at > now]


def update_status(approval_id: str, status: ApprovalStatus) -> PendingApproval | None:
    approval = _store.get(approval_id)
    if approval:
        approval.status = status
    return approval


def bump_attempt(approval_id: str) -> int:
    """Increment attempt counter, return new value."""
    approval = _store.get(approval_id)
    if approval:
        approval.attempt += 1
        return approval.attempt
    return 0


def expire_stale() -> int:
    """Mark overdue pending approvals as expired. Returns count expired."""
    now = datetime.utcnow()
    expired = [
        k for k, v in _store.items()
        if v.status == "pending" and v.expires_at <= now
    ]
    for k in expired:
        _store[k].status = "expired"
    return len(expired)
