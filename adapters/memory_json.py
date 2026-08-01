"""
JSON file memory store — implements MemoryStore for local development.

Keeps the exact on-disk layout LongTermMemory used before:
  data/session_summaries/<session_id>.json  → {summary, covered_through}
  data/user_memory/<user_id>.json           → {facts: [...], updated_at}

Mirrors DynamoMemoryStore so the local->AWS swap is config-only (APP_ENV).
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interfaces.memory_store import MemoryStore


class JsonMemoryStore(MemoryStore):
    def __init__(self, base_dir: str = "data") -> None:
        self._summaries_dir = Path(base_dir) / "session_summaries"
        self._memory_dir = Path(base_dir) / "user_memory"
        self._summaries_dir.mkdir(parents=True, exist_ok=True)
        self._memory_dir.mkdir(parents=True, exist_ok=True)

    def _summary_path(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in ("-", "_"))
        return self._summaries_dir / f"{safe}.json"

    def _memory_path(self, user_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", user_id)
        return self._memory_dir / f"{safe}.json"

    def get_summary(self, session_id: str) -> dict[str, Any]:
        p = self._summary_path(session_id)
        if not p.exists():
            return {"summary": None, "covered_through": 0}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"summary": None, "covered_through": 0}

    def save_summary(self, session_id: str, summary: str, covered_through: int) -> None:
        self._summary_path(session_id).write_text(
            json.dumps({"summary": summary, "covered_through": covered_through}, indent=2),
            encoding="utf-8",
        )

    def get_user_facts(self, user_id: str) -> list[str]:
        p = self._memory_path(user_id)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("facts", [])
        except (json.JSONDecodeError, OSError):
            return []

    def save_user_facts(self, user_id: str, facts: list[str]) -> None:
        self._memory_path(user_id).write_text(
            json.dumps(
                {"facts": facts, "updated_at": datetime.now(timezone.utc).isoformat()},
                indent=2,
            ),
            encoding="utf-8",
        )
