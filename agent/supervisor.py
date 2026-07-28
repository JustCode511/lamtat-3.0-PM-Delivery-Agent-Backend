"""
Supervisor node — scope guard + broad intent classifier.

Only 5 real intents plus out_of_scope and default.
The 'query' intent covers ALL PM read/analysis requests —
the PM Query Agent handles the tool selection internally.
"""
from __future__ import annotations
import logging
from datetime import date
from agent.state import AgentState, extract_json, llm_generate
from interfaces.llm import LLMClient

log = logging.getLogger(__name__)

SUPERVISOR_PROMPT = """You are a scope guard and router for a PM Delivery Agent.
Today: {today}

STEP 1 — SCOPE CHECK:
This agent handles ONLY project management work: Jira projects, tickets, risks, milestones,
team workload, deliverables, status reports, Slack notifications, and PowerPoint reports.

Use "out_of_scope" if the user asks for:
- Writing or generating code, scripts, or programs
- Translating text, writing essays, creative writing, or homework
- News, weather, sports, personal questions, or anything unrelated to PM work

STEP 2 — CLASSIFY intent (pick exactly one):
- query      : ANY request to read, search, analyse, or report on PM/Jira data —
                status, risks, milestones, team workload, ticket search, project comparison,
                deliverables, "who is assigned to what", "show me tickets", "compare X vs Y",
                "flag risks", "generate a summary", "what's at risk", "status report", etc.
                NOTE: generating a report or summary for the user to READ is always "query" —
                even if the user says "flag", "generate", "show me", "give me a report".
- create_issue: user wants to CREATE a new Jira ticket, bug, story, task, or epic
- send_slack_notification: user EXPLICITLY says "send to Slack", "post to Slack",
                "notify the team on Slack", or "message the channel". Generating a report
                for the user to read in chat is NOT this intent. The user must explicitly
                request a Slack action.
- generate_ppt: user explicitly wants a PowerPoint / PPT / slide deck file
- out_of_scope: request is outside PM scope (see Step 1)
- default    : greetings, "what can you do?", or genuinely unclear requests

STEP 3 — EXTRACT project_key:
- Return exactly ONE value: a single Jira key (e.g. AABGFY26), "__ALL__", or null
- NEVER return comma-separated keys — if the user mentions multiple projects, return null
- If user says "all projects", "all", "every project", "portfolio" → return "__ALL__"
- If one specific project is clearly mentioned → return that key
- Otherwise → null (the query agent resolves it using tools)

Reply with JSON only — no other text:
{{"intent": "<query|create_issue|send_slack_notification|generate_ppt|out_of_scope|default>", "project_key": "<KEY|__ALL__|null>"}}"""


def make_supervisor_node(llm: LLMClient):
    async def supervisor_node(state: AgentState) -> AgentState:
        log.info("[SUPERVISOR] classifying: %r", state["user_message"][:100])

        history_block = ""
        if state["history"]:
            recent = state["history"][-10:]
            lines = "\n".join(f'{m["role"].upper()}: {m["content"][:200]}' for m in recent)
            history_block = f"\n\nRecent conversation:\n{lines}"

        today = date.today().isoformat()
        raw = await llm_generate(
            llm,
            SUPERVISOR_PROMPT.format(today=today),
            f"Current message: {state['user_message']}{history_block}",
        )
        parsed = extract_json(raw)
        intent = parsed.get("intent", "default")
        project_key = parsed.get("project_key") or None
        # Normalise: null strings → None
        if project_key in ("null", "none", ""):
            project_key = None
        log.info("[SUPERVISOR] → intent=%r  project_key=%r", intent, project_key)
        return {**state, "intent": intent, "project_key": project_key}

    return supervisor_node
