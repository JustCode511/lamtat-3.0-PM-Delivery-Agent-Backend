"""
Service layer for the Cloud FinOps module.

Pulls live data from finops/aws_client.py and turns it into the
dashboard/anomaly/rightsizing/budget shapes defined in finops/models.py.
All numbers here are derived from real AWS API responses — nothing is
fabricated. When AWS can't supply a signal (no anomaly monitors, Compute
Optimizer not enrolled, insufficient forecast history) the result says so
explicitly instead of inventing a number.

Each "get_*" method is independently usable (used by the per-tab GET
endpoints) and fetches its own raw data sequentially. get_dashboard(), which
needs nearly everything at once, instead fires all the independent boto3
calls concurrently via a thread pool — Cost Explorer/Compute Optimizer/EC2
calls are all blocking I/O with no shared state, so this cuts total load
time from the sum of ~10 round-trips to roughly the slowest one.
"""
from __future__ import annotations

import logging
import os
import statistics
import uuid
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Optional

from finops import aws_client
from finops.models import (
    Anomaly,
    AnomalyResult,
    BudgetStatus,
    CostSummary,
    DailyCostPoint,
    DashboardStats,
    RightsizingRecommendation,
    RightsizingResult,
    ServiceCost,
    SlackDigestResult,
)
from finops.repository import FinOpsSettingsRepository

log = logging.getLogger(__name__)

# Rough public on-demand EBS pricing (USD / GB-month, us-east-1-ish) — used only
# to size the savings estimate for unattached volumes, a real and common waste
# pattern that doesn't require Compute Optimizer enrollment to detect.
_EBS_PRICE_PER_GB = {
    "gp3": 0.08, "gp2": 0.10, "io1": 0.125, "io2": 0.125, "st1": 0.045, "sc1": 0.015,
}


def _clamp_noise(amount: float) -> float:
    """Zero out sub-half-cent float noise (AWS credits/rounding artifacts)."""
    return 0.0 if abs(amount) < 0.005 else amount


class FinOpsService:
    def __init__(self) -> None:
        self.settings_repo = FinOpsSettingsRepository()

    # ------------------------------------------------------------------
    # Cost summary
    # ------------------------------------------------------------------

    def _build_cost_summary(
        self,
        account_id: Optional[str],
        daily_raw: list[dict],
        by_service_raw: list[dict],
        active_services: Optional[list[str]] = None,
    ) -> CostSummary:
        # AWS occasionally reports fractions-of-a-cent negative amounts (credits/
        # rounding) that would otherwise render as a confusing "-$0.00" — clamp
        # noise below half a cent to zero.
        daily = [{"date": p["date"], "amount": _clamp_noise(p["amount"])} for p in daily_raw]

        total_30d = sum(p["amount"] for p in daily)
        daily_avg = round(total_30d / len(daily), 2) if daily else 0.0

        month_start = date.today().replace(day=1).isoformat()
        total_mtd = sum(p["amount"] for p in daily if p["date"] >= month_start)

        # Clamp the same sub-half-cent noise per service, and base percentages
        # only on positive spend — a stray credit line item (negative amount)
        # must not shrink/blow up every other service's share of the total.
        by_service_clamped = [{"service": s["service"], "amount": _clamp_noise(s["amount"])} for s in by_service_raw]
        total_service = sum(a["amount"] for a in by_service_clamped if a["amount"] > 0) or 1.0
        by_service = sorted(
            (
                ServiceCost(
                    service=s["service"],
                    amount=round(s["amount"], 4),
                    pct_of_total=round(max(s["amount"], 0) / total_service * 100, 1),
                )
                for s in by_service_clamped
            ),
            key=lambda s: s.amount,
            reverse=True,
        )

        return CostSummary(
            account_id=account_id,
            period_start=daily[0]["date"] if daily else date.today().isoformat(),
            period_end=daily[-1]["date"] if daily else date.today().isoformat(),
            total_30d=round(total_30d, 4),
            total_mtd=round(total_mtd, 4),
            daily_avg=daily_avg,
            by_service=by_service,
            active_services=sorted(active_services) if active_services is not None else [s.service for s in by_service],
            daily_trend=[DailyCostPoint(date=p["date"], amount=round(p["amount"], 4)) for p in daily],
        )

    def get_cost_summary(self, days: int = 30) -> CostSummary:
        account_id = aws_client.get_account_id()
        daily_raw = aws_client.get_daily_cost(days)
        by_service_raw = aws_client.get_cost_by_service(days)
        active_services = aws_client.get_active_services(days)
        return self._build_cost_summary(account_id, daily_raw, by_service_raw, active_services)

    # ------------------------------------------------------------------
    # Anomaly detection
    # ------------------------------------------------------------------

    def _build_anomalies(
        self, cost_summary: CostSummary, native_raw: list[dict], monitors_configured: bool
    ) -> AnomalyResult:
        anomalies: list[Anomaly] = []

        # 1) AWS-native Cost Anomaly Detection (only populated if the account
        #    has Anomaly Monitors configured with enough baseline history).
        for a in native_raw:
            impact = a.get("Impact", {})
            anomalies.append(Anomaly(
                id=a.get("AnomalyId", str(uuid.uuid4())),
                date=a.get("AnomalyStartDate", "")[:10],
                scope=(a.get("RootCauses", [{}])[0].get("Service") if a.get("RootCauses") else None) or "Total spend",
                expected_amount=float(impact.get("TotalExpectedSpend", 0.0)),
                actual_amount=float(impact.get("TotalActualSpend", 0.0)),
                delta_amount=float(impact.get("TotalImpact", 0.0)),
                delta_pct=float(a.get("AnomalyScore", {}).get("CurrentScore", 0.0)),
                severity="critical" if impact.get("TotalImpact", 0) > 50 else "high",
                root_cause=f"AWS Cost Anomaly Detection flagged unexpected spend in {a.get('RootCauses', [{}])[0].get('Service', 'the account') if a.get('RootCauses') else 'the account'}.",
                recommended_action="Review the linked service's usage in Cost Explorer for the anomaly window and confirm it's expected.",
                source="aws_anomaly_detection",
            ))

        # 2) Statistical fallback over real daily totals — a day-over-day
        #    z-score check, so anomalies still surface even when no AWS
        #    Anomaly Monitor is configured. Needs enough variance to mean
        #    anything; flat/near-zero history correctly yields nothing.
        trend = cost_summary.daily_trend
        amounts = [p.amount for p in trend]
        if len(amounts) >= 5 and statistics.pstdev(amounts) > 0.01:
            mean = statistics.mean(amounts)
            stdev = statistics.pstdev(amounts)
            for p in trend:
                z = (p.amount - mean) / stdev
                if z > 2.5 and p.amount > mean * 1.5:
                    anomalies.append(Anomaly(
                        id=f"stat-{p.date}",
                        date=p.date,
                        scope="Total spend",
                        expected_amount=round(mean, 4),
                        actual_amount=round(p.amount, 4),
                        delta_amount=round(p.amount - mean, 4),
                        delta_pct=round((p.amount - mean) / mean * 100, 1) if mean else 0.0,
                        severity="critical" if z > 4 else "high",
                        root_cause=(
                            f"Spend on {p.date} (${p.amount:.2f}) is {z:.1f} standard deviations above the "
                            f"{len(amounts)}-day average (${mean:.2f}) — a statistically significant spike."
                        ),
                        recommended_action="Check Cost Explorer's per-service breakdown for this date to identify which service drove the spike, and confirm it wasn't an unintended deployment or leftover resource.",
                        source="statistical",
                    ))

        note = None
        if not anomalies:
            note = (
                "No anomalies detected in the current cost data."
                if monitors_configured or len(amounts) >= 5
                else "Not enough cost history yet for anomaly detection to be meaningful."
            )

        return AnomalyResult(anomalies=anomalies, monitors_configured=monitors_configured, note=note)

    def get_anomalies(self, cost_summary: Optional[CostSummary] = None) -> AnomalyResult:
        cost_summary = cost_summary or self.get_cost_summary()
        monitors_configured = aws_client.has_anomaly_monitors()
        native_raw = aws_client.get_native_anomalies()
        return self._build_anomalies(cost_summary, native_raw, monitors_configured)

    # ------------------------------------------------------------------
    # Rightsizing
    # ------------------------------------------------------------------

    def _build_rightsizing(
        self, co: dict[str, Any], instances: list[dict], volumes: list[dict]
    ) -> RightsizingResult:
        recommendations: list[RightsizingRecommendation] = []

        for r in co["recommendations"]:
            finding = r.get("finding", "")
            if finding in ("OPTIMIZED", ""):
                continue
            options = r.get("recommendationOptions", [])
            best = options[0] if options else {}
            recommendations.append(RightsizingRecommendation(
                resource_id=r.get("instanceArn", "").split("/")[-1] or r.get("instanceArn", "unknown"),
                resource_type="EC2 Instance",
                region=r.get("instanceArn", "").split(":")[3] if ":" in r.get("instanceArn", "") else "",
                finding=finding,
                current_spec=r.get("currentInstanceType", "unknown"),
                recommended_spec=best.get("instanceType", "unknown"),
                estimated_monthly_savings=round(float(best.get("estimatedMonthlySavings", {}).get("value", 0.0)), 2),
                reason=f"Compute Optimizer finding: {finding.replace('_', ' ').title()}",
            ))

        stopped = [i for i in instances if i["state"] == "stopped"]
        for i in stopped:
            recommendations.append(RightsizingRecommendation(
                resource_id=i["id"],
                resource_type="EC2 Instance",
                region="",
                finding="STOPPED_IDLE",
                current_spec=i["type"],
                recommended_spec="terminate if unused",
                estimated_monthly_savings=0.0,
                reason="Instance is stopped but not terminated — any attached EBS volumes are still billed. Terminate if no longer needed.",
            ))

        for v in volumes:
            price = _EBS_PRICE_PER_GB.get(v["type"], 0.08)
            savings = round((v.get("size_gb") or 0) * price, 2)
            recommendations.append(RightsizingRecommendation(
                resource_id=v["id"],
                resource_type="EBS Volume",
                region=v["region"],
                finding="UNATTACHED",
                current_spec=f"{v['size_gb']}GB {v['type']}",
                recommended_spec="delete or snapshot + delete",
                estimated_monthly_savings=savings,
                reason="Volume is unattached to any instance and still incurring storage charges.",
            ))

        recommendations.sort(key=lambda r: r.estimated_monthly_savings, reverse=True)
        total_savings = round(sum(r.estimated_monthly_savings for r in recommendations), 2)

        note = None
        if not co["enrolled"]:
            note = (
                "This AWS account is not enrolled in Compute Optimizer, so AI-powered EC2 rightsizing "
                "recommendations aren't available. Idle-instance and unattached-volume checks below run "
                "independently and don't require enrollment."
            )
        if not instances and not volumes and co["enrolled"] and not recommendations:
            note = "No EC2 instances or EBS volumes found in this account/region — nothing to rightsize."

        return RightsizingResult(
            compute_optimizer_enrolled=co["enrolled"],
            ec2_instance_count=len(instances),
            recommendations=recommendations,
            total_estimated_monthly_savings=total_savings,
            note=note,
        )

    def get_rightsizing(self) -> RightsizingResult:
        co = aws_client.get_compute_optimizer_recommendations()
        instances = aws_client.get_ec2_instances()
        volumes = aws_client.get_unattached_ebs_volumes()
        return self._build_rightsizing(co, instances, volumes)

    # ------------------------------------------------------------------
    # Budget & forecast
    # ------------------------------------------------------------------

    def _build_budget_status(
        self, cost_summary: CostSummary, forecast: Optional[float], aws_budgets: list[dict]
    ) -> BudgetStatus:
        target = self.settings_repo.get_target_budget()

        today = date.today()
        days_in_month = monthrange(today.year, today.month)[1]
        days_elapsed = today.day
        days_remaining = days_in_month - days_elapsed

        forecast_source = "aws_forecast"
        if forecast is None and days_elapsed > 0:
            projected_daily_avg = cost_summary.total_mtd / days_elapsed
            forecast = round(projected_daily_avg * days_remaining, 4)
            forecast_source = "linear_projection"
        forecast_month_end = round((cost_summary.total_mtd + forecast), 4) if forecast is not None else None
        if forecast is None:
            forecast_source = "none"

        pct_used = round(cost_summary.total_mtd / target * 100, 1) if target else None

        status = "no_budget_set"
        if target:
            if cost_summary.total_mtd > target:
                status = "over_budget"
            elif forecast_month_end is not None and forecast_month_end > target:
                status = "at_risk"
            elif pct_used is not None and pct_used > 90:
                status = "at_risk"
            else:
                status = "on_track"

        return BudgetStatus(
            target_monthly_budget=target,
            mtd_spend=cost_summary.total_mtd,
            forecast_month_end=forecast_month_end,
            forecast_source=forecast_source,
            pct_used=pct_used,
            status=status,
            aws_budgets=aws_budgets,
        )

    def get_budget_status(self, cost_summary: Optional[CostSummary] = None) -> BudgetStatus:
        cost_summary = cost_summary or self.get_cost_summary()
        days_remaining = monthrange(date.today().year, date.today().month)[1] - date.today().day
        forecast = aws_client.get_cost_forecast(days_ahead=max(days_remaining, 1))
        aws_budgets = aws_client.get_aws_budgets(cost_summary.account_id)
        return self._build_budget_status(cost_summary, forecast, aws_budgets)

    def set_target_budget(self, amount: float) -> BudgetStatus:
        self.settings_repo.set_target_budget(amount)
        return self.get_budget_status()

    # ------------------------------------------------------------------
    # Dashboard — fetches everything concurrently, then reuses the same
    # builders as the individual get_* methods so there's one source of
    # truth for how raw AWS data becomes each panel's shape.
    # ------------------------------------------------------------------

    def get_dashboard(self, days: int = 30) -> DashboardStats:
        today = date.today()
        days_remaining = monthrange(today.year, today.month)[1] - today.day

        with ThreadPoolExecutor(max_workers=10) as pool:
            f_account = pool.submit(aws_client.get_account_id)
            f_daily = pool.submit(aws_client.get_daily_cost, days)
            f_by_service = pool.submit(aws_client.get_cost_by_service, days)
            f_active_services = pool.submit(aws_client.get_active_services, days)
            f_forecast = pool.submit(aws_client.get_cost_forecast, max(days_remaining, 1))
            f_monitors = pool.submit(aws_client.has_anomaly_monitors)
            f_native_anom = pool.submit(aws_client.get_native_anomalies, days)
            f_co = pool.submit(aws_client.get_compute_optimizer_recommendations)
            f_instances = pool.submit(aws_client.get_ec2_instances)
            f_volumes = pool.submit(aws_client.get_unattached_ebs_volumes)

            account_id = f_account.result()
            daily_raw = f_daily.result()
            by_service_raw = f_by_service.result()
            active_services = f_active_services.result()
            forecast = f_forecast.result()
            monitors_configured = f_monitors.result()
            native_anom_raw = f_native_anom.result()
            co = f_co.result()
            instances = f_instances.result()
            volumes = f_volumes.result()

            # Needs account_id, so it's fired once that's ready, in a second
            # (still concurrent) wave alongside nothing else outstanding.
            aws_budgets = pool.submit(aws_client.get_aws_budgets, account_id).result()

        cost_summary = self._build_cost_summary(account_id, daily_raw, by_service_raw, active_services)
        anomalies = self._build_anomalies(cost_summary, native_anom_raw, monitors_configured)
        rightsizing = self._build_rightsizing(co, instances, volumes)
        budget = self._build_budget_status(cost_summary, forecast, aws_budgets)

        headline = (
            f"${cost_summary.total_mtd:.2f} spent month-to-date across "
            f"{len(cost_summary.by_service)} service(s). "
            f"{len(anomalies.anomalies)} anomal{'y' if len(anomalies.anomalies) == 1 else 'ies'} flagged, "
            f"${rightsizing.total_estimated_monthly_savings:.2f}/mo in potential savings identified."
        )

        return DashboardStats(
            cost_summary=cost_summary,
            anomalies=anomalies,
            rightsizing=rightsizing,
            budget=budget,
            potential_savings_total=rightsizing.total_estimated_monthly_savings,
            headline=headline,
        )

    # ------------------------------------------------------------------
    # Slack digest (reuses the same HITL-style Slack connector as the PM module)
    # ------------------------------------------------------------------

    def build_slack_digest_markdown(self, stats: DashboardStats) -> str:
        lines = [
            "## Cloud FinOps Digest",
            f"**Account:** `{stats.cost_summary.account_id or 'unknown'}`",
            f"**Period:** {stats.cost_summary.period_start} to {stats.cost_summary.period_end}",
            "",
            stats.headline,
            "",
            "### Spend by service (30d)",
        ]
        for s in stats.cost_summary.by_service[:8]:
            lines.append(f"- **{s.service}**: ${s.amount:.2f} ({s.pct_of_total:.1f}%)")
        if not stats.cost_summary.by_service:
            lines.append("- _No service-level spend in this window._")

        lines.append("")
        lines.append("### Anomalies")
        if stats.anomalies.anomalies:
            for a in stats.anomalies.anomalies[:5]:
                lines.append(f"- **{a.date} — {a.scope}**: ${a.actual_amount:.2f} (expected ${a.expected_amount:.2f}). {a.root_cause}")
        else:
            lines.append(f"- {stats.anomalies.note or 'None detected.'}")

        lines.append("")
        lines.append("### Rightsizing opportunities")
        if stats.rightsizing.recommendations:
            for r in stats.rightsizing.recommendations[:5]:
                lines.append(f"- **{r.resource_id}** ({r.resource_type}): {r.reason} — est. **${r.estimated_monthly_savings:.2f}/mo**")
            lines.append(f"- **Total potential savings: ${stats.rightsizing.total_estimated_monthly_savings:.2f}/mo**")
        else:
            lines.append(f"- {stats.rightsizing.note or 'None found.'}")

        lines.append("")
        lines.append("### Budget")
        if stats.budget.target_monthly_budget:
            lines.append(
                f"- Target: ${stats.budget.target_monthly_budget:.2f}/mo · MTD: ${stats.budget.mtd_spend:.2f} "
                f"· Forecast month-end: ${stats.budget.forecast_month_end or 0:.2f} · Status: **{stats.budget.status.replace('_', ' ')}**"
            )
        else:
            lines.append("- No target budget configured.")

        return "\n".join(lines)

    def send_slack_digest(self, channel_id: Optional[str] = None) -> SlackDigestResult:
        from mcp_servers.pm_server.slack_client import post_to_channel

        stats = self.get_dashboard()
        text = self.build_slack_digest_markdown(stats)
        channel = channel_id or os.getenv("FINOPS_SLACK_CHANNEL_ID") or os.getenv("SLACK_CHANNEL_ID", "")
        if not channel:
            return SlackDigestResult(sent=False, note="No Slack channel configured (set FINOPS_SLACK_CHANNEL_ID or SLACK_CHANNEL_ID).")

        result = post_to_channel(channel, text)
        if result.get("ok"):
            return SlackDigestResult(sent=True)
        return SlackDigestResult(sent=False, error=result.get("error") or result.get("note") or "unknown error")
