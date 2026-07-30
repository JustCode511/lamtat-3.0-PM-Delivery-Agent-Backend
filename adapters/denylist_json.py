"""
JSON file token denylist — implements TokenDenylist for local development.

Stores { jti: expires_at } in data/revoked_tokens.json. An entry whose
expires_at is already in the past is treated as not-revoked (the token has
expired on its own), and pruned on the next revoke() so the file stays small.

Mirrors DynamoTokenDenylist so the local->AWS swap is config-only (APP_ENV).
"""
from __future__ import annotations
import json
import time
from pathlib import Path


from interfaces.token_denylist import TokenDenylist


class JsonTokenDenylist(TokenDenylist):
    def __init__(self, path: str = "data/revoked_tokens.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, int]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict[str, int]) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def revoke(self, jti: str, expires_at: int | None = None) -> None:
        now = int(time.time())
        data = self._load()
        # Drop entries that have expired on their own, then add this one.
        data = {j: e for j, e in data.items() if not e or e > now}
        data[jti] = int(expires_at) if expires_at else 0
        self._save(data)

    def is_revoked(self, jti: str) -> bool:
        exp = self._load().get(jti)
        if exp is None:
            return False
        if exp and exp < int(time.time()):
            return False  # token would already be expired — decode_jwt rejects it anyway
        return True
