"""
JSON file user store — implements UserStore for local development.

Stores all credential records in a single JSON file (data/users.json) as
{ username: {password_hash, salt, created_at} }. Passwords are never stored
here — only the salted PBKDF2 hash produced by shared.auth.

Mirrors DynamoUserStore so the local->AWS swap is config-only (APP_ENV).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from interfaces.user_store import UserStore


class JsonUserStore(UserStore):
    def __init__(self, path: str = "data/users.json") -> None:
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

    def get_user(self, username: str) -> dict[str, Any] | None:
        return self._load().get(username)

    def create_user(self, username: str, password_hash: str, salt: str) -> bool:
        data = self._load()
        if username in data:
            return False
        data[username] = {
            "username": username,
            "password_hash": password_hash,
            "salt": salt,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save(data)
        return True
