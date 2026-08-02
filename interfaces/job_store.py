"""
JobStore port — persists async chat jobs so a result can outlive the API
Gateway 30s client timeout.

Why this exists: a leadership report takes ~35s, but API Gateway (HTTP API) caps
the *client* response at a hard 30s. The Lambda invocation, however, keeps running
to completion after API Gateway gives up. So the async chat endpoint runs the agent
normally and writes the finished result HERE; the frontend polls get()/the result
endpoint until status flips to done/error. No self-invocation, no streaming
transport (both blocked on this account) — just a durable handoff.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class JobStore(ABC):
    @abstractmethod
    def create(self, job_id: str, user_id: str, session_id: str, message: str) -> None:
        """Record a new job as 'pending' (so a poll during the run sees it)."""

    @abstractmethod
    def get(self, job_id: str) -> dict[str, Any] | None:
        """Return the job record, or None if it doesn't exist / has expired."""

    @abstractmethod
    def complete(self, job_id: str, reply: str, intent: str, reportable: bool) -> None:
        """Mark the job 'done' with the finished reply."""

    @abstractmethod
    def fail(self, job_id: str, error: str) -> None:
        """Mark the job 'error' with the failure message."""
