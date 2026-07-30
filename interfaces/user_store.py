"""
User store interface (a "port").

Holds the credential records used by JWT auth: one record per username with a
salted password hash (never a plaintext password). Local uses a JSON file;
AWS uses DynamoDB. Swapped via APP_ENV — same contract as SessionStore.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class UserStore(ABC):
    """Stores credential records keyed by username."""

    @abstractmethod
    def get_user(self, username: str) -> dict[str, Any] | None:
        """Return the user record, or None if the username does not exist.

        Record shape: {"username", "password_hash", "salt", "created_at"}.
        """
        raise NotImplementedError

    @abstractmethod
    def create_user(self, username: str, password_hash: str, salt: str) -> bool:
        """Create a new user. Return False if the username already exists
        (so registration can reject duplicates atomically)."""
        raise NotImplementedError
