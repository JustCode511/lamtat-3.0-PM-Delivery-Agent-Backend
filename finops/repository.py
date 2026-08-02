"""
JSON-file-backed repository for the Cloud FinOps module.

Persists two app-level settings: the target monthly budget (AWS Budgets
requires console/API setup with its own IAM action, budgets:ModifyBudget,
that this account may not have — storing a target locally lets the
dashboard compute "on track / at risk / over budget" without needing that
extra permission), and whether the module hits live AWS Cost Explorer/
Compute Optimizer/EC2 APIs or serves mock data — Cost Explorer bills
$0.01 per API request regardless of result, so mock is the default and
live is an opt-in per finops/mock_data.py.

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

    def get_live_data_enabled(self) -> bool:
        # Default False: Cost Explorer bills per API request, so a fresh
        # install must not start racking up charges before anyone opts in.
        return _read().get("live_data_enabled", False)

    def set_live_data_enabled(self, enabled: bool) -> None:
        data = _read()
        data["live_data_enabled"] = enabled
        _write(data)
