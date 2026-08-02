"""
Mock stand-in for finops/aws_client.py.

Same function names/signatures/return shapes as aws_client.py, so
finops/services.py can swap between the two behind the live_data_enabled
setting (finops/repository.py) without any branching of its own. Exists
because AWS Cost Explorer bills $0.01 per API request regardless of
whether it returns data — every finops/routes.py call fires 6-10 Cost
Explorer requests, so iterating on the dashboard/UI against live AWS adds
up fast. This module is pure fabrication for that purpose; nothing here
is read from or written to a real AWS account.

Numbers are seeded off the current date so a given day's dashboard looks
stable across repeated calls/refreshes but drifts day to day, the same
way real cumulating spend would.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Any, Optional

_MOCK_ACCOUNT_ID = "123456789012"

_SERVICES = [
    ("Amazon Elastic Compute Cloud - Compute", 42.0, 6.0),
    ("Amazon Relational Database Service", 28.0, 4.0),
    ("Amazon Simple Storage Service", 9.0, 2.0),
    ("AWS Lambda", 6.0, 1.5),
    ("Amazon CloudWatch", 4.0, 1.0),
    ("Amazon Elastic Block Store", 5.0, 1.0),
    ("Amazon Virtual Private Cloud", 2.5, 0.5),
    ("Data Transfer", 3.5, 1.5),
]


def _rng_for(*parts: Any) -> random.Random:
    return random.Random("|".join(str(p) for p in parts))


def get_account_id() -> Optional[str]:
    return _MOCK_ACCOUNT_ID


def get_daily_cost(days: int = 30) -> list[dict[str, Any]]:
    end = date.today()
    base_daily = sum(mean for _, mean, _ in _SERVICES)
    points = []
    for offset in range(days, 0, -1):
        d = end - timedelta(days=offset - 1)
        rng = _rng_for("daily", d.isoformat())
        amount = max(0.0, rng.gauss(base_daily, base_daily * 0.08))
        # One seeded spike a few days back so anomaly detection has something to find.
        if offset == min(days, 6):
            amount *= 2.4
        points.append({"date": d.isoformat(), "amount": round(amount, 4)})
    return points


def get_cost_by_service(days: int = 30) -> list[dict[str, Any]]:
    rng = _rng_for("by_service", date.today().isoformat(), days)
    scale = days / 30.0
    return [
        {"service": name, "amount": round(max(0.0, rng.gauss(mean, stdev)) * scale, 4)}
        for name, mean, stdev in _SERVICES
    ]


def get_active_services(days: int = 30) -> list[str]:
    return sorted(name for name, _, _ in _SERVICES)


def get_cost_forecast(days_ahead: int = 7) -> Optional[float]:
    rng = _rng_for("forecast", date.today().isoformat(), days_ahead)
    base_daily = sum(mean for _, mean, _ in _SERVICES)
    return round(max(0.0, rng.gauss(base_daily, base_daily * 0.05)) * days_ahead, 4)


def get_native_anomalies(days: int = 30) -> list[dict[str, Any]]:
    end = date.today()
    anomaly_date = end - timedelta(days=min(days, 6) - 1)
    return [{
        "AnomalyId": f"mock-anomaly-{anomaly_date.isoformat()}",
        "AnomalyStartDate": anomaly_date.isoformat(),
        "AnomalyEndDate": anomaly_date.isoformat(),
        "RootCauses": [{"Service": "Amazon Elastic Compute Cloud - Compute"}],
        "AnomalyScore": {"CurrentScore": 87.0, "MaxScore": 92.0},
        "Impact": {
            "TotalExpectedSpend": 42.0,
            "TotalActualSpend": 100.8,
            "TotalImpact": 58.8,
        },
    }]


def has_anomaly_monitors() -> bool:
    return True


def get_compute_optimizer_recommendations() -> dict[str, Any]:
    return {
        "enrolled": True,
        "recommendations": [
            {
                "instanceArn": f"arn:aws:ec2:us-east-1:{_MOCK_ACCOUNT_ID}:instance/i-0mockoverprov1",
                "finding": "OVER_PROVISIONED",
                "currentInstanceType": "m5.xlarge",
                "recommendationOptions": [
                    {"instanceType": "m5.large", "estimatedMonthlySavings": {"value": 61.2, "currency": "USD"}},
                ],
            },
        ],
    }


def get_ec2_instances() -> list[dict[str, Any]]:
    return [
        {"id": "i-0mockoverprov1", "type": "m5.xlarge", "state": "running", "launch_time": "2026-06-01 12:00:00+00:00"},
        {"id": "i-0mockstopped1", "type": "t3.medium", "state": "stopped", "launch_time": "2026-05-10 09:30:00+00:00"},
    ]


def get_unattached_ebs_volumes() -> list[dict[str, Any]]:
    return [
        {"id": "vol-0mockunattach1", "size_gb": 100, "type": "gp3", "region": "us-east-1"},
    ]


def get_aws_budgets(account_id: Optional[str]) -> list[dict[str, Any]]:
    return [
        {"name": "Monthly-Overall-Budget", "limit": 1500.0, "actual_spend": 612.4},
    ]
