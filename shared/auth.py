"""
Auth core — password hashing (PBKDF2, stdlib) and JWT (PyJWT).

Design choices:
  - PBKDF2-HMAC-SHA256 via hashlib: zero third-party deps, so there is no
    compiled wheel to break inside the Lambda zip (the classic bcrypt trap).
    Each password gets a fresh 16-byte salt; verification is constant-time.
  - JWT signed HS256 with a secret from the JWT_SECRET env var (SSM on AWS,
    .env locally). Tokens carry the username in "sub" and expire after
    JWT_EXPIRY_HOURS.

Nothing here talks to storage — main.py wires these to the UserStore.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt  # PyJWT — pure Python, Lambda-safe

# --- Password hashing -------------------------------------------------------
_PBKDF2_ALGO = "sha256"
_PBKDF2_ITERATIONS = 200_000  # OWASP-recommended floor for PBKDF2-HMAC-SHA256
_SALT_BYTES = 16


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Hash a password with PBKDF2. Returns (hash_b64, salt_b64) for storage."""
    if salt is None:
        salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return base64.b64encode(dk).decode("ascii"), base64.b64encode(salt).decode("ascii")


def verify_password(password: str, hash_b64: str, salt_b64: str) -> bool:
    """Constant-time check of a password against a stored hash + salt."""
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac(_PBKDF2_ALGO, password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk, expected)


# --- JWT --------------------------------------------------------------------
_JWT_ALGO = "HS256"


def _secret() -> str:
    # Read at call time (not import) so tests/Lambda can set it after import.
    return os.getenv("JWT_SECRET", "dev-secret-change-me")


def _expiry_hours() -> int:
    try:
        return int(os.getenv("JWT_EXPIRY_HOURS", "24"))
    except ValueError:
        return 24


def create_jwt(username: str) -> str:
    """Issue a signed token for a username. Includes a unique `jti` so the
    token can be individually revoked on sign-out."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + timedelta(hours=_expiry_hours()),
    }
    return jwt.encode(payload, _secret(), algorithm=_JWT_ALGO)


def decode_jwt(token: str) -> dict[str, Any]:
    """Verify signature + expiry and return the claims. Raises on invalid/expired."""
    return jwt.decode(token, _secret(), algorithms=[_JWT_ALGO])
