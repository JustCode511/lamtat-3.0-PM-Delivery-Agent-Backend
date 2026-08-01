"""
Memory store interface (a "port").

Backs LongTermMemory's two durable layers:
  - session summary  (per session_id): {summary, covered_through}
  - user facts        (per user_id):    [fact, fact, ...]

Local uses JSON files; AWS uses DynamoDB. Swapped via APP_ENV — same pattern as
SessionStore / ConversationStore. This makes long-term memory actually durable
on Lambda (its filesystem is read-only + ephemeral).
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class MemoryStore(ABC):
    """Durable storage for rolling session summaries and cross-session user facts."""

    @abstractmethod
    def get_summary(self, session_id: str) -> dict[str, Any]:
        """Return {"summary": str|None, "covered_through": int}. Defaults if absent."""
        raise NotImplementedError

    @abstractmethod
    def save_summary(self, session_id: str, summary: str, covered_through: int) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_user_facts(self, user_id: str) -> list[str]:
        """Return the stored fact list for a user, or [] if none."""
        raise NotImplementedError

    @abstractmethod
    def save_user_facts(self, user_id: str, facts: list[str]) -> None:
        raise NotImplementedError
