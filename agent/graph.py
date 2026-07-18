"""
Graph wiring — assembles the LangGraph supervisor graph from individual modules.

Flow:
    START → supervisor → [specialized node] → aggregator → END

Each component lives in its own module:
    agent/supervisor.py  — intent classification
    agent/nodes.py       — draft_deliverables, track_milestones, flag_risks,
                           generate_status_report, default
    agent/aggregator.py  — final formatting pass
    agent/state.py       — AgentState, shared helpers
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
    "draft_deliverables",
    "track_milestones",
    "flag_risks",
    "generate_status_report",
    "default",
)


def build_graph(llm: LLMClient, mcp: Any) -> Any:
    supervisor_node = make_supervisor_node(llm)
    aggregator_node = make_aggregator_node(llm)
    nodes = make_nodes(llm, mcp)

    def route_from_supervisor(state: AgentState) -> str:
        intent = state["intent"]
        log.info("[ROUTER] dispatching to → %r", intent)
        return intent

    g: StateGraph = StateGraph(AgentState)

    g.add_node("supervisor", supervisor_node)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.add_node("aggregator", aggregator_node)

    g.add_edge(START, "supervisor")
    g.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {name: name for name in SPECIALIZED_NODES},
    )
    for name in SPECIALIZED_NODES:
        g.add_edge(name, "aggregator")
    g.add_edge("aggregator", END)

    return g.compile()
