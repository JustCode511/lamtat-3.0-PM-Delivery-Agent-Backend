"""
Thin boto3 wrapper around the real AWS FinOps surface: Cost Explorer,
Cost Anomaly Detection, Compute Optimizer, EC2/EBS inventory, and Budgets.

Every call here is a genuine AWS API call — there is no seeded/mock data.
Sandbox accounts commonly aren't opted into Compute Optimizer / Rightsizing,
have no configured Anomaly Monitors, and have too little cost history for
GetCostForecast, so each function catches its own ClientError/BotoCoreError
and degrades to an empty/None result with a logged reason rather than
raising — callers (finops/services.py) turn that into an honest UI state
("Compute Optimizer not enrolled for this account", etc.) instead of a 500.

Credentials: local dev picks up the default boto3 credential chain (aws
configure / env vars); on Lambda the execution role provides them — same
pattern as shared/observability.py's CloudWatch client.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)

# Cost Explorer and Budgets are global services but only expose an API
# endpoint in us-east-1, regardless of which region resources actually run in.
_CE_REGION = "us-east-1"
_RESOURCE_REGION = os.getenv("AWS_REGION", "us-east-1")


def _ce():
    return boto3.client("ce", region_name=_CE_REGION)


def _budgets():
    return boto3.client("budgets", region_name=_CE_REGION)


def _compute_optimizer():
    return boto3.client("compute-optimizer", region_name=_RESOURCE_REGION)


def _ec2():
    return boto3.client("ec2", region_name=_RESOURCE_REGION)


def _sts():
    return boto3.client("sts", region_name=_RESOURCE_REGION)


def get_account_id() -> Optional[str]:
    try:
        return _sts().get_caller_identity()["Account"]
    except (ClientError, BotoCoreError) as exc:
        log.warning("[FINOPS] sts.get_caller_identity failed: %s", exc)
        return None


def get_daily_cost(days: int = 30) -> list[dict[str, Any]]:
    """Daily UnblendedCost totals for the trailing `days` days."""
    end = date.today()
    start = end - timedelta(days=days)
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        )
    except (ClientError, BotoCoreError) as exc:
        log.warning("[FINOPS] ce.get_cost_and_usage(daily) failed: %s", exc)
        return []

    points = []
    for r in resp.get("ResultsByTime", []):
        amount = float(r.get("Total", {}).get("UnblendedCost", {}).get("Amount", 0.0))
        points.append({"date": r["TimePeriod"]["Start"], "amount": amount})
    return points


def get_cost_by_service(days: int = 30) -> list[dict[str, Any]]:
    """Total UnblendedCost per AWS service over the trailing `days` days."""
    end = date.today()
    start = end - timedelta(days=days)
    try:
        resp = _ce().get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
    except (ClientError, BotoCoreError) as exc:
        log.warning("[FINOPS] ce.get_cost_and_usage(by service) failed: %s", exc)
        return []

    totals: dict[str, float] = defaultdict(float)
    for r in resp.get("ResultsByTime", []):
        for g in r.get("Groups", []):
            service = g["Keys"][0]
            amount = float(g.get("Metrics", {}).get("UnblendedCost", {}).get("Amount", 0.0))
            totals[service] += amount
    # Include every service with a usage record, even ones that billed exactly
    # $0 (free tier, or usage too small to round above zero) — a service
    # showing here means the account actually used it, not that it cost money.
    return [{"service": s, "amount": a} for s, a in totals.items()]


def get_active_services(days: int = 30) -> list[str]:
    """Every AWS service with at least one usage record in the window,
    independent of cost — a straight service inventory via Cost Explorer's
    dimension catalog rather than derived from cost aggregation."""
    end = date.today()
    start = end - timedelta(days=days)
    try:
        resp = _ce().get_dimension_values(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Dimension="SERVICE",
            Context="COST_AND_USAGE",
        )
    except (ClientError, BotoCoreError) as exc:
        log.warning("[FINOPS] ce.get_dimension_values(SERVICE) failed: %s", exc)
        return []
    return sorted(v["Value"] for v in resp.get("DimensionValues", []) if v.get("Value"))


def get_cost_forecast(days_ahead: int = 7) -> Optional[float]:
    """Forecasted UnblendedCost for the next `days_ahead` days, or None if
    AWS can't produce one (commonly: insufficient historical data)."""
    start = date.today()
    end = start + timedelta(days=days_ahead)
    try:
        resp = _ce().get_cost_forecast(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Metric="UNBLENDED_COST",
            Granularity="DAILY",
        )
        return float(resp["Total"]["Amount"])
    except (ClientError, BotoCoreError) as exc:
        log.info("[FINOPS] ce.get_cost_forecast unavailable: %s", exc)
        return None


def get_native_anomalies(days: int = 30) -> list[dict[str, Any]]:
    """AWS Cost Anomaly Detection results — empty unless the account has
    Anomaly Monitors configured (console/API, one-time setup, needs a
    baseline period before it detects anything)."""
    start = date.today() - timedelta(days=days)
    try:
        resp = _ce().get_anomalies(
            DateInterval={"StartDate": start.isoformat(), "EndDate": date.today().isoformat()}
        )
        return resp.get("Anomalies", [])
    except (ClientError, BotoCoreError) as exc:
        log.info("[FINOPS] ce.get_anomalies unavailable: %s", exc)
        return []


def has_anomaly_monitors() -> bool:
    try:
        resp = _ce().get_anomaly_monitors()
        return len(resp.get("AnomalyMonitors", [])) > 0
    except (ClientError, BotoCoreError) as exc:
        log.info("[FINOPS] ce.get_anomaly_monitors unavailable: %s", exc)
        return False


def get_compute_optimizer_recommendations() -> dict[str, Any]:
    """EC2 rightsizing recommendations from Compute Optimizer.

    Returns {"enrolled": bool, "recommendations": [...]}. Most sandbox
    accounts return OptInRequiredException until someone opts the account
    in from the console/API and waits ~24h for data to populate — that is
    reported back as enrolled=False rather than raised.
    """
    try:
        resp = _compute_optimizer().get_ec2_instance_recommendations()
        return {"enrolled": True, "recommendations": resp.get("instanceRecommendations", [])}
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "OptInRequiredException":
            return {"enrolled": False, "recommendations": []}
        log.warning("[FINOPS] compute-optimizer.get_ec2_instance_recommendations failed: %s", exc)
        return {"enrolled": False, "recommendations": []}
    except BotoCoreError as exc:
        log.warning("[FINOPS] compute-optimizer.get_ec2_instance_recommendations failed: %s", exc)
        return {"enrolled": False, "recommendations": []}


def get_ec2_instances() -> list[dict[str, Any]]:
    """All EC2 instances in the configured region — used as a rightsizing
    fallback signal (stopped/idle instances) when Compute Optimizer isn't
    enrolled."""
    try:
        resp = _ec2().describe_instances()
    except (ClientError, BotoCoreError) as exc:
        log.warning("[FINOPS] ec2.describe_instances failed: %s", exc)
        return []

    instances = []
    for reservation in resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            instances.append({
                "id": inst.get("InstanceId"),
                "type": inst.get("InstanceType"),
                "state": inst.get("State", {}).get("Name"),
                "launch_time": str(inst.get("LaunchTime", "")),
            })
    return instances


def get_unattached_ebs_volumes() -> list[dict[str, Any]]:
    """EBS volumes with no attachments — pure waste, flagged regardless of
    Compute Optimizer enrollment."""
    try:
        resp = _ec2().describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}])
    except (ClientError, BotoCoreError) as exc:
        log.warning("[FINOPS] ec2.describe_volumes failed: %s", exc)
        return []

    return [
        {
            "id": v.get("VolumeId"),
            "size_gb": v.get("Size"),
            "type": v.get("VolumeType"),
            "region": _RESOURCE_REGION,
        }
        for v in resp.get("Volumes", [])
    ]


def get_aws_budgets(account_id: Optional[str]) -> list[dict[str, Any]]:
    """Budgets configured in AWS Budgets (console/API) for this account."""
    if not account_id:
        return []
    try:
        resp = _budgets().describe_budgets(AccountId=account_id)
        out = []
        for b in resp.get("Budgets", []):
            out.append({
                "name": b.get("BudgetName"),
                "limit": float(b.get("BudgetLimit", {}).get("Amount", 0.0)),
                "actual_spend": float(b.get("CalculatedSpend", {}).get("ActualSpend", {}).get("Amount", 0.0)),
            })
        return out
    except (ClientError, BotoCoreError) as exc:
        log.info("[FINOPS] budgets.describe_budgets unavailable: %s", exc)
        return []
