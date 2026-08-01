"""
JSON-file-backed repository for the Cloud FinOps module.

Everything cost-related is live from AWS (see aws_client.py) — the only
thing worth persisting locally is the app-level target monthly budget,
since AWS Budgets requires console/API setup with its own IAM action
(budgets:ModifyBudget) that this account may not have. Storing a target
locally lets the dashboard compute "on track / at risk / over budget"
against real live spend without needing that extra permission.

Mirrors talent/repository.py's pattern: one JSON file, one RLock.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

_DATA_DIR = Path(__file__).parent.parent / "data"
_FILE = "finops_settings.json"
_LOCK = threading.RLock()


def _read() -> dict:
    path = _DATA_DIR / _FILE
    if not path.exists():
        return {}
    with _LOCK:
        return json.loads(path.read_text(encoding="utf-8"))


def _write(data: dict) -> None:
    path = _DATA_DIR / _FILE
    with _LOCK:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


class FinOpsSettingsRepository:
    def get_target_budget(self) -> Optional[float]:
        return _read().get("target_monthly_budget")

    def set_target_budget(self, amount: float) -> None:
        data = _read()
        data["target_monthly_budget"] = amount
        _write(data)
