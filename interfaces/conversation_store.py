"""
Conversation store interface (a "port").

This is the permanent archive of user chats that powers the history sidebar —
separate from SessionStore (which is the agent's short-term working memory).
Every message is appended here, scoped to the user who sent it, so the UI can
list past conversations and replay any one of them.

Local uses a JSON file; AWS uses DynamoDB. Swapped via APP_ENV.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class ConversationStore(ABC):
    """Archives chat messages per user, grouped by conversation (session_id)."""

    @abstractmethod
    def append(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        ui_hint: str | None = None,
    ) -> None:
        """Append one message to a conversation, creating it (and its title,
        derived from the first user message) if it doesn't exist yet."""
        raise NotImplementedError

    @abstractmethod
    def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        """Return the user's conversations, newest first. Each item:
        {session_id, title, updated_at, message_count}."""
        raise NotImplementedError

    @abstractmethod
    def get_messages(self, user_id: str, session_id: str) -> list[dict[str, Any]] | None:
        """Return a conversation's messages, or None if it doesn't exist or
        isn't owned by this user. Each message: {role, content, ui_hint, created_at}."""
        raise NotImplementedError

    @abstractmethod
    def delete(self, user_id: str, session_id: str) -> bool:
        """Permanently delete a conversation. Returns True if it existed and was
        owned by this user (and is now gone), False otherwise. Ownership-scoped
        so a user can only ever delete their own chats."""
        raise NotImplementedError
