"""
Graph wiring — assembles the LangGraph supervisor graph.

Flow: START → supervisor → specialized node → aggregator → END

Nodes:
  query      — PM Query Agent (tool-calling, handles all read/analysis)
  create_issue, generate_ppt, send_slack_notification — action nodes
  out_of_scope — polite rejection (no LLM)
  clarify_project — asks which project (used only when PPT needs one)
  default — greetings / help
"""
from __future__ import annotations
import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from agent.aggregator import make_aggregator_node
from agent.nodes import make_nodes
from agent.state import AgentState
from agent.supervisor import make_supervisor_node
from interfaces.llm import LLMClient

log = logging.getLogger(__name__)

SPECIALIZED_NODES = (
    "query",
    "create_issue",
    "generate_ppt",
    "send_slack_notification",
    "out_of_scope",
    "clarify_project",
    "default",
)

# Only PPT strictly requires a project key upfront
_NEEDS_PROJECT = frozenset({"generate_ppt"})


def build_graph(llm: LLMClient, mcp: Any) -> Any:
    supervisor_node = make_supervisor_node(llm)
    aggregator_node = make_aggregator_node(llm)
    nodes = make_nodes(llm, mcp)

    def route(state: AgentState) -> str:
        intent = state["intent"]
        pk = state.get("project_key")
        if intent in _NEEDS_PROJECT and not pk:
            log.info("[ROUTER] no project key for %r → clarify_project", intent)
            return "clarify_project"
        log.info("[ROUTER] → %r  project_key=%r", intent, pk)
        return intent

    g: StateGraph = StateGraph(AgentState)
    g.add_node("supervisor", supervisor_node)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_node("aggregator", aggregator_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route, {n: n for n in SPECIALIZED_NODES})
    for name in SPECIALIZED_NODES:
        g.add_edge(name, "aggregator")
    g.add_edge("aggregator", END)

    return g.compile()
