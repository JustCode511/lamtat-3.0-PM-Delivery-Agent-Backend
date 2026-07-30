"""
JSON file conversation store — implements ConversationStore for local dev.

Layout in data/conversations.json:
  { "<user_id>": { "<session_id>": {title, created_at, updated_at, messages:[...]} } }

Scoping conversations under user_id gives ownership for free: get_messages only
ever looks inside the requesting user's bucket. Mirrors DynamoConversationStore
so the local->AWS swap is config-only (APP_ENV).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from interfaces.conversation_store import ConversationStore

_TITLE_MAX = 60


class JsonConversationStore(ConversationStore):
    def __init__(self, path: str = "data/conversations.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def append(self, user_id, session_id, role, content, ui_hint=None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        data = self._load()
        convos = data.setdefault(user_id, {})
        convo = convos.get(session_id)
        if convo is None:
            convo = {"session_id": session_id, "title": None,
                     "created_at": now, "updated_at": now, "messages": []}
            convos[session_id] = convo
        # First user message becomes the conversation title.
        if role == "user" and not convo.get("title"):
            convo["title"] = (content.strip()[:_TITLE_MAX] or "New chat")
        convo["messages"].append(
            {"role": role, "content": content, "ui_hint": ui_hint or "", "created_at": now}
        )
        convo["updated_at"] = now
        self._save(data)

    def list_conversations(self, user_id) -> list[dict[str, Any]]:
        convos = self._load().get(user_id, {})
        items = [
            {
                "session_id": sid,
                "title": c.get("title") or "New chat",
                "updated_at": c.get("updated_at"),
                "message_count": len(c.get("messages", [])),
            }
            for sid, c in convos.items()
        ]
        items.sort(key=lambda x: x["updated_at"] or "", reverse=True)
        return items

    def get_messages(self, user_id, session_id) -> list[dict[str, Any]] | None:
        convo = self._load().get(user_id, {}).get(session_id)
        if not convo:
            return None
        return convo.get("messages", [])
