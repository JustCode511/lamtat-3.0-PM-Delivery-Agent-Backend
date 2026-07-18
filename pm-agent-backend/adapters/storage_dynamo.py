"""
DynamoDB storage — implements SessionStore for AWS.

NOT used locally. Written now so the swap is config-only later.
Expects a table with partition key "session_id".
"""
from __future__ import annotations
import json
from typing import Any

import boto3

from interfaces.storage import SessionStore


class DynamoSessionStore(SessionStore):
    def __init__(self, table_name: str = "pm_sessions", region: str = "us-east-1") -> None:
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        resp = self.table.get_item(Key={"session_id": session_id})
        item = resp.get("Item")
        if not item:
            return []
        return json.loads(item.get("history", "[]"))

    def save_history(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self.table.put_item(
            Item={"session_id": session_id, "history": json.dumps(messages)}
        )
