"""
Long-term memory for the LAMTAT chat agents.

Two complementary memory layers:

  1. Rolling session summary — when a session grows past MAX_HISTORY messages,
     the older messages are summarised (via LLM) and stored alongside the session.
     On every subsequent turn the summary is injected as the first context pair,
     so the agent always has the full conversational arc — even 100 turns later.

  2. Cross-session user memory — after each turn a small set of durable facts
     is extracted and stored per user. Every new session starts with a
     "what I know about you" prefix, so the agent is never cold.

Storage (local, mirrors the JSON session store):
  data/session_summaries/<session_id>.json  →  {summary, covered_through}
  data/user_memory/<user_id>.json           →  {facts: [...], updated_at}

Both are best-effort: failures are logged but never propagate to the caller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Must match agent/core.py MAX_HISTORY_MESSAGES so we trim to the same window
MAX_HISTORY = 20
# Start summarising when total history grows beyond this
SUMMARY_TRIGGER = 24
# Maximum facts kept per user
MAX_USER_FACTS = 40

_SUMMARY_SYSTEM = """\
You are a concise conversation summariser.
Summarise the messages below in 4-6 bullet points.
Capture: questions asked, key findings, decisions made, project names, user preferences.
Each bullet: 1-2 sentences. No greetings. Facts only.
Return plain-text bullet points — no JSON, no headers."""

_MEMORY_SYSTEM = """\
Extract 1-3 memorable, durable facts from this single conversation exchange.
Good facts: project names being investigated, user preferences stated, decisions made, \
ongoing work threads worth carrying forward.
Skip transient details ("user asked a question") — only keep information useful in future sessions.
Return ONLY a JSON array of short strings (10–25 words each). Return [] if nothing is worth keeping.
Example: ["User manages project AABGFY26", "Prefers concise bullet-point summaries over prose"]"""


class LongTermMemory:
    """Manages rolling session summaries and cross-session user facts."""

    def __init__(self, llm: Any, base_dir: str = "data") -> None:
        self._llm = llm
        self._summaries_dir = Path(base_dir) / "session_summaries"
        self._memory_dir = Path(base_dir) / "user_memory"
        self._summaries_dir.mkdir(parents=True, exist_ok=True)
        self._memory_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Context building — called BEFORE invoking the agent graph
    # ------------------------------------------------------------------

    def build_context(
        self,
        session_id: str,
        full_history: list[dict[str, Any]],
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a list of synthetic message pairs to prepend to the live history.

        Order: user-memory first (broadest context), session summary second
        (most recent cross-window context), then the caller appends the live
        trimmed window.
        """
        prefix: list[dict[str, Any]] = []

        if user_id:
            facts = self._load_user_memory(user_id)
            if facts:
                bullets = "\n".join(f"• {f}" for f in facts)
                prefix += [
                    {
                        "role": "user",
                        "content": (
                            "[What I remember about you from our previous sessions]\n"
                            + bullets
                        ),
                    },
                    {
                        "role": "assistant",
                        "content": (
                            "I have your background context and will use it throughout "
                            "our conversation."
                        ),
                    },
                ]

        summary = self._load_summary(session_id)
        if summary:
            prefix += [
                {
                    "role": "user",
                    "content": "[Earlier in this conversation]\n" + summary,
                },
                {
                    "role": "assistant",
                    "content": (
                        "I have the summary of our earlier exchanges and will continue "
                        "from there."
                    ),
                },
            ]

        return prefix

    # ------------------------------------------------------------------
    # Post-turn updates — fire-and-forget after each agent response
    # ------------------------------------------------------------------

    async def update_after_turn(
        self,
        session_id: str,
        user_id: str | None,
        user_msg: str,
        assistant_reply: str,
        full_history: list[dict[str, Any]],
    ) -> None:
        """Update session summary + user memory. Best-effort, never raises."""
        try:
            await asyncio.gather(
                self._maybe_update_summary(session_id, full_history),
                self._maybe_update_user_memory(user_id, user_msg, assistant_reply),
            )
        except Exception as exc:
            log.warning("[MEMORY] update_after_turn error: %s", exc)

    # ------------------------------------------------------------------
    # Session summary — internal helpers
    # ------------------------------------------------------------------

    def _summary_path(self, session_id: str) -> Path:
        safe = "".join(c for c in session_id if c.isalnum() or c in ("-", "_"))
        return self._summaries_dir / f"{safe}.json"

    def _load_summary(self, session_id: str) -> str | None:
        p = self._summary_path(session_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("summary") or None
        except Exception:
            return None

    def _load_summary_meta(self, session_id: str) -> dict[str, Any]:
        p = self._summary_path(session_id)
        if not p.exists():
            return {"summary": None, "covered_through": 0}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {"summary": None, "covered_through": 0}

    def _save_summary(self, session_id: str, summary: str, covered_through: int) -> None:
        self._summary_path(session_id).write_text(
            json.dumps({"summary": summary, "covered_through": covered_through}, indent=2),
            encoding="utf-8",
        )

    async def _maybe_update_summary(
        self, session_id: str, full_history: list[dict[str, Any]]
    ) -> None:
        n = len(full_history)
        if n <= SUMMARY_TRIGGER:
            return

        meta = self._load_summary_meta(session_id)
        covered = meta.get("covered_through", 0)
        cutoff = n - MAX_HISTORY  # first index still in the live window

        if covered >= cutoff:
            return  # everything that can be summarised already is

        old_summary = meta.get("summary") or ""
        new_messages = full_history[covered:cutoff]

        msg_block = "\n".join(
            f'{m["role"].upper()}: {m["content"][:400]}' for m in new_messages
        )
        prompt = (
            (f"Existing summary:\n{old_summary}\n\nNew messages to incorporate:\n" if old_summary else "")
            + msg_block
        )

        try:
            from agent.state import llm_generate
            updated = await llm_generate(self._llm, _SUMMARY_SYSTEM, prompt)
            self._save_summary(session_id, updated.strip(), cutoff)
            log.info(
                "[MEMORY] session=%s  summary updated  covered %d→%d",
                session_id, covered, cutoff,
            )
        except Exception as exc:
            log.warning("[MEMORY] summary failed for session=%s: %s", session_id, exc)

    # ------------------------------------------------------------------
    # User memory — internal helpers
    # ------------------------------------------------------------------

    def _memory_path(self, user_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", user_id)
        return self._memory_dir / f"{safe}.json"

    def _load_user_memory(self, user_id: str) -> list[str]:
        p = self._memory_path(user_id)
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("facts", [])
        except Exception:
            return []

    def _save_user_memory(self, user_id: str, facts: list[str]) -> None:
        self._memory_path(user_id).write_text(
            json.dumps(
                {"facts": facts, "updated_at": datetime.now(timezone.utc).isoformat()},
                indent=2,
            ),
            encoding="utf-8",
        )

    async def _maybe_update_user_memory(
        self, user_id: str | None, user_msg: str, assistant_reply: str
    ) -> None:
        if not user_id:
            return
        prompt = (
            f"USER: {user_msg[:500]}\n"
            f"ASSISTANT: {assistant_reply[:500]}"
        )
        try:
            from agent.state import llm_generate
            raw = await llm_generate(self._llm, _MEMORY_SYSTEM, prompt)
            m = re.search(r"\[.*?\]", raw, re.DOTALL)
            if not m:
                return
            new_facts: list[str] = json.loads(m.group())
            if not new_facts or not isinstance(new_facts, list):
                return
            existing = self._load_user_memory(user_id)
            merged = (existing + new_facts)[-MAX_USER_FACTS:]
            self._save_user_memory(user_id, merged)
            log.info(
                "[MEMORY] user=%s  +%d facts  total=%d",
                user_id, len(new_facts), len(merged),
            )
        except Exception as exc:
            log.warning("[MEMORY] user memory failed for user=%s: %s", user_id, exc)
