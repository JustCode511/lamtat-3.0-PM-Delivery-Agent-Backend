"""
Token denylist interface (a "port").

JWTs are stateless, so a token stays valid until it expires — even after the
user signs out. To make voluntary sign-out actually end a session, /auth/logout
records the token's unique id (jti) here, and every request checks it.

Local uses a JSON file; AWS uses DynamoDB (with TTL so revoked entries are
auto-purged once the token would have expired anyway). Swapped via APP_ENV.
"""
from __future__ import annotations
from abc import ABC, abstractmethod


class TokenDenylist(ABC):
    """Records revoked token ids (jti) so a signed-out token can't be reused."""

    @abstractmethod
    def revoke(self, jti: str, expires_at: int | None = None) -> None:
        """Mark a token id as revoked. `expires_at` (unix seconds) lets the
        backing store auto-expire the entry once the token itself would."""
        raise NotImplementedError

    @abstractmethod
    def is_revoked(self, jti: str) -> bool:
        """True if this token id has been revoked (and hasn't itself expired)."""
        raise NotImplementedError
