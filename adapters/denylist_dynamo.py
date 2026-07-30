"""
DynamoDB token denylist — implements TokenDenylist for AWS.

NOT used locally. Written now so the swap is config-only later (APP_ENV=aws).
Expects a table with partition key "jti" and TTL enabled on attribute "ttl".

The TTL means revoked entries self-delete once the token would have expired,
so the table never grows unbounded. Slightly-late TTL deletion is harmless:
an expired JWT is already rejected by decode_jwt regardless of the denylist.
On Lambda, credentials come from the IAM role automatically (no key needed).
"""
from __future__ import annotations

import boto3

from interfaces.token_denylist import TokenDenylist


class DynamoTokenDenylist(TokenDenylist):
    def __init__(self, table_name: str = "pm_revoked_tokens", region: str = "us-east-1") -> None:
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def revoke(self, jti: str, expires_at: int | None = None) -> None:
        item: dict = {"jti": jti}
        if expires_at:
            item["ttl"] = int(expires_at)  # DynamoDB TTL attribute — auto-expiry
        self.table.put_item(Item=item)

    def is_revoked(self, jti: str) -> bool:
        resp = self.table.get_item(Key={"jti": jti})
        return "Item" in resp
