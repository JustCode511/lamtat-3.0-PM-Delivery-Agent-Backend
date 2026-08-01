"""
Pydantic models for the Cloud FinOps module.

All cost data is read live from AWS (Cost Explorer, Compute Optimizer, EC2,
Budgets) — see finops/aws_client.py. These models are the shapes the service
layer normalises that live data into before handing it to routes/agent.
"""
from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field


class ServiceCost(BaseModel):
    service: str
    amount: float
    pct_of_total: float = 0.0


class DailyCostPoint(BaseModel):
    date: str
    amount: float


class CostSummary(BaseModel):
    account_id: Optional[str] = None
    currency: str = "USD"
    period_start: str
    period_end: str
    total_30d: float
    total_mtd: float
    daily_avg: float
    by_service: list[ServiceCost] = Field(default_factory=list)
    active_services: list[str] = Field(default_factory=list)
    daily_trend: list[DailyCostPoint] = Field(default_factory=list)
    data_source: str = "aws_cost_explorer"


class Anomaly(BaseModel):
    id: str
    date: str
    scope: str  # e.g. "Total spend" or a service name
    expected_amount: float
    actual_amount: float
    delta_amount: float
    delta_pct: float
    severity: str  # "critical" | "high" | "medium"
    root_cause: str
    recommended_action: str
    source: str  # "statistical" | "aws_anomaly_detection"


class AnomalyResult(BaseModel):
    anomalies: list[Anomaly] = Field(default_factory=list)
    monitors_configured: bool = False
    note: Optional[str] = None


class RightsizingRecommendation(BaseModel):
    resource_id: str
    resource_type: str  # "EC2 Instance" | "EBS Volume" | ...
    region: str
    finding: str
    current_spec: str
    recommended_spec: str
    estimated_monthly_savings: float
    reason: str


class RightsizingResult(BaseModel):
    compute_optimizer_enrolled: bool
    ec2_instance_count: int
    recommendations: list[RightsizingRecommendation] = Field(default_factory=list)
    total_estimated_monthly_savings: float = 0.0
    note: Optional[str] = None


class BudgetStatus(BaseModel):
    target_monthly_budget: Optional[float] = None
    mtd_spend: float
    forecast_month_end: Optional[float] = None
    forecast_source: str = "none"  # "aws_forecast" | "linear_projection" | "none"
    pct_used: Optional[float] = None
    status: str = "no_budget_set"  # "on_track" | "at_risk" | "over_budget" | "no_budget_set"
    aws_budgets: list[dict[str, Any]] = Field(default_factory=list)


class SetBudgetRequest(BaseModel):
    target_monthly_budget: float = Field(..., ge=0)


class DashboardStats(BaseModel):
    cost_summary: CostSummary
    anomalies: AnomalyResult
    rightsizing: RightsizingResult
    budget: BudgetStatus
    potential_savings_total: float
    headline: str


class SlackDigestRequest(BaseModel):
    channel_id: Optional[str] = None


class SlackDigestResult(BaseModel):
    sent: bool
    note: Optional[str] = None
    error: Optional[str] = None
