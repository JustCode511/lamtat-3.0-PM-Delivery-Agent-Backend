"""
Storage interface (a "port").

The agent reads/writes conversation history through this contract only.
Local uses JSON files; AWS uses DynamoDB. Swapped via APP_ENV.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class SessionStore(ABC):
    """Stores conversation history per session id."""

    @abstractmethod
    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        """Return the message list for a session, or [] if new."""
        raise NotImplementedError

    @abstractmethod
    def save_history(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Persist the full message list for a session."""
        raise NotImplementedError
