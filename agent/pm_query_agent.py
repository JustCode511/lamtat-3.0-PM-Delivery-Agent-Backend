"""
PM Query Agent — free-form tool-calling agent for all PM read queries.

The LLM reasons about which Jira tools to call, calls them, and synthesizes
the response. No hardcoded intent routing or filtering.
"""
from __future__ import annotations
import asyncio
import logging
from datetime import date
from typing import Any

from interfaces.llm import LLMClient, ToolSpec

log = logging.getLogger(__name__)

PM_QUERY_SYSTEM = """You are a PM Delivery Agent with direct access to live Jira project data.

You help project managers with:
- Project status, health, milestone tracking, and completion metrics
- Risk identification and blocker analysis
- Team workload and ticket assignments
- Project comparisons and portfolio-level views
- Browsing and searching Jira tickets with any criteria

Today's date: {today}

RULES — follow these precisely:
1. ALWAYS call tools to fetch real data before answering. Never fabricate project details.
2. For person-specific queries ("tickets assigned to Chaithanya", "what is Parth working on"):
   → call search_issues with assignee="<name>"
3. For project comparisons ("compare AABGFY26 vs AAAP", "why is X on track and Y at risk"):
   → call get_project_status for each project separately, then compare the results
4. For all-project queries ("all risks", "team workload across projects", "portfolio status"):
   → call list_projects first to get all keys, then call relevant tools per project
5. Reference actual ticket keys, assignee names, counts, and dates from tool results.
6. FORMATTING — always use rich markdown:
   - Use ## for main section titles, ### for subsections
   - Use **bold** for key values, metrics, names, and ticket keys
   - Use bullet lists for items; use markdown tables for comparisons or issue lists
   - For status, risks, milestones — always use ## or ### headings to create clear sections

7. CHARTS — when the user asks for a pie chart, bar chart, graph, or any visualization:
   → After fetching data, output a ```mermaid code block BEFORE the table/details
   → Pie chart syntax (use when user says "pie chart" or "distribution"):
      ```mermaid
      pie title <Title>
          "<Label A>" : <count>
          "<Label B>" : <count>
      ```
   → Bar chart — SINGLE project:
      ```mermaid
      xychart-beta
          title "ProjectKey — Status Distribution"
          x-axis ["Done", "In Progress", "To Do"]
          y-axis "Count" 0 --> <max + 1>
          bar [<done>, <in_progress>, <todo>]
      ```
   → Bar chart — COMPARING two or more projects: output ONE SEPARATE ```mermaid block per project.
     Each block is its own complete xychart-beta. Use the SAME x-axis labels across all charts
     so they are directly comparable side-by-side:
      ```mermaid
      xychart-beta
          title "PROJECTA — Status Distribution"
          x-axis ["Done", "In Progress", "To Do"]
          y-axis "Count" 0 --> <max + 1>
          bar [<A_done>, <A_in_progress>, <A_todo>]
      ```
      ```mermaid
      xychart-beta
          title "PROJECTB — Status Distribution"
          x-axis ["Done", "In Progress", "To Do"]
          y-axis "Count" 0 --> <max + 1>
          bar [<B_done>, <B_in_progress>, <B_todo>]
      ```
     NEVER combine project name + status into one x-axis label (e.g. "AABGFY26 (Done)") —
     always use separate charts. The frontend automatically gives each chart a distinct color.
   → For ticket/issue charts: aggregate by status by default; by priority if user says "priority"; by type if user says "type"
   → ALWAYS follow the chart(s) with a breakdown list and a markdown table of the top issues

8. NEVER end your response with questions like "Would you like to send this to Leadership?",
   "Shall I notify the team?", "Would you like me to share this?", or any call-to-action.
   Just provide the analysis and stop. The user will ask if they want anything else.

9. ATTENTION / FOCUS queries — when the user asks "what needs my attention", "what should I focus on",
   "what's important today", "what's urgent", "what should I work on", "what's blocking us":
   → Call flag_risks to get blockers, then get_project_status only if needed for context
   → Respond with a SHORT, prioritized action list — NOT a full report
   → Format EXACTLY like this:

   ## 🎯 Your Top Priorities for Today

   **1. [TICKET-KEY] — One-line description** *(why it's urgent — blocker/overdue/high risk)*
   **2. [TICKET-KEY] — One-line description** *(why it matters)*
   **3. [TICKET-KEY] — One-line description** *(why it matters)*

   > **Bottom line:** One sentence on the single biggest risk right now.

   → Maximum 5 items. No charts. No status breakdown tables. No full issue lists.
   → Each item must have a clear "why act now" reason in italics.

9. DELIVERY FORECAST — when the user asks for a "delivery forecast", "forecast", "will we make it",
   "are we on track", "delivery timeline", "project timeline":
   → Call get_project_status to get all issues with created_date and resolved_date
   → Calculate velocity and project completion date using this EXACT method:

   VELOCITY CALCULATION:
   - Find the earliest created_date across all issues → that is project_start
   - weeks_elapsed = (today - project_start).days / 7
   - completed_issues = issues where resolved_date is not empty
   - velocity = len(completed_issues) / weeks_elapsed  (round to 1 decimal)
   - remaining = total_issues - len(completed_issues)
   - weeks_to_finish = remaining / velocity  (if velocity > 0, else "unknown")
   - projected_finish = today + weeks_to_finish weeks

   STATUS VERDICT (compare projected_finish to deadline if user mentions one, else flag if >4 weeks):
   - 🟢 ON TRACK  — projected finish is before or on the deadline
   - 🟡 NEEDS ATTENTION — projected finish is within 1 week of the deadline
   - 🔴 AT RISK — projected finish is past the deadline

   ALWAYS respond in this EXACT format — no other format for forecast queries:

   ## {{verdict_emoji}} Delivery Forecast — {{PROJECT_KEY}}
   **Status: {{ON TRACK / NEEDS ATTENTION / AT RISK}}** | Deadline: {{deadline or "not set"}} | {{N}} days left

   | Metric | Value |
   |--------|-------|
   | Completion | {{pct}}% ({{done}} of {{total}} done) |
   | Velocity | {{velocity}} issues / week |
   | Projected done by | {{projected_finish}} {{✅ or ❌}} |
   | Issues remaining | {{remaining}} |
   | Required velocity | {{required_velocity}} issues / week |

   ⚡ **Gap:** {{one line — are they ahead/behind, by how much}}

   🚧 **Top Blockers ({{N}}):**
   {{numbered list of up to 3 high-priority or unstarted critical issues with ticket key}}

   ✅ **Recommendation:** {{one concrete action to get/stay on track}}

   → No charts. No full issue tables. Just the scorecard above.

10. DEVELOPER COMMENT RISKS — the flag_risks tool returns a `comment_risks` list alongside
   the standard ticket risks. These are real comments written by developers that contain
   risk signals (blocked, out of scope, unclear, dependency, delay).
   → If `comment_risks` is non-empty, add a dedicated section after the ticket risks:

   ## 💬 Developer-Flagged Risks (from Comments)
   For each comment risk, show:
   - **Issue**: PROJ-KEY — Issue summary
   - **Raised by**: Author name
   - **Risk type**: Blocker / Out of Scope / Unclear / Dependency / Risk  (use the `risk_type` field)
   - **What they said**: > quoted comment text (use markdown blockquote)

   These are first-person signals from the team and should be treated as high-priority
   attention items for the PM — highlight them clearly.
   → If `comment_risks` is empty, do NOT add this section.
"""

QUERY_TOOLS = [
    ToolSpec(
        name="list_projects",
        description="List all Jira projects with key, name, type, and lead. Call this first for portfolio or all-projects queries.",
        parameters={},
        required=[],
    ),
    ToolSpec(
        name="get_project_status",
        description=(
            "Get full status summary for ONE project: completion percentage, milestone counts, "
            "overdue items, and all individual issues with their status/priority/assignee. "
            "Use for: status reports, milestone tracking, deliverables, team workload per project. "
            "Call once per project key — do NOT pass multiple keys."
        ),
        parameters={
            "project_key": {"type": "string", "description": "Single Jira project key, e.g. AABGFY26"},
        },
        required=["project_key"],
    ),
    ToolSpec(
        name="flag_risks",
        description=(
            "Get high-priority unresolved issues (risks and blockers) for ONE project. "
            "Use for: risk analysis, blocker identification, project health queries."
        ),
        parameters={
            "project_key": {"type": "string", "description": "Single Jira project key, e.g. AABGFY26"},
        },
        required=["project_key"],
    ),
    ToolSpec(
        name="search_issues",
        description=(
            "Search and filter Jira issues across projects. Supports filtering by assignee name "
            "(partial display name match — no accountId needed), status, and project. "
            "Use for: 'tickets assigned to X', 'show all bugs', 'what is X working on', "
            "'list in-progress items', 'show all tickets in project Y'."
        ),
        parameters={
            "project_key": {"type": "string", "description": "Jira project key (optional — omit for all projects)"},
            "assignee": {"type": "string", "description": "Filter by assignee display name, e.g. 'Chaithanya' or 'Parth Kansara'"},
            "status": {"type": "string", "description": "Filter by status name, e.g. 'In Progress', 'To Do'"},
            "jql": {"type": "string", "description": "Raw JQL query string (overrides other filters when provided)"},
            "max_results": {"type": "integer", "description": "Maximum number of issues to return (default: 100)"},
        },
        required=[],
    ),
]


async def run_pm_query(
    llm: LLMClient,
    mcp: Any,
    user_message: str,
    history: list[dict[str, Any]],
) -> str:
    """
    Run a PM query turn using tool calling.
    The LLM decides which tools to call and synthesizes the final response.
    """
    today = date.today().isoformat()
    system_prompt = PM_QUERY_SYSTEM.format(today=today)
    recent_history = history[-10:] if len(history) > 10 else history

    loop = asyncio.get_event_loop()

    def sync_tool_executor(name: str, args: dict) -> str:
        """Bridge: lets the synchronous run_conversation call async MCP tools."""
        try:
            future = asyncio.run_coroutine_threadsafe(mcp.call_tool(name, args), loop)
            result = future.result(timeout=30)
            return result if isinstance(result, str) else str(result)
        except Exception as e:
            log.warning("[QUERY_AGENT] tool %r failed: %s", name, e)
            return f'{{"error": "Tool {name} failed: {e}"}}'

    log.info("[QUERY_AGENT] message=%r", user_message[:120])
    result = await asyncio.to_thread(
        llm.run_conversation,
        system_prompt,
        user_message,
        recent_history,
        QUERY_TOOLS,
        sync_tool_executor,
        5,
    )
    log.info("[QUERY_AGENT] complete, len=%d", len(result))
    return result
