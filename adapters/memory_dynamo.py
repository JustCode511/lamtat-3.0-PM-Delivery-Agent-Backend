"""
DynamoDB memory store — implements MemoryStore for AWS.

Durable long-term memory (unlike /tmp, which is wiped on cold starts). One table
holds both entity types, keyed by a prefixed partition key:
    summary#<session_id>  → {summary, covered_through}
    user#<user_id>        → {facts: [...], updated_at}

  Table  pm_memory
    PK   pk  (String)

region=None -> boto3 uses AWS_REGION (set automatically on Lambda).
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

import boto3

from interfaces.memory_store import MemoryStore


class DynamoMemoryStore(MemoryStore):
    def __init__(self, table_name: str = "pm_memory", region: str | None = None) -> None:
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def get_summary(self, session_id: str) -> dict[str, Any]:
        item = self.table.get_item(Key={"pk": f"summary#{session_id}"}).get("Item")
        if not item:
            return {"summary": None, "covered_through": 0}
        return {
            "summary": item.get("summary"),
            "covered_through": int(item.get("covered_through", 0)),
        }

    def save_summary(self, session_id: str, summary: str, covered_through: int) -> None:
        self.table.put_item(
            Item={
                "pk": f"summary#{session_id}",
                "summary": summary,
                "covered_through": int(covered_through),
            }
        )

    def get_user_facts(self, user_id: str) -> list[str]:
        item = self.table.get_item(Key={"pk": f"user#{user_id}"}).get("Item")
        return item.get("facts", []) if item else []

    def save_user_facts(self, user_id: str, facts: list[str]) -> None:
        self.table.put_item(
            Item={
                "pk": f"user#{user_id}",
                "facts": facts,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
