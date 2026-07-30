"""
DynamoDB user store — implements UserStore for AWS.

NOT used locally. Written now so the swap is config-only later (APP_ENV=aws).
Expects a table with partition key "username".

create_user uses a conditional write (attribute_not_exists) so two concurrent
registrations for the same username can't both succeed — the second gets False.
On Lambda, credentials come from the IAM role automatically (no key needed).
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from interfaces.user_store import UserStore


class DynamoUserStore(UserStore):
    def __init__(self, table_name: str = "pm_users", region: str = "us-east-1") -> None:
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def get_user(self, username: str) -> dict[str, Any] | None:
        resp = self.table.get_item(Key={"username": username})
        return resp.get("Item")

    def create_user(self, username: str, password_hash: str, salt: str) -> bool:
        try:
            self.table.put_item(
                Item={
                    "username": username,
                    "password_hash": password_hash,
                    "salt": salt,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                ConditionExpression="attribute_not_exists(username)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False  # username already taken
            raise
