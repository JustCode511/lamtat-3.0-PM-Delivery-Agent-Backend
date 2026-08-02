"""
DynamoDB job store — implements JobStore for AWS.

  Table  pm_jobs
    PK   job_id
    TTL  ttl   (jobs self-expire ~1h after creation)

On Lambda, credentials come from the IAM role automatically. Mirrors the JSON
store used locally.
"""
from __future__ import annotations
import time
from datetime import datetime, timezone
from typing import Any

import boto3

from interfaces.job_store import JobStore

_TTL_SECONDS = 3600  # jobs auto-expire after 1 hour


class DynamoJobStore(JobStore):
    def __init__(self, table_name: str = "pm_jobs", region: str | None = None) -> None:
        # region=None -> boto3 uses AWS_REGION (set automatically on Lambda).
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def create(self, job_id, user_id, session_id, message) -> None:
        self.table.put_item(Item={
            "job_id": job_id,
            "status": "pending",
            "user_id": user_id,
            "session_id": session_id,
            "message": message,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ttl": int(time.time()) + _TTL_SECONDS,
        })

    def get(self, job_id) -> dict[str, Any] | None:
        return self.table.get_item(Key={"job_id": job_id}).get("Item")

    def complete(self, job_id, reply, intent, reportable) -> None:
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s, reply = :r, intent = :i, reportable = :rp, updated_at = :u",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":s": "done", ":r": reply, ":i": intent, ":rp": bool(reportable),
                ":u": datetime.now(timezone.utc).isoformat(),
            },
        )

    def fail(self, job_id, error) -> None:
        self.table.update_item(
            Key={"job_id": job_id},
            UpdateExpression="SET #s = :s, #e = :e, updated_at = :u",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":s": "error", ":e": str(error),
                ":u": datetime.now(timezone.utc).isoformat(),
            },
        )
