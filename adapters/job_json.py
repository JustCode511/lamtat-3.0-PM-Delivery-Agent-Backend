"""
JSON job store — implements JobStore for local dev (APP_ENV=local).

Backs the async chat flow with a small on-disk file so the poll endpoint works
locally too. Mirrors the DynamoDB store's shape exactly.
"""
from __future__ import annotations
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from interfaces.job_store import JobStore

_DATA = Path(__file__).resolve().parent.parent / "data" / "jobs.json"
_LOCK = threading.Lock()


class JsonJobStore(JobStore):
    def __init__(self, path: Path = _DATA) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, indent=2))

    def create(self, job_id, user_id, session_id, message) -> None:
        with _LOCK:
            data = self._read()
            data[job_id] = {
                "job_id": job_id, "status": "pending", "user_id": user_id,
                "session_id": session_id, "message": message,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self._write(data)

    def get(self, job_id) -> dict[str, Any] | None:
        return self._read().get(job_id)

    def complete(self, job_id, reply, intent, reportable) -> None:
        with _LOCK:
            data = self._read()
            job = data.get(job_id, {"job_id": job_id})
            job.update({"status": "done", "reply": reply, "intent": intent,
                        "reportable": bool(reportable),
                        "updated_at": datetime.now(timezone.utc).isoformat()})
            data[job_id] = job
            self._write(data)

    def fail(self, job_id, error) -> None:
        with _LOCK:
            data = self._read()
            job = data.get(job_id, {"job_id": job_id})
            job.update({"status": "error", "error": str(error),
                        "updated_at": datetime.now(timezone.utc).isoformat()})
            data[job_id] = job
            self._write(data)
