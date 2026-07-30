"""
DynamoDB conversation store — implements ConversationStore for AWS.

NOT used locally. Written now so the swap is config-only later (APP_ENV=aws).

Model (right-sized for a demo): one item per conversation, keyed by session_id,
holding the message list plus title/timestamps. A GSI on user_id lets us list a
user's conversations. Mirrors the JSON store exactly.

  Table  pm_conversations
    PK   session_id
    GSI  user_id-index  (PK: user_id)   — for list_conversations

For very long conversations at scale you'd switch to one-item-per-message; a
single item comfortably holds a demo's worth of turns (400 KB limit).
On Lambda, credentials come from the IAM role automatically.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from interfaces.conversation_store import ConversationStore

_TITLE_MAX = 60


class DynamoConversationStore(ConversationStore):
    def __init__(self, table_name: str = "pm_conversations", region: str | None = None) -> None:
        # region=None -> boto3 uses AWS_REGION (set automatically on Lambda).
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def append(self, user_id, session_id, role, content, ui_hint=None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        item = self.table.get_item(Key={"session_id": session_id}).get("Item")
        if not item:
            item = {"session_id": session_id, "user_id": user_id, "title": None,
                    "created_at": now, "updated_at": now, "messages": []}
        if role == "user" and not item.get("title"):
            item["title"] = (content.strip()[:_TITLE_MAX] or "New chat")
        item["messages"].append(
            {"role": role, "content": content, "ui_hint": ui_hint or "", "created_at": now}
        )
        item["updated_at"] = now
        item["user_id"] = user_id
        self.table.put_item(Item=item)

    def list_conversations(self, user_id) -> list[dict[str, Any]]:
        resp = self.table.query(
            IndexName="user_id-index",
            KeyConditionExpression=Key("user_id").eq(user_id),
        )
        items = [
            {
                "session_id": it["session_id"],
                "title": it.get("title") or "New chat",
                "updated_at": it.get("updated_at"),
                "message_count": len(it.get("messages", [])),
            }
            for it in resp.get("Items", [])
        ]
        items.sort(key=lambda x: x["updated_at"] or "", reverse=True)
        return items

    def get_messages(self, user_id, session_id) -> list[dict[str, Any]] | None:
        item = self.table.get_item(Key={"session_id": session_id}).get("Item")
        if not item or item.get("user_id") != user_id:
            return None
        return item.get("messages", [])
