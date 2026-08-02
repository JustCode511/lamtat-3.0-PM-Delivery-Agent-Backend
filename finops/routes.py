"""
FastAPI router for the Cloud FinOps module.

All endpoints are prefixed with /finops and require a bearer token in the
Authorization header (same JWT tokens issued by /auth/login). Mirrors
talent/routes.py's auth-dependency pattern; chat endpoints live in main.py
alongside the other modules' chat endpoints.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from finops.models import SetBudgetRequest, SlackDigestRequest
from finops.services import FinOpsService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/finops", tags=["finops"])


async def verify_api_key(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. Expected: Bearer <token>.",
        )
    return authorization.removeprefix("Bearer ").strip()


# Single shared service instance (stateless business logic; each call hits
# live AWS APIs, so there's no in-process state to protect)
_svc = FinOpsService()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@router.get("/dashboard", dependencies=[Depends(verify_api_key)])
async def finops_dashboard() -> dict[str, Any]:
    """Aggregate FinOps view: cost summary, anomalies, rightsizing, budget."""
    result = await asyncio.to_thread(_svc.get_dashboard)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Costs
# ---------------------------------------------------------------------------

@router.get("/costs", dependencies=[Depends(verify_api_key)])
async def finops_costs(
    days: int = Query(default=30, ge=1, le=90, description="Lookback window in days"),
) -> dict[str, Any]:
    """Live AWS Cost Explorer summary: daily trend + spend by service."""
    result = await asyncio.to_thread(_svc.get_cost_summary, days)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------

@router.get("/anomalies", dependencies=[Depends(verify_api_key)])
async def finops_anomalies() -> dict[str, Any]:
    """AI/statistical cost anomaly detection over live spend data."""
    result = await asyncio.to_thread(_svc.get_anomalies)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Rightsizing
# ---------------------------------------------------------------------------

@router.get("/rightsizing", dependencies=[Depends(verify_api_key)])
async def finops_rightsizing() -> dict[str, Any]:
    """Compute Optimizer + idle-resource rightsizing recommendations."""
    result = await asyncio.to_thread(_svc.get_rightsizing)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

@router.get("/budget", dependencies=[Depends(verify_api_key)])
async def finops_budget() -> dict[str, Any]:
    """Budget vs. actual spend + month-end forecast."""
    result = await asyncio.to_thread(_svc.get_budget_status)
    return result.model_dump()


@router.post("/budget", dependencies=[Depends(verify_api_key)])
async def finops_set_budget(req: SetBudgetRequest) -> dict[str, Any]:
    """Set (or update) the target monthly budget used for status/forecast comparisons."""
    result = await asyncio.to_thread(_svc.set_target_budget, req.target_monthly_budget)
    return result.model_dump()


# ---------------------------------------------------------------------------
# Slack digest (human-in-the-loop action, mirrors /pm/send-to-leadership)
# ---------------------------------------------------------------------------

@router.post("/slack-digest", dependencies=[Depends(verify_api_key)])
async def finops_slack_digest(req: SlackDigestRequest) -> dict[str, Any]:
    """Send the current cost/anomaly/rightsizing digest to Slack."""
    result = await asyncio.to_thread(_svc.send_slack_digest, req.channel_id)
    if not result.sent:
        raise HTTPException(status_code=502, detail=result.error or result.note or "Slack send failed")
    return result.model_dump()
