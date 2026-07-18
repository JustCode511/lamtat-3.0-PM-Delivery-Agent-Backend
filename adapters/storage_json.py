"""
JSON file storage — implements SessionStore for local development.

Stores each session as a JSON file under data/sessions/.
Uses pathlib so paths work identically on Windows and Mac.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

from interfaces.storage import SessionStore


class JsonSessionStore(SessionStore):
    def __init__(self, base_dir: str = "data/sessions") -> None:
        # Path() handles Windows backslashes vs Mac forward-slashes automatically
        self.base = Path(base_dir)
        self.base.mkdir(parents=True, exist_ok=True)

    def _file(self, session_id: str) -> Path:
        # sanitize so a session id can't escape the folder
        safe = "".join(c for c in session_id if c.isalnum() or c in ("-", "_"))
        return self.base / f"{safe}.json"

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        f = self._file(session_id)
        if not f.exists():
            return []
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def save_history(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        f = self._file(session_id)
        f.write_text(json.dumps(messages, indent=2), encoding="utf-8")
